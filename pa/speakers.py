"""Who is talking? Voice recognition with a local model (3D-Speaker CAM++, ONNX, via sherpa-onnx).

Every piece of speech gets a voice fingerprint (512 numbers). It is compared with the people PA
already knows (cosine similarity). A match above `speaker_threshold` gets that person's name; a new
voice gets a random, friendly placeholder name (kind 'auto') that you rename in the People page.
Your own voice: once PA has heard enough from your microphone, the voice that dominates it is named
after you (config 'my_name') with kind 'me'. Fingerprints stay in data/pa.sqlite on this laptop.

Model choice (16-Oct, speech-only clips): the multilingual "advanced" CAM++ separated real Hindi
speakers 100% (same person >= 0.64, different <= 0.44) and 2-second pieces 99%; the English-only CAM++
scored 83-96% and ResNet34 65-88%. ~0.08-0.16 s per piece on this laptop.
"""
from __future__ import annotations

import logging
import random
import re
import threading
from pathlib import Path

import numpy as np

from . import config, db

log = logging.getLogger("pa.voice")
MODEL = config.ROOT / "models" / "speaker" / "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
MIN_SECONDS = 1.2          # shorter speech gives unreliable fingerprints: it keeps the previous speaker
UPDATE_MARGIN = 0.08       # only very sure matches refine the stored voice

# Placeholder names (common Indian first names) for voices nobody has named yet.
NAME_POOL = [
    "Kabir", "Meera", "Ishaan", "Tara", "Vihaan", "Anaya", "Advait", "Kiara", "Reyansh", "Saanvi",
    "Arjun", "Myra", "Dhruv", "Aditi", "Rohan", "Ira", "Nikhil", "Zoya", "Aarav", "Riya",
    "Kunal", "Nisha", "Pranav", "Kavya", "Siddharth", "Diya", "Varun", "Pooja", "Yash", "Sneha",
    "Aryan", "Ishita", "Manav", "Tanvi", "Rahul", "Avni", "Harsh", "Mitali", "Om", "Simran",
]

_lock = threading.RLock()


def _blob(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float32).tobytes()


def _vec(b: bytes | None) -> np.ndarray | None:
    return None if not b else np.frombuffer(b, dtype=np.float32).copy()


class VoiceModel:
    """Thin wrapper around the sherpa-onnx speaker embedding extractor."""

    def __init__(self, model: Path = MODEL, threads: int = 2):
        import sherpa_onnx
        if not Path(model).exists():
            raise FileNotFoundError(f"voice model missing: {model} (run scripts\\setup.ps1)")
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model), num_threads=threads)
        self.ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)

    def embed(self, audio: np.ndarray) -> np.ndarray | None:
        if len(audio) < MIN_SECONDS * 16000:
            return None
        s = self.ex.create_stream()
        s.accept_waveform(16000, np.ascontiguousarray(audio, dtype=np.float32))
        s.input_finished()
        v = np.asarray(self.ex.compute(s), dtype=np.float32)
        n = float(np.linalg.norm(v))
        return v / n if n > 0 else None


# ------------------------------------------------------------------ registry
def active_speakers() -> list[dict]:
    rows = db.conn().execute("SELECT * FROM speakers WHERE merged_into IS NULL ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["centroid"] = _vec(d["centroid"])
        out.append(d)
    return out


def get(speaker_id: int) -> dict | None:
    r = db.conn().execute("SELECT * FROM speakers WHERE id=?", (speaker_id,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    d.pop("centroid", None)
    return d


def resolve(speaker_id: int | None) -> int | None:
    """Follow merges to the speaker that is still in use."""
    seen = set()
    while speaker_id is not None and speaker_id not in seen:
        seen.add(speaker_id)
        r = db.conn().execute("SELECT merged_into FROM speakers WHERE id=?", (speaker_id,)).fetchone()
        if r is None or r["merged_into"] is None:
            return speaker_id
        speaker_id = r["merged_into"]
    return speaker_id


def _new_name() -> str:
    used = {r["name"] for r in db.conn().execute("SELECT name FROM speakers")}
    free = [n for n in NAME_POOL if n not in used]
    if free:
        return random.choice(free)
    i = 2
    while True:
        for n in NAME_POOL:
            if f"{n} {i}" not in used:
                return f"{n} {i}"
        i += 1


def create(emb: np.ndarray | None, seconds: float = 0.0, name: str | None = None, kind: str = "auto") -> int:
    return db.conn().execute(
        "INSERT INTO speakers(name, kind, centroid, samples, seconds, created_at, last_heard) VALUES (?,?,?,?,?,?,?)",
        (name or _new_name(), kind, _blob(emb) if emb is not None else None, 1 if emb is not None else 0,
         seconds, db.now(), db.now())).lastrowid


def identify(emb: np.ndarray, seconds: float, threshold: float = 0.55, prev_id: int | None = None,
             min_new_seconds: float = 2.5, stay_bonus: float = 0.08) -> tuple[int | None, float]:
    """Best matching speaker. Returns (id, score).
    - The person who spoke just before (prev_id) gets a small bonus, so names do not flicker.
    - A new person is only created from >= min_new_seconds of speech; a shorter unclear piece goes to
      the closest known voice (or stays unknown when nobody is known yet)."""
    with _lock:
        best, best_score = None, -1.0
        for s in active_speakers():
            if s["centroid"] is None:
                continue
            score = float(emb @ s["centroid"]) + (stay_bonus if s["id"] == prev_id else 0.0)
            if score > best_score:
                best, best_score = s, score
        if best is None or best_score < threshold:
            if seconds < min_new_seconds:
                if best is not None and best_score >= threshold - 0.2:
                    return best["id"], best_score
                return (prev_id, best_score) if prev_id else (None, best_score)
            sid = create(emb, seconds)
            log.info("new voice -> %s (best match %.2f)", get(sid)["name"], best_score)
            return sid, 1.0
        c = db.conn()
        if best_score >= threshold + UPDATE_MARGIN:
            w = min(best["samples"], 50)          # running mean, but keep adapting slowly
            cen = best["centroid"] * w + emb
            cen = cen / (np.linalg.norm(cen) or 1.0)
            c.execute("UPDATE speakers SET centroid=?, samples=samples+1, seconds=seconds+?, last_heard=? WHERE id=?",
                      (_blob(cen), seconds, db.now(), best["id"]))
        else:
            c.execute("UPDATE speakers SET seconds=seconds+?, last_heard=? WHERE id=?", (seconds, db.now(), best["id"]))
        return best["id"], best_score


def maybe_enroll_me(my_name: str, min_mic_seconds: float = 120) -> int | None:
    """When nobody is marked as you yet: the voice that dominates your microphone becomes you."""
    c = db.conn()
    if c.execute("SELECT 1 FROM speakers WHERE kind='me' AND merged_into IS NULL").fetchone():
        return None
    rows = c.execute(
        "SELECT speaker_id, sum((julianday(ended_at)-julianday(started_at))*86400) secs FROM segments "
        "WHERE source='me' AND speaker_id IS NOT NULL GROUP BY speaker_id ORDER BY secs DESC").fetchall()
    total = sum(r["secs"] or 0 for r in rows)
    if not rows or total < min_mic_seconds or (rows[0]["secs"] or 0) < 0.6 * total:
        return None
    sid = resolve(rows[0]["speaker_id"])
    old = get(sid)
    if old and old["kind"] == "auto":
        rename(sid, my_name, kind="me")
        log.info("your voice found: %s is now %s", old["name"], my_name)
        return sid
    return None


# ------------------------------------------------------------------ rename / merge
TEXT_TARGETS = [
    ("todos", ["title", "detail", "owner"]),
    ("notes", ["title", "body"]),
    ("conversations", ["title", "summary", "people", "points"]),
    ("topics", ["title", "summary"]),
    ("topic_updates", ["summary", "points", "people"]),
]


def _replace_name_everywhere(old: str, new: str) -> int:
    """Auto names are unique placeholders, so replacing the whole word is safe."""
    if not old or old == new:
        return 0
    rx = re.compile(rf"(?<![\w]){re.escape(old)}(?![\w])")
    changed = 0
    with db.tx() as c:
        for table, cols in TEXT_TARGETS:
            for col in cols:
                rows = c.execute(f"SELECT id, {col} AS v FROM {table} WHERE {col} LIKE ?", (f"%{old}%",)).fetchall()
                for r in rows:
                    if r["v"] and rx.search(r["v"]):
                        c.execute(f"UPDATE {table} SET {col}=? WHERE id=?", (rx.sub(new, r["v"]), r["id"]))
                        changed += 1
    return changed


def rename(speaker_id: int, name: str, kind: str = "named") -> dict:
    name = " ".join((name or "").split())[:60]
    if not name:
        raise ValueError("name is empty")
    old = get(speaker_id)
    if old is None:
        raise KeyError(speaker_id)
    if kind == "me":
        db.conn().execute("UPDATE speakers SET kind='named' WHERE kind='me' AND id<>?", (speaker_id,))
    db.conn().execute("UPDATE speakers SET name=?, kind=? WHERE id=?", (name, kind, speaker_id))
    _replace_name_everywhere(old["name"], name)
    db.log_event("speaker_rename", f"{old['name']} -> {name}")
    return get(speaker_id)


def merge(source_id: int, target_id: int) -> dict:
    """Same person heard as two voices: move everything from source to target."""
    if source_id == target_id:
        raise ValueError("cannot merge a person into themselves")
    src, dst = db.conn().execute("SELECT * FROM speakers WHERE id=?", (source_id,)).fetchone(), \
        db.conn().execute("SELECT * FROM speakers WHERE id=?", (target_id,)).fetchone()
    if src is None or dst is None:
        raise KeyError("speaker not found")
    a, b = _vec(src["centroid"]), _vec(dst["centroid"])
    cen = b if a is None else a if b is None else (a * max(src["samples"], 1) + b * max(dst["samples"], 1))
    if cen is not None:
        cen = cen / (np.linalg.norm(cen) or 1.0)
    with db.tx() as c:
        c.execute("UPDATE segments SET speaker_id=? WHERE speaker_id=?", (target_id, source_id))
        c.execute("UPDATE speakers SET centroid=?, samples=samples+?, seconds=seconds+?, last_heard=max(last_heard, ?) "
                  "WHERE id=?", (_blob(cen) if cen is not None else None, src["samples"], src["seconds"],
                                 src["last_heard"] or "", target_id))
        c.execute("UPDATE speakers SET merged_into=? WHERE id=?", (target_id, source_id))
    _replace_name_everywhere(src["name"], dst["name"])
    db.log_event("speaker_merge", f"{src['name']} -> {dst['name']}")
    return get(target_id)


def people(limit_lines: int = 3) -> list[dict]:
    """Everyone heard, most talk time first, with a few recent lines each (for the People page)."""
    c = db.conn()
    out = []
    for s in c.execute("SELECT id, name, kind, samples, seconds, created_at, last_heard FROM speakers "
                       "WHERE merged_into IS NULL ORDER BY (kind='me') DESC, seconds DESC"):
        d = dict(s)
        d["lines"] = [dict(r) for r in c.execute(
            "SELECT id, started_at, source, text FROM segments WHERE speaker_id=? ORDER BY started_at DESC LIMIT ?",
            (d["id"], limit_lines))]
        d["conversations"] = c.execute("SELECT count(DISTINCT conversation_id) FROM segments WHERE speaker_id=?",
                                       (d["id"],)).fetchone()[0]
        out.append(d)
    return out


def names_by_id() -> dict[int, str]:
    return {r["id"]: r["name"] for r in db.conn().execute("SELECT id, name FROM speakers")}
