"""Notes and to-dos from a conversation, keeping only office talk.

Ways in, one way to save:
  1. Groq free LLM (default, free): strict JSON output.
  2. Claude API (paid, optional): structured output.
  3. Claude Code (no key needed): the 'pa-process' skill reads transcripts through the MCP server,
     makes the same JSON itself, and calls `save_extraction` -> `import_extraction()`.
Every answer has a category (work / personal / small_talk). With office_only on, only work
conversations keep notes and to-dos.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import threading
from datetime import date, datetime
from pathlib import Path

from . import config, db, keys
from .schema import PRIORITIES, TODO_KINDS, build_flat_schema, build_schema

log = logging.getLogger("pa.extract")
HERE = Path(__file__).resolve().parent
PROFILE_PATH = config.DATA_DIR / "profile.md"
FALLBACK_MODELS = {"claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5-5"}
_busy = threading.Lock()
_paused_until = 0.0     # after every free model hit its DAILY limit, automatic note-making waits


# ------------------------------------------------------------------ prompt
def load_profile() -> str:
    if not PROFILE_PATH.exists():
        PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PROFILE_PATH.write_text((HERE / "profile_default.md").read_text(encoding="utf-8"), encoding="utf-8")
    return PROFILE_PATH.read_text(encoding="utf-8")


def system_prompt(short: bool = False) -> str:
    """short=True drops the worked example (saves ~600 tokens for the free Groq per-minute limit)."""
    profile = re.sub(r"^# My profile.*?\n(?=## )", "", load_profile(), flags=re.S).strip()
    my_name = (config.load().get("my_name") or "").strip()
    if not my_name or my_name.lower() == "me":
        my_name = "the user"
    text = ((HERE / "extract_prompt.md").read_text(encoding="utf-8")
            .replace("{{PROFILE}}", profile).replace("{{MY_NAME}}", my_name))
    if short and "\n## Example" in text:
        text = text[:text.index("\n## Example")].rstrip() + "\n"
    return text


def user_message(conv: dict, transcript: str | None = None, part: tuple[int, int] | None = None) -> str:
    d = date.fromisoformat(conv["day"])
    open_todos = db.list_todos(status="open", limit=25)       # kept short: free plan counts every token
    todo_lines = "; ".join(f"#{t['id']} {t['title'][:70]} ({t['owner']})" for t in open_todos) or "none"
    part_note = f"; part {part[0]} of {part[1]} of one long conversation" if part and part[1] > 1 else ""
    topic_lines = "; ".join(f"#{t['id']} {t['title'][:60]}" for t in db.recent_topics(20)) or "none"
    if transcript is None:
        transcript = db.conversation_transcript(conv["id"])
    return (f"<context>date: {d.isoformat()} ({d.strftime('%A')}); conversation: {conv['id']} "
            f"({conv['started_at'][11:16]}-{conv['ended_at'][11:16]}){part_note}; open_todos: {todo_lines}; "
            f"open_topics: {topic_lines}</context>\n"
            f"<transcript>\n{transcript}\n</transcript>")


def est_tokens(text: str) -> int:
    """Rough, on-the-safe-side token count (measured 16-Oct: bytes/4 was ~1.4x too low for this text)."""
    return len(text.encode("utf-8")) // 3 + 1


def split_transcript(text: str, max_tokens: int) -> list[str]:
    parts, cur, size = [], [], 0
    for line in text.splitlines():
        t = est_tokens(line)
        if cur and size + t > max_tokens:
            parts.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += t
    if cur:
        parts.append("\n".join(cur))
    return parts or [""]


def merge_parts(results: list[dict]) -> dict:
    """Join the answers for the parts of one long conversation."""
    work = [r for r in results if (r.get("conversation") or {}).get("category") == "work"]
    head = (work or results)[0]["conversation"]
    people = []
    for r in results:
        for p in (r.get("conversation") or {}).get("people") or []:
            if p not in people:
                people.append(p)
    summary = " ".join((r.get("conversation") or {}).get("summary") or "" for r in (work or results)).strip()
    topics: dict[str, dict] = {}       # same subject in several parts -> one topic, points in order
    for r in results:
        for t in r.get("topics") or []:
            key = (t.get("same_as") or "") + "|" + _norm(t.get("title") or "")
            cur = topics.get(key) or next((v for k, v in topics.items()
                                           if _similar(v["title"], t.get("title") or "") >= 0.8), None)
            if cur is None:
                topics[key] = {**t, "points": list(t.get("points") or [])}
            else:
                cur["points"] += [p for p in t.get("points") or [] if p not in cur["points"]]
                cur["summary"] = t.get("summary") or cur["summary"]   # latest part knows the latest state
                cur["same_as"] = cur.get("same_as") or t.get("same_as")
    return {"conversation": {**head, "people": people, "summary": summary[:1500],
                             "category": "work" if work else head.get("category", "small_talk")},
            "topics": list(topics.values()),
            "notes": [n for r in results for n in r.get("notes") or []],
            "todos": [t for r in results for t in r.get("todos") or []]}


# ------------------------------------------------------------------ keys (kept for older callers)
def get_api_key() -> str | None:
    return keys.get("anthropic")


def set_api_key(key: str | None) -> None:
    keys.set("anthropic", key)


def ready(cfg: dict) -> bool:
    """Can notes be made automatically right now (a key for the chosen engine exists)?"""
    eng = cfg.get("llm_engine", "groq")
    return (eng == "groq" and bool(keys.get("groq"))) or (eng == "claude" and bool(keys.get("anthropic")))


# ------------------------------------------------------------------ engines
def call_claude(conv: dict, cfg: dict, client=None) -> tuple[dict, dict]:
    """Returns (extraction JSON, usage dict). `client` can be a fake in tests."""
    if client is None:
        import anthropic
        key = get_api_key()
        if not key:
            raise RuntimeError("No Claude API key. Use the free Groq engine, or run /pa-process in Claude Code.")
        client = anthropic.Anthropic(api_key=key, max_retries=3, timeout=300)
    model = cfg["claude_model"]
    kwargs = dict(
        model=model, max_tokens=16000,
        system=[{"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_message(conv)}],
        output_config={"effort": cfg.get("claude_effort", "medium"),
                       "format": {"type": "json_schema", "schema": build_schema(cfg["projects"])}},
    )
    if model in FALLBACK_MODELS:  # if a safety check declines, let the server retry on a fallback model
        resp = client.beta.messages.create(**kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    else:
        resp = client.beta.messages.create(**kwargs)
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined this conversation (refusal).")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Answer was cut off (max_tokens). Try processing again.")
    text = next(b.text for b in resp.content if b.type == "text")
    usage = {"input_tokens": getattr(resp.usage, "input_tokens", None),
             "output_tokens": getattr(resp.usage, "output_tokens", None)}
    return json.loads(text), usage


def call_groq(conv: dict, cfg: dict, client=None, sleep=None) -> tuple[dict, dict]:
    """Free Groq LLM. Long transcripts are sent in parts that fit the free per-minute token limit.
    'Wait a minute' answers (short retry-after) are waited out; when a model's daily limit is used
    up, the smaller model is tried."""
    import time as _time
    from .groq_api import LLM_MODELS, Groq, GroqError, RateLimited
    sleep = sleep or _time.sleep
    if client is None:
        key = keys.get("groq")
        if not key:
            raise RuntimeError("No Groq API key. Add the free key in Settings, or run /pa-process in Claude Code.")
        client = Groq(key)
    first = cfg.get("groq_llm_model", LLM_MODELS[0])
    models = [first] + [m for m in LLM_MODELS if m != first]
    schema = build_flat_schema(cfg["projects"])
    system = system_prompt(short=True)
    budget = int(cfg.get("groq_chunk_tokens", 1800))
    queue = split_transcript(db.conversation_transcript(conv["id"]), budget)
    results, used, done = [], {"input_tokens": 0, "output_tokens": 0}, 0
    while queue:
        part = queue.pop(0)
        msg = user_message(conv, part, (done + 1, done + 1 + len(queue)))
        answer, too_big = None, False
        for m in models:
            for attempt in range(6):
                try:
                    data, usage = client.chat_json(system, msg, schema, m)
                    answer = data
                    used["input_tokens"] += usage.get("prompt_tokens") or 0
                    used["output_tokens"] += usage.get("completion_tokens") or 0
                    used["model"] = m
                    if m != models[0]:
                        used["fallback"] = True
                    break
                except RateLimited as e:
                    kind = e.kind
                    if getattr(e, "too_large", False) and kind != "day":
                        too_big = True                         # never fits one minute: send less
                        break
                    if kind == "minute" and attempt < 5:       # per-minute limit: wait, then try again
                        wait = min(max(e.retry_after, 1.0), 65.0)
                        log.info("Groq %s busy, waiting %.0fs", m, wait)
                        sleep(wait + 2)
                        continue
                    log.warning("Groq %s %s limit reached, trying next model", m, kind)
                    break
                except GroqError as e:
                    if "413" in str(e) or "too large" in str(e).lower():
                        too_big = True
                        break
                    raise
            if answer is not None or too_big:
                break
        if too_big:
            halves = split_transcript(part, max(200, est_tokens(part) // 2))
            if len(halves) < 2:
                raise RuntimeError("A single transcript line is too large for the free Groq plan.")
            log.info("part too large for the free plan; splitting it in %d", len(halves))
            queue = halves + queue
            continue
        if answer is None:
            raise RuntimeError("Groq free limit reached for all models. Notes will be made later.")
        results.append(answer)
        done += 1
    return (results[0] if len(results) == 1 else merge_parts(results)), used


def call_llm(conv: dict, cfg: dict, client=None) -> tuple[dict, dict]:
    eng = cfg.get("llm_engine", "groq")
    if eng == "groq":
        return call_groq(conv, cfg, client)
    if eng == "claude":
        return call_claude(conv, cfg, client)
    raise RuntimeError("Notes engine is 'Claude Code only': run /pa-process in Claude Code.")


def _empty_work(data: dict) -> bool:
    """Office talk, but no points, notes or to-dos came back."""
    meta = data.get("conversation") or {}
    if meta.get("category") not in (None, "", "work"):
        return False
    return not any((t or {}).get("points") for t in data.get("topics") or [])         and not data.get("notes") and not data.get("todos")


def _word_count(conv_id: int) -> int:
    return sum(len(s["text"].split()) for s in db.list_segments(conversation_id=conv_id, limit=100000))


def extract_conversation(conv_id: int, cfg: dict | None = None, client=None, force: bool = False) -> dict:
    """force=True: make notes even if it is short or not office talk ('make notes anyway')."""
    cfg = cfg or config.load()
    conv = db.get_conversation(conv_id)
    if conv is None:
        raise KeyError(conv_id)
    if not force and _word_count(conv_id) < int(cfg.get("min_words_for_notes", 25)):
        return import_extraction(conv_id, {"conversation": {
            "title": "Short talk", "summary": "Too short to make notes.", "people": [], "project": "Other",
            "category": "small_talk"}, "notes": [], "todos": []}, origin="filter", cfg=cfg)
    db.set_conversation(conv_id, status="processing", error=None)
    eng = cfg.get("llm_engine", "groq")
    run = db.add_run(conv_id, cfg.get("groq_llm_model") if eng == "groq" else cfg["claude_model"])
    try:
        data, usage = call_llm(conv, cfg, client)
        if usage.get("fallback") and _empty_work(data) and _word_count(conv_id) >= 150:
            # the small backup model sometimes returns nothing useful: keep the old notes, retry later
            raise RuntimeError("Groq free limit reached for the better models (the backup model gave empty "
                               "notes, so the old notes were kept). Will retry later.")
        result = import_extraction(conv_id, data, origin=eng, cfg=cfg, force=force)
        db.finish_run(run, "ok", usage.get("input_tokens"), usage.get("output_tokens"))
        return result
    except Exception as e:
        db.finish_run(run, "error", error=str(e)[:500])
        db.set_conversation(conv_id, status="error", error=str(e)[:500])
        raise


def process_pending(cfg: dict | None = None, day: str | None = None, client=None,
                    respect_pause: bool = True) -> dict:
    """Process every finished conversation that has no notes yet. Safe to call often.
    respect_pause=False (the 'Make notes' button) ignores the 30-minute pause after daily limits."""
    global _paused_until
    import time as _time
    cfg = cfg or config.load()
    done, failed = [], []
    if respect_pause and _time.time() < _paused_until:
        return {"processed": done, "failed": failed, "busy": False, "paused": True}
    if not _busy.acquire(blocking=False):
        return {"processed": done, "failed": failed, "busy": True}
    try:
        db.split_long_conversations(float(cfg.get("max_conversation_min", 20)))
        db.close_idle_conversations(float(cfg["conversation_gap_min"]))
        for st in ("ended", "error"):
            for conv in db.list_conversations(day=day, status=st):
                try:
                    extract_conversation(conv["id"], cfg, client)
                    done.append(conv["id"])
                except Exception as e:
                    log.warning("conversation %s failed: %s", conv["id"], e)
                    failed.append({"id": conv["id"], "error": str(e)})
                    if "limit reached for" in str(e):
                        _paused_until = _time.time() + 1800
                        log.info("all free models used up; automatic notes pause for 30 minutes")
                    if "API key" in str(e) or "limit reached" in str(e) or "Claude Code" in str(e):
                        return {"processed": done, "failed": failed, "busy": False}
        if done and cfg.get("llm_engine", "groq") == "groq" and keys.get("groq"):
            try:
                merged = merge_duplicate_todos(cfg, client)
                if merged:
                    log.info("merged %d duplicate to-dos", merged)
            except Exception as e:  # merging is a nice-to-have; never block notes
                log.warning("to-do merge skipped: %s", e)
            try:
                merged = merge_duplicate_topics(cfg, client)
                if merged:
                    log.info("merged %d duplicate topics", merged)
            except Exception as e:
                log.warning("topic merge skipped: %s", e)
    finally:
        _busy.release()
    return {"processed": done, "failed": failed, "busy": False}


MERGE_PROMPT = """You clean a developer's to-do list. Find to-dos that mean the SAME piece of work
(same task said in different words, or one is just a step already covered by another). Do not group
tasks that are only related. For each group pick keep_id = the clearest, most complete one.
Return JSON only. If there are no duplicates, return {"groups": []}."""

MERGE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["groups"], "properties": {
    "groups": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                          "required": ["keep_id", "merge_ids", "reason"],
                                          "properties": {"keep_id": {"type": "integer"},
                                                         "merge_ids": {"type": "array", "items": {"type": "integer"}},
                                                         "reason": {"type": "string"}}}}}}


TOPIC_MERGE_PROMPT = """You clean a developer's list of discussion topics. Find topics that are the SAME
subject (for example a bug, its cause, its fix and the decision about it are one subject; two names for the
same feature are one subject). Do not group topics that are only related. For each group pick keep_id = the
topic with the clearest title. Return JSON only. If nothing should be merged, return {"groups": []}."""


def merge_duplicate_topics(cfg: dict | None = None, client=None, days: int = 14) -> int:
    """One small AI call over recent topics; same-subject topics are merged (all dated sections,
    notes and to-dos move to the kept topic, so no history is lost). Returns how many were merged."""
    from datetime import timedelta
    cfg = cfg or config.load()
    since = (date.today() - timedelta(days=days)).isoformat()
    topics = [t for t in db.list_topics(status="active") if t["last_day"] >= since and t["title"] != "Other points"]
    if len(topics) < 2:
        return 0
    if client is None:
        from .groq_api import Groq
        client = Groq(keys.get("groq"))
    listing = "\n".join(f"{t['id']}: [{t['project']}] {t['title']} — {t['summary'][:160]}" for t in topics[:60])
    data = None
    from .groq_api import LLM_MODELS, RateLimited
    for m in [cfg.get("groq_llm_model", LLM_MODELS[0])] + [x for x in LLM_MODELS if x != cfg.get("groq_llm_model")]:
        try:
            data, _ = client.chat_json(TOPIC_MERGE_PROMPT, f"Topics (id: text):\n{listing}", MERGE_SCHEMA, m,
                                       name="merge_topics")
            break
        except RateLimited:
            continue
    if data is None:
        return 0
    ids = {t["id"]: t for t in topics}
    merged = 0
    for g in data.get("groups") or []:
        keep = g.get("keep_id")
        if keep not in ids:
            continue
        for mid in g.get("merge_ids") or []:
            if mid in ids and mid != keep:
                db.merge_topics(mid, keep)
                db.log_event("topic_merge", f"#{mid} {ids[mid]['title']} -> #{keep} ({g.get('reason', '')[:100]})")
                ids.pop(mid)
                merged += 1
    return merged


def merge_duplicate_todos(cfg: dict | None = None, client=None) -> int:
    """One small AI call over the open, auto-made to-dos; duplicates are marked 'dropped' with a
    note 'merged into T<id>' (nothing is deleted). Returns how many were merged."""
    cfg = cfg or config.load()
    todos = [t for t in db.list_todos(status="open", limit=80) if t["origin"] != "manual"]
    if len(todos) < 2:
        return 0
    if client is None:
        from .groq_api import Groq
        client = Groq(keys.get("groq"))
    listing = "\n".join(f"{t['id']}: [{t['project']}] (owner {t['owner']}) {t['title']} — {t['detail'][:120]}"
                         for t in todos)
    data, _ = client.chat_json(MERGE_PROMPT, f"To-dos (id: text):\n{listing}", MERGE_SCHEMA,
                               cfg.get("groq_llm_model", "openai/gpt-oss-120b"), name="merge")
    ids = {t["id"]: t for t in todos}
    merged = 0
    for g in data.get("groups") or []:
        keep = ids.get(g.get("keep_id"))
        if keep is None:
            continue
        for mid in g.get("merge_ids") or []:
            other = ids.get(mid)
            if other is None or mid == keep["id"] or other["status"] != "open":
                continue
            db.update_todo(keep["id"], comment=f"Merged T{mid}: {other['title']}"
                           + (f" (heard: {other['evidence']})" if other.get("evidence") else ""),
                           due=other["due"] if other["due"] and not keep["due"] else None)
            db.update_todo(mid, status="dropped", comment=f"merged into T{keep['id']} ({g.get('reason', '')[:120]})")
            other["status"] = "dropped"
            merged += 1
    return merged


# ------------------------------------------------------------------ import (shared by both paths)
def _norm(s: str) -> str:
    return re.sub(r"[^\wऀ-ॿ]+", " ", (s or "").lower()).strip()


def quote_found(quote: str, transcript: str) -> bool:
    q, t = _norm(quote), _norm(transcript)
    if not q:
        return False
    if q in t:
        return True
    # fuzzy: best window match (ASR text and the quote may differ in small words)
    words, qn = t.split(), len(q.split())
    for i in range(0, max(1, len(words) - qn + 1)):
        if difflib.SequenceMatcher(None, q, " ".join(words[i:i + qn])).ratio() > 0.8:
            return True
    return False


def save_topics(conv_id: int, day: str, topics: list[dict], meta: dict) -> dict[str, int]:
    """Attach each topic of this conversation to an existing topic card (same_as id, same title,
    or a very similar title) or create a new card, then add today's dated section to it.
    Old sections are never changed, so the history of every topic is kept.
    Returns {normalised title: topic id} for linking notes and to-dos."""
    out: dict[str, int] = {}
    project = meta.get("project") or "Other"
    people = meta.get("people") or []
    all_points = []
    for t in topics:
        title = (t.get("title") or "").strip()
        points = [p for p in (t.get("points") or []) if p and p.strip()]
        if not title or (not points and not t.get("summary")):
            continue
        topic = None
        if t.get("same_as"):
            try:
                topic = db.get_topic_row(int(str(t["same_as"]).lstrip("#")))
            except ValueError:
                topic = None
        topic = topic or db.find_topic(title, project)
        if topic is None:
            for r in db.recent_topics(60):
                if _similar(r["title"], title) >= 0.75:
                    topic = db.get_topic_row(r["id"])
                    break
        if topic is None:
            topic = db.create_topic(title, project, day)
        db.add_topic_update(topic["id"], day, conv_id, t.get("summary") or "", points, people)
        out[_norm(title)] = topic["id"]
        out[_norm(topic["title"])] = topic["id"]
        all_points += [{"topic": topic["title"], "topic_id": topic["id"], "text": p} for p in points]
    db.set_conversation(conv_id, points=all_points)
    return out


def _similar(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    seq = difflib.SequenceMatcher(None, a, b).ratio()
    wa, wb = set(a.split()), set(b.split())
    jac = len(wa & wb) / max(1, len(wa | wb))
    return max(seq, jac)


def similar_note(day: str, ntype: str, title: str, conv_id: int, threshold: float = 0.6) -> dict | None:
    """An auto-made note of the same type, same day, with a very similar title (from another
    conversation, usually the previous part of the same long talk)."""
    best, best_score = None, threshold
    for n in db.list_notes(day=day, type=ntype):
        if n["conversation_id"] == conv_id or n["origin"] == "manual":
            continue
        score = _similar(n["title"], title)
        if score >= best_score:
            best, best_score = n, score
    return best


def merge_note_body(old: dict, new: dict) -> dict:
    """Keep the old note, fill gaps from the new one; list fields are joined; a later 'fix' wins
    when the old one was only 'Planned:'."""
    out = dict(old)
    for k, v in new.items():
        if k == "evidence_found":
            out[k] = bool(old.get(k)) or bool(v)
        elif isinstance(v, list):
            out[k] = list(dict.fromkeys([*(old.get(k) or []), *v]))
        elif not old.get(k):
            out[k] = v
        elif k == "fix" and str(old.get(k, "")).startswith("Planned") and not str(v).startswith("Planned"):
            out[k] = v
    out["merged_from"] = int(old.get("merged_from", 1)) + 1
    return out


NOTE_TITLE = {
    "decision": lambda n: n.get("decision", ""),
    "question": lambda n: n.get("question", ""),
}


def _valid_due(v) -> str | None:
    try:
        return date.fromisoformat(v).isoformat() if v else None
    except (TypeError, ValueError):
        return None


def import_extraction(conv_id: int, data: dict, origin: str = "claude", cfg: dict | None = None,
                      force: bool = False) -> dict:
    """Save one conversation's extraction. Re-processing replaces earlier auto-made items that
    you have not edited. With office_only on, non-work conversations are marked 'skipped' and
    keep no notes or to-dos (force=True keeps them anyway). Returns counts."""
    cfg = cfg or config.load()
    conv = db.get_conversation(conv_id)
    if conv is None:
        raise KeyError(f"conversation {conv_id} not found")
    if not isinstance(data, dict) or not isinstance(data.get("conversation"), dict):
        raise ValueError("extraction must have a 'conversation' object")
    transcript = db.conversation_transcript(conv_id)
    day = conv["day"]
    c = db.conn()
    # drop earlier auto items from this conversation that were never touched by hand
    c.execute("DELETE FROM notes WHERE conversation_id=? AND origin!='manual' AND updated_at=created_at", (conv_id,))
    c.execute("DELETE FROM todos WHERE conversation_id=? AND origin!='manual' AND status='open' "
              "AND updated_at=created_at", (conv_id,))
    db.delete_topic_updates_for(conv_id)

    meta = data["conversation"]
    category = meta.get("category") if meta.get("category") in ("work", "personal", "small_talk") else "work"
    skip = cfg.get("office_only", True) and category != "work" and not force
    db.set_conversation(conv_id, title=(meta.get("title") or "")[:120], summary=meta.get("summary") or "",
                        people=meta.get("people") or [], project=meta.get("project") or "Other",
                        category=category, status="skipped" if skip else "processed", error=None,
                        processed_at=db.now())
    if skip:
        if cfg.get("delete_non_work_text"):
            c.execute("DELETE FROM segments WHERE conversation_id=?", (conv_id,))
        db.log_event("skipped", f"conversation {conv_id}: {category}")
        return {"conversation_id": conv_id, "category": category, "skipped": True,
                "notes": 0, "todos_added": 0, "todos_updated": 0}

    topic_ids = save_topics(conv_id, day, data.get("topics") or [], meta)

    def topic_for(name: str | None) -> int | None:
        if not name:
            return None
        key = _norm(name)
        if key in topic_ids:
            return topic_ids[key]
        best = max(topic_ids.items(), key=lambda kv: _similar(kv[0], key), default=None)
        return best[1] if best and _similar(best[0], key) >= 0.6 else None

    notes = 0
    for n in data.get("notes") or []:
        ntype = n.get("type")
        if ntype not in db.NOTE_TYPES:
            continue
        title = n.get("title") or NOTE_TITLE.get(ntype, lambda _: "")(n) or ntype
        # flat notes (Groq) carry every field; keep only the ones that have a value
        body = {k: v for k, v in n.items() if k not in ("type", "project", "title", "evidence_quote", "topic")
                and v not in (None, "", [])}
        tid = topic_for(n.get("topic"))
        ev = n.get("evidence_quote")
        if ntype == "info" and not any(body.get(k) for k in ("text", "what", "why")) and ev:
            body["text"] = ev   # the model left the content empty: keep at least what was heard
        body["evidence_found"] = quote_found(ev or "", transcript)
        same = similar_note(day, ntype, title, conv_id)
        if same is not None:   # the same topic again (e.g. next part of a long call): merge, don't repeat
            merged = merge_note_body(same.get("body") or {}, body)
            db.update_note(same["id"], body=merged, topic_id=same.get("topic_id") or tid)
            continue
        new = db.add_note(day, ntype, title[:200], body, n.get("project") or "Other", conv_id, ev, origin)
        if tid:
            db.set_item_topic("notes", new["id"], tid)
        notes += 1

    added = updated = 0
    open_todos = db.list_todos(status="open")
    for t in data.get("todos") or []:
        title = (t.get("title") or "").strip()
        if not title:
            continue
        ev = t.get("evidence_quote")
        conf = float(t.get("confidence") or 0.5)
        if ev and not quote_found(ev, transcript):
            conf *= 0.6   # the quote is not in the transcript: trust it less
        same = None
        if t.get("same_as"):
            try:
                same = db.get_todo(int(str(t["same_as"]).lstrip("#")))
            except ValueError:
                same = None
        if same is None:  # catch duplicates the model missed
            for o in open_todos:
                if _similar(o["title"], title) >= 0.8:
                    same = o
                    break
        if same is not None:
            changes = {}
            if t.get("status") == "done":
                changes["status"] = "done"
            if t.get("priority") in PRIORITIES and t["priority"] != "none" and same["priority"] == "none":
                changes["priority"] = t["priority"]
            if _valid_due(t.get("due")) and not same["due"]:
                changes["due"] = _valid_due(t.get("due"))
            if not same.get("topic_id") and topic_for(t.get("topic")):
                changes["topic_id"] = topic_for(t.get("topic"))
            db.update_todo(same["id"], comment=f"Mentioned again {conv['started_at'][11:16]}: {ev or title}",
                           **changes)
            updated += 1
            continue
        if t.get("status") == "done":
            continue  # finished in the same breath; nothing to track
        new = db.add_todo(day, title[:200], t.get("detail") or "",
                          t.get("kind") if t.get("kind") in TODO_KINDS else "other",
                          t.get("priority") if t.get("priority") in PRIORITIES else "none",
                          t.get("owner") or "me", _valid_due(t.get("due")), t.get("project") or "Other",
                          "open", ev, conf, conv_id, origin)
        if topic_for(t.get("topic")):
            db.set_item_topic("todos", new["id"], topic_for(t.get("topic")))
        added += 1
    db.log_event("processed", f"conversation {conv_id}: {notes} notes, {added} new to-dos, {updated} updated")
    return {"conversation_id": conv_id, "notes": notes, "todos_added": added, "todos_updated": updated}
