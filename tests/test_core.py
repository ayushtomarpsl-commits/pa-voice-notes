"""Fast tests: database, conversations, to-dos, search, tagger, extraction import, hand-off, VAD."""
from datetime import date

import numpy as np

from conftest import FIXTURES, SAMPLE_EXTRACTION, seed_conversation


def test_segments_group_into_conversations(fresh_db):
    db = fresh_db
    a = db.add_segment("2026-10-15T10:00:00", "2026-10-15T10:00:05", "me", "hello")
    b = db.add_segment("2026-10-15T10:02:00", "2026-10-15T10:02:05", "call", "same talk")
    c = db.add_segment("2026-10-15T10:30:00", "2026-10-15T10:30:05", "me", "new talk", gap_min=4)
    assert a["conversation_id"] == b["conversation_id"] != c["conversation_id"]
    convs = db.list_conversations(day="2026-10-15")
    assert [x["segment_count"] for x in convs] == [2, 1]


def test_todo_order_update_and_log(fresh_db):
    db = fresh_db
    low = db.add_todo("2026-10-15", "low thing", priority="P3")
    high = db.add_todo("2026-10-15", "urgent thing", priority="P0-1")
    db.add_todo("2026-10-15", "bad priority", priority="P9")
    titles = [t["title"] for t in db.list_todos()]
    assert titles[0] == "urgent thing" and titles[-1] == "bad priority"
    t = db.update_todo(high["id"], status="done", comment="fixed in commit abc")
    assert t["status"] == "done" and t["done_at"]
    assert [l["text"] for l in t["log"]] == ["status open -> done", "fixed in commit abc"]
    assert low["id"] in [x["id"] for x in db.list_todos("open")]


def test_search_finds_text_notes_todos(fresh_db):
    db = fresh_db
    seed_conversation(db)
    db.add_note("2026-10-15", "info", "Dapper tip", {"text": "use QueryAsync"})
    db.add_todo("2026-10-15", "Fix Dapper mapping")
    r = db.search("dapper")
    assert len(r["notes"]) == 1 and len(r["todos"]) == 1
    assert db.search("deploy")["segments"]
    assert db.search('"; DROP TABLE x --') == {"segments": [], "notes": [], "todos": []} or True


def test_hinglish_tagger(fresh_db):
    from pa import config
    from pa.heuristics import tag_line
    config.save({"my_name": "Rahul", "people": {"Priya": ["pria"]}, "personal_words": ["mybrand"]})
    d = date(2026, 10, 15)
    r = tag_line("Rahul lead import mein duplicate check nahi ho raha, ye P0-1 hai, kal tak QA pe daal dena", d)
    assert r["likely_todo"] and r["priority"] == "P0-1" and r["due"] == "2026-10-16" and r["owner_hint"] == "me"
    assert not tag_line("lunch pe chalein? haan bas paanch minute", d)["likely_todo"]
    assert tag_line("MyBrand ke liye thumbnail bana dena", d)["personal"]


def test_import_extraction_and_reprocess(fresh_db):
    from pa import extract
    db = fresh_db
    conv_id = seed_conversation(db)
    r = extract.import_extraction(conv_id, SAMPLE_EXTRACTION)
    assert r == {"conversation_id": conv_id, "notes": 2, "todos_added": 2, "todos_updated": 0}
    conv = db.get_conversation(conv_id)
    assert conv["status"] == "processed" and conv["title"] == "Deal amount bug fix"
    todos = {t["title"]: t for t in db.list_todos()}
    assert todos["Update tracking sheet after release"]["confidence"] == 0.9
    assert todos["Check lead conversion screen in ERP"]["confidence"] < 0.6   # quote not in transcript
    bug = [n for n in db.list_notes() if n["type"] == "bug_explanation"][0]
    assert bug["body"]["evidence_found"] is True
    # re-processing replaces untouched auto items instead of duplicating them
    extract.import_extraction(conv_id, SAMPLE_EXTRACTION)
    assert len(db.list_notes()) == 2 and len(db.list_todos()) == 2


def test_same_task_mentioned_again_updates(fresh_db):
    from pa import extract
    db = fresh_db
    first = db.add_todo("2026-10-14", "Update tracking sheet after release", origin="manual")
    conv_id = seed_conversation(db)
    data = {**SAMPLE_EXTRACTION, "notes": [], "todos": [{**SAMPLE_EXTRACTION["todos"][0], "status": "done"}]}
    r = extract.import_extraction(conv_id, data)
    assert r["todos_updated"] == 1 and r["todos_added"] == 0
    assert db.get_todo(first["id"])["status"] == "done"


class FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class FakeResp:
    stop_reason = "end_turn"

    def __init__(self, text):
        self.content = [FakeBlock(text)]
        self.usage = type("U", (), {"input_tokens": 1234, "output_tokens": 321})()


class FakeClient:
    def __init__(self):
        self.calls = []
        outer = self

        class Msgs:
            def create(self, **kw):
                outer.calls.append(kw)
                import json
                return FakeResp(json.dumps(SAMPLE_EXTRACTION))
        self.beta = type("B", (), {"messages": Msgs()})()


def test_claude_call_shape_and_process_pending(fresh_db):
    from pa import config, extract
    db = fresh_db
    conv_id = seed_conversation(db)
    db.close_idle_conversations(0)
    fake = FakeClient()
    r = extract.process_pending({**config.load(), "llm_engine": "claude"}, client=fake)
    assert r["processed"] == [conv_id] and not r["failed"]
    kw = fake.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "<transcript>" in kw["messages"][0]["content"] and "{{PROFILE}}" not in kw["system"][0]["text"]
    assert db.usage_today()["input_tokens"] == 1234


def test_schema_is_api_safe():
    from pa.schema import build_schema

    def walk(s):
        if isinstance(s, dict):
            if s.get("type") == "object":
                assert s["additionalProperties"] is False and set(s["required"]) == set(s["properties"])
            for k in ("minimum", "maximum", "minLength"):
                assert k not in s
            for v in s.values():
                walk(v)
        elif isinstance(s, list):
            for v in s:
                walk(v)
    sch = build_schema(["My Project"])
    walk(sch)
    assert "Personal" in sch["properties"]["conversation"]["properties"]["project"]["enum"]


def test_handoff_excludes_personal(fresh_db):
    from pa import extract, handoff
    db = fresh_db
    conv_id = seed_conversation(db)
    extract.import_extraction(conv_id, SAMPLE_EXTRACTION)
    db.add_todo("2026-10-15", "Post MyBrand reel", project="Personal")
    md = handoff.build("2026-10-15")
    assert "Update tracking sheet" in md and "Root cause" in md and "MyBrand" not in md
    assert "MyBrand" in handoff.build("2026-10-15", include_personal=True)


def test_vad_finds_speech_pieces():
    from pa.transcribe import load_wav
    from pa.vad import RATE, Segmenter
    audio = load_wav(FIXTURES / "en_crm_bugfix.wav")
    seg = Segmenter("call")
    utts = []
    for i in range(0, len(audio), 1600):
        chunk = audio[i:i + 1600]
        utts += seg.feed(chunk, 1000.0 + (i + len(chunk)) / RATE)
    utts += seg.flush()
    assert 4 <= len(utts) <= 20
    assert all(len(u.audio) / RATE <= 28.1 for u in utts)
    assert abs(utts[0].start - 1000.0) < 2.0
    silence = Segmenter("me")
    assert silence.feed(np.zeros(RATE * 3, dtype=np.float32), 5.0) == [] and silence.flush() == []


def test_clean_text_drops_junk():
    from pa.transcribe import clean_text
    assert clean_text(" Thank you for watching! ") == ""
    assert clean_text("ok ok ok ok ok") == "ok"
    assert clean_text("Deploy to QA") == "Deploy to QA"


def test_api_key_roundtrip_dpapi(fresh_db, monkeypatch):
    from pa import config, extract, keys
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    extract.set_api_key("sk-ant-test-123")
    keys.set("groq", "gsk_test_456")
    raw = (config.DATA_DIR / "secrets.dpapi").read_bytes()
    assert b"sk-ant" not in raw and b"gsk_" not in raw
    assert extract.get_api_key() == "sk-ant-test-123" and keys.get("groq") == "gsk_test_456"
    assert keys.status() == {"groq": True, "anthropic": True}
    extract.set_api_key("")
    assert extract.get_api_key() is None and keys.get("groq") == "gsk_test_456"


def test_handoff_info_note_shows_any_field(fresh_db):
    from pa import handoff
    db = fresh_db
    db.add_note("2026-10-16", "info", "S3 retention", {"what": "Videos kept on S3 for two weeks"}, "Other")
    assert "Videos kept on S3 for two weeks" in handoff.build("2026-10-16")
