"""Local web page + JSON API on http://127.0.0.1:<port>  (never on the network).

Safety: only Host 127.0.0.1/localhost is accepted (stops DNS-rebinding), and every change
(POST/PATCH/PUT/DELETE) needs the X-PA-Token header. The token is written into the page we
serve, so only our own page has it; other websites cannot read it.
"""
from __future__ import annotations

import secrets
import threading
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import audio, config, db, extract, handoff, keys

STATIC = Path(__file__).resolve().parent / "static"
TOKEN_PATH = config.DATA_DIR / "ui_token"


def ui_token() -> str:
    config.ensure_dirs()
    if not TOKEN_PATH.exists():
        TOKEN_PATH.write_text(secrets.token_urlsafe(24), encoding="utf-8")
    return TOKEN_PATH.read_text(encoding="utf-8").strip()


def create_app(assistant=None) -> FastAPI:
    """`assistant` is the running pa.app.Assistant, or None when only the notes UI runs."""
    app = FastAPI(title="PA", docs_url=None, redoc_url=None, openapi_url=None)
    token = ui_token()
    port = config.load()["port"]
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost", "testserver"}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host", "") not in allowed_hosts:
            return JSONResponse({"error": "bad host"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-pa-token") != token:
            return JSONResponse({"error": "missing token"}, status_code=403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def index():
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        return html.replace("{{TOKEN}}", token)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def need(x, what="not found"):
        if x is None:
            raise HTTPException(404, what)
        return x

    # ---------------------------------------------------------------- status + control
    @app.get("/api/status")
    def status():
        if assistant is None:
            return {"listening": False, "paused": True, "pause_reason": "listener not running",
                    "sources": [], "transcriber": None, "api_key_set": extract.ready(config.load()), "keys": keys.status(),
                    "usage_today": db.usage_today(), "today": db.today()}
        return assistant.status()

    @app.post("/api/pause")
    def pause(body: dict = Body(...)):
        need(assistant, "listener not running").set_paused(bool(body.get("paused")))
        return assistant.status()

    @app.post("/api/private")
    def private(body: dict = Body(...)):
        need(assistant, "listener not running").set_private(bool(body.get("on")))
        return assistant.status()

    @app.get("/api/devices")
    def devices():
        return audio.list_devices()

    # ---------------------------------------------------------------- days / transcript
    @app.get("/api/days")
    def days():
        return db.list_days()

    @app.get("/api/conversations")
    def conversations(day: str | None = None):
        return db.list_conversations(day=day or db.today())

    @app.get("/api/segments")
    def segments(day: str | None = None, conversation_id: int | None = None, after_id: int = 0, limit: int = 500):
        return db.list_segments(day, conversation_id, after_id, min(limit, 5000))

    @app.get("/api/live")
    def live(limit: int = 40):
        return db.latest_segments(min(limit, 200))

    @app.delete("/api/segments/{seg_id}")
    def delete_segment(seg_id: int):
        db.delete_segment(seg_id)
        return {"ok": True}

    progress: dict = {"running": False, "last": None}

    @app.post("/api/process")
    def process(body: dict = Body(default={})):
        """Starts note-making in the background (free-plan waits can take minutes)."""
        day = body.get("day")
        if progress["running"]:
            return {"started": False, "busy": True}

        def work():
            progress["running"] = True
            try:
                if assistant is not None:
                    progress["last"] = assistant.process_now(day)
                else:
                    db.close_idle_conversations(0)
                    progress["last"] = extract.process_pending(config.load(), day)
            except Exception as e:  # keep the error for the page
                progress["last"] = {"processed": [], "failed": [{"id": None, "error": str(e)}]}
            finally:
                progress["running"] = False
        if body.get("wait"):
            work()
            return {"started": True, "busy": False, "result": progress["last"]}
        threading.Thread(target=work, daemon=True, name="process-now").start()
        return {"started": True, "busy": False}

    @app.get("/api/process")
    def process_state():
        return progress

    @app.post("/api/conversations/{conv_id}/process")
    def process_one(conv_id: int, force: bool = False):
        need(db.get_conversation(conv_id))
        try:
            return extract.extract_conversation(conv_id, force=force)
        except RuntimeError as e:
            raise HTTPException(400, str(e))

    @app.patch("/api/conversations/{conv_id}")
    def patch_conversation(conv_id: int, body: dict = Body(...)):
        need(db.get_conversation(conv_id))
        db.set_conversation(conv_id, **{k: v for k, v in body.items() if k in ("title", "project", "status")})
        return db.get_conversation(conv_id)

    # ---------------------------------------------------------------- notes
    @app.get("/api/notes")
    def notes(day: str | None = None, type: str | None = None, project: str | None = None,
              conversation_id: int | None = None):
        return db.list_notes(day, type, project, conversation_id)

    @app.post("/api/notes")
    def add_note(body: dict = Body(...)):
        try:
            return db.add_note(body.get("day") or db.today(), body.get("type", "info"), body.get("title", ""),
                               body.get("body") or {"text": body.get("text", "")}, body.get("project", "Other"),
                               body.get("conversation_id"), None, "manual")
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.patch("/api/notes/{note_id}")
    def patch_note(note_id: int, body: dict = Body(...)):
        need(db.get_note(note_id))
        return db.update_note(note_id, **body)

    @app.delete("/api/notes/{note_id}")
    def delete_note(note_id: int):
        db.delete_note(note_id)
        return {"ok": True}

    # ---------------------------------------------------------------- todos
    @app.get("/api/todos")
    def todos(status: str = "today", project: str | None = None, priority: str | None = None,
              day: str | None = None):
        return db.list_todos(status, project, priority, day)

    @app.post("/api/todos")
    def add_todo(body: dict = Body(...)):
        if not (body.get("title") or "").strip():
            raise HTTPException(400, "title is required")
        return db.add_todo(db.today(), body["title"], body.get("detail", ""), body.get("kind", "other"),
                           body.get("priority", "none"), body.get("owner", "me"), body.get("due") or None,
                           body.get("project", "Other"), origin="manual")

    @app.patch("/api/todos/{todo_id}")
    def patch_todo(todo_id: int, body: dict = Body(...)):
        need(db.get_todo(todo_id))
        comment = body.pop("comment", None)
        return db.update_todo(todo_id, comment=comment, **body)

    @app.delete("/api/todos/{todo_id}")
    def delete_todo(todo_id: int):
        db.delete_todo(todo_id)
        return {"ok": True}

    # ---------------------------------------------------------------- topics (cards + dated history)
    @app.get("/api/topics")
    def topics(status: str = "active", project: str | None = None, q: str | None = None):
        return db.list_topics(status, project, q)

    @app.get("/api/topics/{topic_id}")
    def topic(topic_id: int):
        return need(db.topic_detail(topic_id))

    @app.patch("/api/topics/{topic_id}")
    def patch_topic(topic_id: int, body: dict = Body(...)):
        need(db.get_topic_row(topic_id))
        return db.update_topic(topic_id, **body)

    @app.post("/api/topics/{topic_id}/merge")
    def merge_topic(topic_id: int, body: dict = Body(...)):
        into = int(body.get("into") or 0)
        need(db.get_topic_row(topic_id)); need(db.get_topic_row(into), "target topic not found")
        if into == topic_id:
            raise HTTPException(400, "cannot merge a topic into itself")
        return db.merge_topics(topic_id, into)

    # ---------------------------------------------------------------- people (voice recognition)
    @app.get("/api/people")
    def people():
        from . import speakers
        return speakers.people()

    @app.patch("/api/people/{speaker_id}")
    def patch_person(speaker_id: int, body: dict = Body(...)):
        from . import speakers
        cur = need(speakers.get(speaker_id))
        try:
            return speakers.rename(speaker_id, body.get("name") or cur["name"],
                                   kind="me" if body.get("me") else ("named" if body.get("name") else cur["kind"]))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/people/{speaker_id}/merge")
    def merge_person(speaker_id: int, body: dict = Body(...)):
        from . import speakers
        try:
            return speakers.merge(speaker_id, int(body.get("into") or 0))
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e))

    # ---------------------------------------------------------------- overview + conversation detail
    @app.get("/api/overview")
    def overview(day: str | None = None):
        d = day or db.today()
        c = db.conn()
        secs = c.execute("SELECT coalesce(sum((julianday(ended_at)-julianday(started_at))*86400),0) FROM segments "
                         "WHERE day=?", (d,)).fetchone()[0]
        people_today = [dict(r) for r in c.execute(
            "SELECT sp.id, sp.name, sp.kind, count(*) AS lines, "
            "sum((julianday(s.ended_at)-julianday(s.started_at))*86400) AS seconds "
            "FROM segments s JOIN speakers sp ON sp.id=s.speaker_id WHERE s.day=? GROUP BY sp.id "
            "ORDER BY seconds DESC LIMIT 12", (d,))]
        touched = [dict(r) for r in c.execute(
            "SELECT DISTINCT t.id, t.title, t.project, t.summary, t.first_day, t.last_day, "
            "(SELECT count(*) FROM topic_updates u2 WHERE u2.topic_id=t.id) AS updates, "
            "(SELECT count(*) FROM todos d2 WHERE d2.topic_id=t.id AND d2.status='open') AS open_todos "
            "FROM topic_updates u JOIN topics t ON t.id=u.topic_id WHERE u.day=? ORDER BY t.updated_at DESC", (d,))]
        open_todos = db.list_todos(status="open")
        return {"day": d, "speech_minutes": round(secs / 60, 1),
                "conversations": len(db.list_conversations(day=d)),
                "people": people_today, "topics": touched,
                "open_todos": len(open_todos),
                "due_today": len([t for t in open_todos if t["due"] and t["due"] <= d]),
                "urgent": len([t for t in open_todos if t["priority"].startswith("P0")]),
                "done_today": len([t for t in db.list_todos(status="done") if (t["done_at"] or "")[:10] == d])}

    @app.get("/api/conversations/{conv_id}")
    def conversation(conv_id: int):
        conv = need(db.get_conversation(conv_id))
        conv["segments"] = db.list_segments(conversation_id=conv_id, limit=5000)
        conv["notes"] = db.list_notes(conversation_id=conv_id)
        conv["todos"] = [t for t in db.list_todos(status="all") if t["conversation_id"] == conv_id]
        conv["topics"] = [dict(r) for r in db.conn().execute(
            "SELECT t.id, t.title, u.summary FROM topic_updates u JOIN topics t ON t.id=u.topic_id "
            "WHERE u.conversation_id=?", (conv_id,))]
        return conv

    # ---------------------------------------------------------------- search + export
    @app.get("/api/search")
    def search(q: str):
        return db.search(q)

    @app.get("/api/export/{day}", response_class=PlainTextResponse)
    def export_md(day: str):
        return handoff.build(day)

    @app.post("/api/export/{day}")
    def export_write(day: str):
        return {"path": handoff.write(day)}

    # ---------------------------------------------------------------- settings
    @app.get("/api/settings")
    def get_settings():
        cfg = config.load()
        return {"config": cfg, "defaults": config.DEFAULTS, "api_key_set": extract.ready(cfg), "keys": keys.status(),
                "profile": extract.load_profile(), "paths": {"data": str(config.DATA_DIR),
                                                            "exports": str(config.EXPORT_DIR)}}

    @app.put("/api/settings")
    def put_settings(body: dict = Body(...)):
        if "profile" in body:
            extract.PROFILE_PATH.write_text(body.pop("profile"), encoding="utf-8")
        cfg = config.save(body.get("config", {}))
        return {"config": cfg, "restart_needed": any(k in body.get("config", {}) for k in
                                                      ("mic_device", "capture_calls", "whisper_model",
                                                       "cpu_threads", "hotkey", "port"))}

    @app.put("/api/apikey")
    def put_key(body: dict = Body(...)):
        """body: {name: 'groq' | 'anthropic', key: '...'}; empty key removes it."""
        name = body.get("name", "anthropic")
        if name not in keys.ENV:
            raise HTTPException(400, "unknown key name")
        value = (body.get("key") or "").strip()
        if value and name == "groq" and not value.startswith("gsk_"):
            hint = " This looks like an xAI (Grok) key — Groq is a different company." if value.startswith("xai-") else ""
            raise HTTPException(400, "A Groq key starts with gsk_ (from console.groq.com)." + hint)
        if value and name == "anthropic" and not value.startswith("sk-ant-"):
            raise HTTPException(400, "A Claude API key starts with sk-ant-.")
        keys.set(name, body.get("key"))
        return {"keys": keys.status()}

    @app.post("/api/check/groq")
    def check_groq():
        """Live test of the free Groq key: tiny transcription + tiny JSON chat."""
        from .groq_api import Groq
        import numpy as np
        key = keys.get("groq")
        if not key:
            raise HTTPException(400, "No Groq key saved")
        g = Groq(key, timeout=60)
        out = {}
        try:
            t = np.linspace(0, 1, 16000, dtype=np.float32)
            g.transcribe(0.01 * np.sin(2 * np.pi * 220 * t), config.load().get("groq_stt_model", "whisper-large-v3"))
            out["speech_to_text"] = "ok"
        except Exception as e:
            out["speech_to_text"] = f"failed: {e}"
        try:
            data, _ = g.chat_json("Reply in JSON.", "Say ok.", {"type": "object", "properties": {"ok": {"type": "string"}},
                                  "required": ["ok"], "additionalProperties": False}, config.load().get("groq_llm_model"))
            out["notes_ai"] = "ok" if "ok" in data else f"odd answer: {data}"
        except Exception as e:
            out["notes_ai"] = f"failed: {e}"
        return out

    return app
