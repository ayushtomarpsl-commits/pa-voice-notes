"""SQLite storage. One file (data/pa.sqlite), WAL mode so the app, the web UI and the
MCP server (a separate process) can read and write at the same time.

Times are local wall-clock ISO strings "YYYY-MM-DDTHH:MM:SS". `day` is the local date.
Every thread gets its own connection through `conn()`.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id INTEGER PRIMARY KEY,
  day TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT NOT NULL,
  title TEXT,
  summary TEXT,
  people TEXT NOT NULL DEFAULT '[]',
  project TEXT,
  category TEXT,                  -- work | personal | small_talk (from the note-maker)
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','ended','processing','processed','error','skipped')),
  error TEXT,
  processed_at TEXT
);
CREATE INDEX IF NOT EXISTS conv_day ON conversations(day, started_at);

CREATE TABLE IF NOT EXISTS segments (
  id INTEGER PRIMARY KEY,
  day TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('me','call')),
  text TEXT NOT NULL,
  lang TEXT,
  avg_logprob REAL,
  conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
  hint TEXT                       -- JSON from the offline tagger, or NULL
);
CREATE INDEX IF NOT EXISTS seg_day ON segments(day, started_at);
CREATE INDEX IF NOT EXISTS seg_conv ON segments(conversation_id);

CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY,
  day TEXT NOT NULL,
  conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
  type TEXT NOT NULL CHECK (type IN ('bug_explanation','feature_requirement','decision','info','question')),
  project TEXT NOT NULL DEFAULT 'Other',
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '{}',  -- JSON: the type-specific fields
  evidence TEXT,
  pinned INTEGER NOT NULL DEFAULT 0,
  origin TEXT NOT NULL DEFAULT 'claude',  -- claude | claude-code | manual
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS note_day ON notes(day, type);

CREATE TABLE IF NOT EXISTS todos (
  id INTEGER PRIMARY KEY,
  day TEXT NOT NULL,              -- day it was first heard
  conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
  title TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'other',
  priority TEXT NOT NULL DEFAULT 'none',
  owner TEXT NOT NULL DEFAULT 'me',
  due TEXT,
  project TEXT NOT NULL DEFAULT 'Other',
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','dropped')),
  evidence TEXT,
  confidence REAL NOT NULL DEFAULT 1.0,
  origin TEXT NOT NULL DEFAULT 'claude',
  log TEXT NOT NULL DEFAULT '[]', -- JSON list of {at, text}: comments and status changes
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  done_at TEXT
);
CREATE INDEX IF NOT EXISTS todo_status ON todos(status, priority);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  kind TEXT NOT NULL,             -- pause, resume, private_on, device_lost, error, processed ...
  detail TEXT
);

CREATE TABLE IF NOT EXISTS processing_runs (
  id INTEGER PRIMARY KEY,
  conversation_id INTEGER,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  model TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  status TEXT NOT NULL,
  error TEXT
);

-- Full-text search. The extra M* categories keep Devanagari words whole.
CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
  text, content='segments', content_rowid='id',
  tokenize="unicode61 remove_diacritics 2 categories 'L* N* Co M*'");
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
  title, body, content='notes', content_rowid='id',
  tokenize="unicode61 remove_diacritics 2 categories 'L* N* Co M*'");
CREATE VIRTUAL TABLE IF NOT EXISTS todos_fts USING fts5(
  title, detail, content='todos', content_rowid='id',
  tokenize="unicode61 remove_diacritics 2 categories 'L* N* Co M*'");

CREATE TRIGGER IF NOT EXISTS seg_ai AFTER INSERT ON segments BEGIN
  INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text); END;
CREATE TRIGGER IF NOT EXISTS seg_ad AFTER DELETE ON segments BEGIN
  INSERT INTO segments_fts(segments_fts, rowid, text) VALUES ('delete', old.id, old.text); END;
CREATE TRIGGER IF NOT EXISTS seg_au AFTER UPDATE OF text ON segments BEGIN
  INSERT INTO segments_fts(segments_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text); END;

CREATE TRIGGER IF NOT EXISTS note_ai AFTER INSERT ON notes BEGIN
  INSERT INTO notes_fts(rowid, title, body) VALUES (new.id, new.title, new.body); END;
CREATE TRIGGER IF NOT EXISTS note_ad AFTER DELETE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body); END;
CREATE TRIGGER IF NOT EXISTS note_au AFTER UPDATE OF title, body ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
  INSERT INTO notes_fts(rowid, title, body) VALUES (new.id, new.title, new.body); END;

CREATE TRIGGER IF NOT EXISTS todo_ai AFTER INSERT ON todos BEGIN
  INSERT INTO todos_fts(rowid, title, detail) VALUES (new.id, new.title, new.detail); END;
CREATE TRIGGER IF NOT EXISTS todo_ad AFTER DELETE ON todos BEGIN
  INSERT INTO todos_fts(todos_fts, rowid, title, detail) VALUES ('delete', old.id, old.title, old.detail); END;
CREATE TRIGGER IF NOT EXISTS todo_au AFTER UPDATE OF title, detail ON todos BEGIN
  INSERT INTO todos_fts(todos_fts, rowid, title, detail) VALUES ('delete', old.id, old.title, old.detail);
  INSERT INTO todos_fts(rowid, title, detail) VALUES (new.id, new.title, new.detail); END;
"""

STAGES = ("todo", "doing", "blocked")   # board columns for open to-dos (Done = status done)
PRIORITY_ORDER = ["P0", "P0-1", "P0-2", "P0-3", "P1", "P2", "P3", "none"]
NOTE_TYPES = ["bug_explanation", "feature_requirement", "decision", "info", "question"]
TODO_FIELDS = ("title", "detail", "kind", "priority", "owner", "due", "project", "status", "confidence",
               "stage", "topic_id")

_local = threading.local()
_init_lock = threading.Lock()
_initialised: set[str] = set()


# ---------------------------------------------------------------- helpers
def now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def today() -> str:
    return date.today().isoformat()


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in ("people", "body", "log", "hint", "points"):
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                pass
    return d


def _rows(rs) -> list[dict]:
    return [_row(r) for r in rs]


def conn(path: Path | None = None) -> sqlite3.Connection:
    """Thread-local connection. Creates the schema on first use."""
    path = Path(path or config.DB_PATH)
    key = str(path)
    cache = getattr(_local, "conns", None)
    if cache is None:
        cache = _local.conns = {}
    c = cache.get(key)
    if c is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(key, timeout=10, isolation_level=None)  # autocommit; use tx() for groups
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout = 10000")
        c.execute("PRAGMA foreign_keys = ON")
        with _init_lock:
            if key not in _initialised:
                c.execute("PRAGMA journal_mode = WAL")
                c.execute("PRAGMA synchronous = NORMAL")
                c.executescript(SCHEMA)
                _migrate(c)
                c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                _initialised.add(key)
        cache[key] = c
    return c


SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS speakers (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'auto' CHECK (kind IN ('me','named','auto')),  -- auto = random name, not checked yet
  centroid BLOB,                  -- mean voice fingerprint (float32), never leaves the laptop
  samples INTEGER NOT NULL DEFAULT 0,
  seconds REAL NOT NULL DEFAULT 0,
  merged_into INTEGER REFERENCES speakers(id),
  created_at TEXT NOT NULL,
  last_heard TEXT
);

CREATE TABLE IF NOT EXISTS topics (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  project TEXT NOT NULL DEFAULT 'Other',
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','closed')),
  summary TEXT NOT NULL DEFAULT '',     -- current state (from the newest update)
  first_day TEXT NOT NULL,
  last_day TEXT NOT NULL,
  pinned INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS topic_last ON topics(status, last_day);

CREATE TABLE IF NOT EXISTS topic_updates (   -- one dated section per conversation that discussed the topic
  id INTEGER PRIMARY KEY,
  topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  day TEXT NOT NULL,
  conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
  summary TEXT NOT NULL DEFAULT '',
  points TEXT NOT NULL DEFAULT '[]',      -- every point said about the topic, in order
  people TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tu_topic ON topic_updates(topic_id, day);
"""


def _add_col(c: sqlite3.Connection, table: str, col: str, ddl: str) -> None:
    if col not in {r[1] for r in c.execute(f"PRAGMA table_info({table})")}:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def _migrate(c: sqlite3.Connection) -> None:
    """Upgrade an older pa.sqlite in place (only adds things, never removes)."""
    _add_col(c, "conversations", "category", "TEXT")
    # v3: voices, topics with dated history, full discussion points, to-do board stage
    c.executescript(SCHEMA_V3)
    _add_col(c, "segments", "speaker_id", "INTEGER")
    _add_col(c, "segments", "speaker_score", "REAL")
    _add_col(c, "conversations", "points", "TEXT NOT NULL DEFAULT '[]'")
    _add_col(c, "notes", "topic_id", "INTEGER")
    _add_col(c, "todos", "topic_id", "INTEGER")
    _add_col(c, "todos", "stage", "TEXT NOT NULL DEFAULT 'todo'")   # todo | doing | blocked (open items)
    c.execute("CREATE INDEX IF NOT EXISTS seg_speaker ON segments(speaker_id)")


class tx:
    """`with tx() as c:` runs the block in one IMMEDIATE transaction."""

    def __init__(self, path: Path | None = None):
        self.c = conn(path)

    def __enter__(self) -> sqlite3.Connection:
        self.c.execute("BEGIN IMMEDIATE")
        return self.c

    def __exit__(self, exc_type, exc, tb):
        self.c.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def close_all() -> None:
    for c in getattr(_local, "conns", {}).values():
        c.close()
    _local.conns = {}


def log_event(kind: str, detail: str | None = None) -> None:
    conn().execute("INSERT INTO events(at, kind, detail) VALUES (?,?,?)", (now(), kind, detail))


# ---------------------------------------------------------------- segments + conversations
def add_segment(started_at: str, ended_at: str, source: str, text: str, lang: str | None = None,
                avg_logprob: float | None = None, hint: dict | None = None, gap_min: float = 4,
                max_min: float = 20, speaker_id: int | None = None, speaker_score: float | None = None) -> dict:
    """Store one transcribed piece and attach it to a conversation.
    A new conversation starts when the last one ended more than `gap_min` minutes ago, or when the
    last one is already `max_min` minutes long (a non-stop talk is cut into parts)."""
    day = started_at[:10]
    with tx() as c:
        last = c.execute(
            "SELECT * FROM conversations WHERE day=? ORDER BY ended_at DESC LIMIT 1", (day,)).fetchone()
        conv_id = None
        if last is not None and last["status"] in ("open", "ended"):
            gap = (datetime.fromisoformat(started_at) - datetime.fromisoformat(last["ended_at"])).total_seconds()
            length = (datetime.fromisoformat(started_at) - datetime.fromisoformat(last["started_at"])).total_seconds()
            if gap <= gap_min * 60 and length < max_min * 60:
                conv_id = last["id"]
                c.execute("UPDATE conversations SET ended_at=max(ended_at, ?), status='open' WHERE id=?",
                          (ended_at, conv_id))
        if conv_id is None:
            conv_id = c.execute(
                "INSERT INTO conversations(day, started_at, ended_at) VALUES (?,?,?)",
                (day, started_at, ended_at)).lastrowid
        seg_id = c.execute(
            "INSERT INTO segments(day, started_at, ended_at, source, text, lang, avg_logprob, conversation_id, hint, "
            "speaker_id, speaker_score) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (day, started_at, ended_at, source, text, lang, avg_logprob, conv_id,
             json.dumps(hint, ensure_ascii=False) if hint else None, speaker_id, speaker_score)).lastrowid
    return get_segment(seg_id)


def get_segment(seg_id: int) -> dict | None:
    return _row(conn().execute("SELECT * FROM segments WHERE id=?", (seg_id,)).fetchone())


def segments_between(source: str, start: str, end: str) -> list[dict]:
    return _rows(conn().execute(
        "SELECT * FROM segments WHERE source=? AND ended_at>=? AND started_at<=?", (source, start, end)))


def delete_segment(seg_id: int) -> None:
    conn().execute("DELETE FROM segments WHERE id=?", (seg_id,))


def list_segments(day: str | None = None, conversation_id: int | None = None,
                  after_id: int = 0, limit: int = 500) -> list[dict]:
    q, a = (f"SELECT s.*, {_SPK} FROM segments s LEFT JOIN speakers sp ON sp.id = s.speaker_id "
            "WHERE s.id>?"), [after_id]
    if day:
        q += " AND s.day=?"; a.append(day)
    if conversation_id:
        q += " AND s.conversation_id=?"; a.append(conversation_id)
    q += " ORDER BY s.started_at, s.id LIMIT ?"; a.append(limit)
    return _rows(conn().execute(q, a))


_SPK = "sp.name AS speaker_name, sp.kind AS speaker_kind"


def latest_segments(limit: int = 30) -> list[dict]:
    rows = _rows(conn().execute(f"SELECT s.*, {_SPK} FROM segments s LEFT JOIN speakers sp ON sp.id = s.speaker_id "
                                "ORDER BY s.id DESC LIMIT ?", (limit,)))
    return list(reversed(rows))


def split_long_conversations(max_min: float) -> int:
    """Cut not-yet-processed conversations that are longer than `max_min` minutes into parts.
    Returns how many new conversations were made."""
    made = 0
    with tx() as c:
        convs = c.execute("SELECT * FROM conversations WHERE status IN ('open','ended','error')").fetchall()
        for conv in convs:
            segs = c.execute("SELECT id, started_at, ended_at FROM segments WHERE conversation_id=? "
                             "ORDER BY started_at, id", (conv["id"],)).fetchall()
            if not segs:
                continue
            part_start, part_id, parts = segs[0]["started_at"], conv["id"], [[]]
            for sg in segs:
                if (datetime.fromisoformat(sg["started_at"]) - datetime.fromisoformat(part_start)).total_seconds() >= max_min * 60:
                    parts.append([])
                    part_start = sg["started_at"]
                parts[-1].append(sg)
            if len(parts) == 1:
                continue
            last_status = conv["status"]
            for i, part in enumerate(parts):
                s0, s1 = part[0]["started_at"], max(x["ended_at"] for x in part)
                if i == 0:
                    c.execute("UPDATE conversations SET ended_at=?, status='ended', error=NULL WHERE id=?", (s1, conv["id"]))
                    continue
                new_id = c.execute("INSERT INTO conversations(day, started_at, ended_at, status) VALUES (?,?,?,?)",
                                   (conv["day"], s0, s1, last_status if i == len(parts) - 1 else "ended")).lastrowid
                c.executemany("UPDATE segments SET conversation_id=? WHERE id=?", [(new_id, x["id"]) for x in part])
                made += 1
    return made


def close_idle_conversations(gap_min: float) -> list[int]:
    """Mark open conversations as 'ended' when nothing was said for `gap_min` minutes."""
    cutoff = (datetime.now() - timedelta(minutes=gap_min)).strftime("%Y-%m-%dT%H:%M:%S")
    with tx() as c:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM conversations WHERE status='open' AND ended_at < ?", (cutoff,))]
        for i in ids:
            c.execute("UPDATE conversations SET status='ended' WHERE id=?", (i,))
    return ids


def get_conversation(conv_id: int) -> dict | None:
    return _row(conn().execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone())


def list_conversations(day: str | None = None, status: str | None = None) -> list[dict]:
    q = ("SELECT c.*, (SELECT count(*) FROM segments s WHERE s.conversation_id=c.id) AS segment_count, "
         "(SELECT count(*) FROM notes n WHERE n.conversation_id=c.id) AS note_count, "
         "(SELECT count(*) FROM todos t WHERE t.conversation_id=c.id) AS todo_count "
         "FROM conversations c WHERE 1=1")
    a: list = []
    if day:
        q += " AND c.day=?"; a.append(day)
    if status:
        q += " AND c.status=?"; a.append(status)
    q += " ORDER BY c.started_at"
    return _rows(conn().execute(q, a))


def set_conversation(conv_id: int, **fields) -> None:
    allowed = {"title", "summary", "people", "project", "category", "status", "error", "processed_at", "points"}
    sets = {k: (json.dumps(v, ensure_ascii=False) if k in ("people", "points") else v)
            for k, v in fields.items() if k in allowed}
    if not sets:
        return
    conn().execute(f"UPDATE conversations SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                   (*sets.values(), conv_id))


def conversation_transcript(conv_id: int) -> str:
    """Lines like '[10:01:02 Meera (call)] text'. The name comes from voice recognition; without it,
    the source ('me' = microphone, 'call' = PC sound) is used."""
    names = {r["id"]: r["name"] for r in conn().execute("SELECT id, name FROM speakers")}
    lines = []
    for s in list_segments(conversation_id=conv_id, limit=100000):
        who = names.get(s.get("speaker_id"))
        label = f"{who} ({s['source']})" if who else s["source"]
        lines.append(f"[{s['started_at'][11:19]} {label}] {s['text']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- notes
def add_note(day: str, type: str, title: str, body: dict, project: str = "Other",
             conversation_id: int | None = None, evidence: str | None = None, origin: str = "claude") -> dict:
    if type not in NOTE_TYPES:
        raise ValueError(f"unknown note type {type!r}")
    t = now()
    nid = conn().execute(
        "INSERT INTO notes(day, conversation_id, type, project, title, body, evidence, origin, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (day, conversation_id, type, project or "Other", title.strip() or "(untitled)",
         json.dumps(body, ensure_ascii=False), evidence, origin, t, t)).lastrowid
    return get_note(nid)


def get_note(note_id: int) -> dict | None:
    return _row(conn().execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone())


def list_notes(day: str | None = None, type: str | None = None, project: str | None = None,
               conversation_id: int | None = None, pinned: bool | None = None, limit: int = 500) -> list[dict]:
    q, a = "SELECT * FROM notes WHERE 1=1", []
    for col, val in (("day", day), ("type", type), ("project", project), ("conversation_id", conversation_id)):
        if val:
            q += f" AND {col}=?"; a.append(val)
    if pinned is not None:
        q += " AND pinned=?"; a.append(1 if pinned else 0)
    q += " ORDER BY pinned DESC, created_at DESC LIMIT ?"; a.append(limit)
    return _rows(conn().execute(q, a))


def update_note(note_id: int, **fields) -> dict | None:
    allowed = {"title", "body", "project", "type", "pinned", "topic_id"}
    sets = {k: (json.dumps(v, ensure_ascii=False) if k == "body" else v)
            for k, v in fields.items() if k in allowed and v is not None}
    if sets:
        sets["updated_at"] = now()
        conn().execute(f"UPDATE notes SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                       (*sets.values(), note_id))
    return get_note(note_id)


def delete_note(note_id: int) -> None:
    conn().execute("DELETE FROM notes WHERE id=?", (note_id,))


# ---------------------------------------------------------------- todos
def add_todo(day: str, title: str, detail: str = "", kind: str = "other", priority: str = "none",
             owner: str = "me", due: str | None = None, project: str = "Other", status: str = "open",
             evidence: str | None = None, confidence: float = 1.0, conversation_id: int | None = None,
             origin: str = "claude") -> dict:
    if priority not in PRIORITY_ORDER:
        priority = "none"
    t = now()
    tid = conn().execute(
        "INSERT INTO todos(day, conversation_id, title, detail, kind, priority, owner, due, project, status, "
        "evidence, confidence, origin, created_at, updated_at, done_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (day, conversation_id, title.strip() or "(untitled)", detail or "", kind or "other", priority,
         owner or "me", due, project or "Other", status, evidence, max(0.0, min(1.0, float(confidence))),
         origin, t, t, t if status == "done" else None)).lastrowid
    return get_todo(tid)


def get_todo(todo_id: int) -> dict | None:
    return _row(conn().execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone())


_PRIO_SQL = "CASE priority " + " ".join(f"WHEN '{p}' THEN {i}" for i, p in enumerate(PRIORITY_ORDER)) + " END"


def list_todos(status: str | None = "open", project: str | None = None, priority: str | None = None,
               day: str | None = None, owner: str | None = None, limit: int = 1000,
               topic_id: int | None = None) -> list[dict]:
    """status: open | done | dropped | all | 'today' (open + done today)."""
    q, a = ("SELECT todos.*, (SELECT title FROM topics WHERE topics.id=todos.topic_id) AS topic_title "
            "FROM todos WHERE 1=1"), []
    if status == "today":
        q += " AND (status='open' OR (status='done' AND substr(done_at,1,10)=?))"; a.append(today())
    elif status and status != "all":
        q += " AND status=?"; a.append(status)
    for col, val in (("project", project), ("priority", priority), ("day", day), ("owner", owner),
                     ("topic_id", topic_id)):
        if val:
            q += f" AND {col}=?"; a.append(val)
    q += f" ORDER BY status='done', {_PRIO_SQL}, (due IS NULL), due, created_at LIMIT ?"; a.append(limit)
    return _rows(conn().execute(q, a))


def update_todo(todo_id: int, comment: str | None = None, **fields) -> dict | None:
    cur = get_todo(todo_id)
    if cur is None:
        return None
    sets = {k: v for k, v in fields.items() if k in TODO_FIELDS and v is not None}
    if "priority" in sets and sets["priority"] not in PRIORITY_ORDER:
        sets.pop("priority")
    if "due" in fields and fields["due"] == "":
        sets["due"] = None
    if "stage" in sets and sets["stage"] not in STAGES:
        sets.pop("stage")
    log = cur.get("log") or []
    if "stage" in sets and sets["stage"] != cur.get("stage"):
        log.append({"at": now(), "text": f"moved to {sets['stage']}"})
    if "status" in sets and sets["status"] != cur["status"]:
        log.append({"at": now(), "text": f"status {cur['status']} -> {sets['status']}"})
        sets["done_at"] = now() if sets["status"] == "done" else None
    if comment:
        log.append({"at": now(), "text": comment})
    sets["log"] = json.dumps(log, ensure_ascii=False)
    sets["updated_at"] = now()
    conn().execute(f"UPDATE todos SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", (*sets.values(), todo_id))
    return get_todo(todo_id)


def delete_todo(todo_id: int) -> None:
    conn().execute("DELETE FROM todos WHERE id=?", (todo_id,))


# ---------------------------------------------------------------- days + search
def list_days(limit: int = 60) -> list[dict]:
    return _rows(conn().execute(
        "SELECT d.day, "
        "(SELECT count(*) FROM conversations c WHERE c.day=d.day) AS conversations, "
        "(SELECT count(*) FROM segments s WHERE s.day=d.day) AS segments, "
        "(SELECT count(*) FROM notes n WHERE n.day=d.day) AS notes, "
        "(SELECT count(*) FROM todos t WHERE t.day=d.day) AS todos, "
        "(SELECT count(*) FROM conversations c WHERE c.day=d.day AND c.status IN ('open','ended','error')) AS unprocessed "
        "FROM (SELECT day FROM segments UNION SELECT day FROM notes UNION SELECT day FROM todos) d "
        "ORDER BY d.day DESC LIMIT ?", (limit,)))


def _fts_query(q: str) -> str:
    words = [w for w in "".join(ch if ch.isalnum() or ch in "-_." else " " for ch in q).split() if w]
    return " ".join('"' + w.replace('"', '') + '"*' for w in words)


def search(q: str, limit: int = 50) -> dict:
    fq = _fts_query(q)
    if not fq:
        return {"segments": [], "notes": [], "todos": [], "topics": []}
    c = conn()
    segs = _rows(c.execute(
        "SELECT s.*, snippet(segments_fts, 0, '[', ']', '…', 12) AS snippet FROM segments_fts "
        "JOIN segments s ON s.id = segments_fts.rowid WHERE segments_fts MATCH ? ORDER BY rank LIMIT ?", (fq, limit)))
    notes = _rows(c.execute(
        "SELECT n.* FROM notes_fts JOIN notes n ON n.id = notes_fts.rowid WHERE notes_fts MATCH ? "
        "ORDER BY rank LIMIT ?", (fq, limit)))
    todos = _rows(c.execute(
        "SELECT t.* FROM todos_fts JOIN todos t ON t.id = todos_fts.rowid WHERE todos_fts MATCH ? "
        "ORDER BY rank LIMIT ?", (fq, limit)))
    like = f"%{q.strip()}%"
    topics = _rows(c.execute(
        "SELECT DISTINCT t.* FROM topics t LEFT JOIN topic_updates u ON u.topic_id=t.id "
        "WHERE t.title LIKE ? OR t.summary LIKE ? OR u.points LIKE ? OR u.summary LIKE ? "
        "ORDER BY t.last_day DESC LIMIT ?", (like, like, like, like, limit)))
    return {"segments": segs, "notes": notes, "todos": todos, "topics": topics}


def add_run(conversation_id: int, model: str) -> int:
    return conn().execute("INSERT INTO processing_runs(conversation_id, started_at, model, status) VALUES (?,?,?,?)",
                          (conversation_id, now(), model, "running")).lastrowid


def finish_run(run_id: int, status: str, input_tokens: int | None = None, output_tokens: int | None = None,
               error: str | None = None) -> None:
    conn().execute("UPDATE processing_runs SET finished_at=?, status=?, input_tokens=?, output_tokens=?, error=? "
                   "WHERE id=?", (now(), status, input_tokens, output_tokens, error, run_id))


def usage_today() -> dict:
    r = conn().execute("SELECT count(*) n, coalesce(sum(input_tokens),0) i, coalesce(sum(output_tokens),0) o "
                       "FROM processing_runs WHERE substr(started_at,1,10)=?", (today(),)).fetchone()
    return {"runs": r["n"], "input_tokens": r["i"], "output_tokens": r["o"]}


# ---------------------------------------------------------------- topics (cards with dated history)
def recent_topics(limit: int = 30) -> list[dict]:
    """Active topics, most recently discussed first (also shown to the note-maker so it can
    continue an existing topic instead of starting a new one)."""
    return _rows(conn().execute(
        "SELECT id, title, project, last_day, summary FROM topics WHERE status='active' "
        "ORDER BY last_day DESC, updated_at DESC LIMIT ?", (limit,)))


def find_topic(title: str, project: str | None = None) -> dict | None:
    return _row(conn().execute(
        "SELECT * FROM topics WHERE lower(title)=lower(?) ORDER BY (project=?) DESC, last_day DESC LIMIT 1",
        (title.strip(), project or "")).fetchone())


def create_topic(title: str, project: str, day: str, summary: str = "") -> dict:
    t = now()
    tid = conn().execute(
        "INSERT INTO topics(title, project, summary, first_day, last_day, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (title.strip()[:160] or "Untitled topic", project or "Other", summary, day, day, t, t)).lastrowid
    return get_topic_row(tid)


def get_topic_row(topic_id: int) -> dict | None:
    return _row(conn().execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone())


def add_topic_update(topic_id: int, day: str, conversation_id: int | None, summary: str,
                     points: list, people: list) -> int:
    t = now()
    uid = conn().execute(
        "INSERT INTO topic_updates(topic_id, day, conversation_id, summary, points, people, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (topic_id, day, conversation_id, summary or "", json.dumps(points or [], ensure_ascii=False),
         json.dumps(people or [], ensure_ascii=False), t)).lastrowid
    conn().execute("UPDATE topics SET summary=CASE WHEN ?<>'' THEN ? ELSE summary END, "
                   "last_day=max(last_day, ?), first_day=min(first_day, ?), status='active', updated_at=? WHERE id=?",
                   (summary or "", summary or "", day, day, t, topic_id))
    return uid


def delete_topic_updates_for(conversation_id: int) -> None:
    """Used when a conversation is processed again; topics left empty (and made by it) go too."""
    c = conn()
    tids = [r[0] for r in c.execute("SELECT DISTINCT topic_id FROM topic_updates WHERE conversation_id=?",
                                    (conversation_id,))]
    c.execute("DELETE FROM topic_updates WHERE conversation_id=?", (conversation_id,))
    for tid in tids:
        if not c.execute("SELECT 1 FROM topic_updates WHERE topic_id=?", (tid,)).fetchone():
            c.execute("UPDATE notes SET topic_id=NULL WHERE topic_id=?", (tid,))
            c.execute("UPDATE todos SET topic_id=NULL WHERE topic_id=?", (tid,))
            c.execute("DELETE FROM topics WHERE id=? AND pinned=0", (tid,))


def list_topics(status: str | None = "active", project: str | None = None, q: str | None = None,
                limit: int = 200) -> list[dict]:
    sql = ("SELECT t.*, "
           "(SELECT count(*) FROM topic_updates u WHERE u.topic_id=t.id) AS updates, "
           "(SELECT count(DISTINCT u.day) FROM topic_updates u WHERE u.topic_id=t.id) AS days, "
           "(SELECT count(*) FROM todos d WHERE d.topic_id=t.id AND d.status='open') AS open_todos, "
           "(SELECT count(*) FROM notes n WHERE n.topic_id=t.id) AS notes, "
           "(SELECT people FROM topic_updates u WHERE u.topic_id=t.id ORDER BY u.id DESC LIMIT 1) AS people "
           "FROM topics t WHERE 1=1")
    a: list = []
    if status and status != "all":
        sql += " AND t.status=?"; a.append(status)
    if project:
        sql += " AND t.project=?"; a.append(project)
    if q:
        sql += " AND (t.title LIKE ? OR t.summary LIKE ?)"; a += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY t.pinned DESC, t.last_day DESC, t.updated_at DESC LIMIT ?"; a.append(limit)
    return _rows(conn().execute(sql, a))


def topic_detail(topic_id: int) -> dict | None:
    t = get_topic_row(topic_id)
    if t is None:
        return None
    c = conn()
    t["updates"] = _rows(c.execute(
        "SELECT u.*, c.title AS conversation_title, c.started_at, c.ended_at FROM topic_updates u "
        "LEFT JOIN conversations c ON c.id=u.conversation_id WHERE u.topic_id=? ORDER BY u.day DESC, u.id DESC",
        (topic_id,)))
    t["notes"] = _rows(c.execute("SELECT * FROM notes WHERE topic_id=? ORDER BY created_at DESC", (topic_id,)))
    t["todos"] = list_todos(status="all", topic_id=topic_id)
    return t


def update_topic(topic_id: int, **fields) -> dict | None:
    allowed = {"title", "project", "status", "pinned", "summary"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if sets:
        sets["updated_at"] = now()
        conn().execute(f"UPDATE topics SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", (*sets.values(), topic_id))
    return get_topic_row(topic_id)


def merge_topics(source_id: int, target_id: int) -> dict | None:
    with tx() as c:
        c.execute("UPDATE topic_updates SET topic_id=? WHERE topic_id=?", (target_id, source_id))
        c.execute("UPDATE notes SET topic_id=? WHERE topic_id=?", (target_id, source_id))
        c.execute("UPDATE todos SET topic_id=? WHERE topic_id=?", (target_id, source_id))
        r = c.execute("SELECT min(day) f, max(day) l FROM topic_updates WHERE topic_id=?", (target_id,)).fetchone()
        c.execute("UPDATE topics SET first_day=coalesce(?, first_day), last_day=coalesce(?, last_day), updated_at=? "
                  "WHERE id=?", (r["f"], r["l"], now(), target_id))
        c.execute("DELETE FROM topics WHERE id=?", (source_id,))
    return get_topic_row(target_id)


def set_item_topic(table: str, item_id: int, topic_id: int | None) -> None:
    if table not in ("notes", "todos"):
        raise ValueError(table)
    conn().execute(f"UPDATE {table} SET topic_id=? WHERE id=?", (topic_id, item_id))
