"""Free setup: Groq client (mocked HTTP), speech engine fallback, office-only filter, bench scoring."""
import json

import httpx
import numpy as np
import pytest

from conftest import FIXTURES, SAMPLE_EXTRACTION, seed_conversation


def groq_with(handler):
    from pa.groq_api import Groq
    return Groq("gsk_test", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_groq_transcribe_and_chat_shapes():
    seen = {}

    def handler(req: httpx.Request):
        seen.setdefault("paths", []).append(req.url.path)
        assert req.headers["authorization"] == "Bearer gsk_test"
        if req.url.path.endswith("/audio/transcriptions"):
            body = req.content
            assert b'name="model"' in body and b"whisper-large-v3" in body and b"verbose_json" in body
            assert b"RIFF" in body  # a wav file is uploaded
            return httpx.Response(200, json={"language": "en", "text": "deploy to QA", "segments": [
                {"start": 0.0, "end": 1.5, "text": " deploy to QA", "avg_logprob": -0.2, "no_speech_prob": 0.01}]})
        payload = json.loads(req.content)
        assert payload["response_format"]["json_schema"]["strict"] is True
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": "yes"}'}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 3}})

    g = groq_with(handler)
    segs, lang = g.transcribe(np.zeros(16000, dtype=np.float32), "whisper-large-v3", "en", "CRM, P0")
    assert segs == [(0.0, 1.5, " deploy to QA", -0.2, 0.01)] and lang == "en"
    data, usage = g.chat_json("sys", "user", {"type": "object"})
    assert data == {"ok": "yes"} and usage["prompt_tokens"] == 10


def test_groq_rate_limit_and_strict_fallback():
    from pa.groq_api import RateLimited
    calls = []

    def handler(req):
        calls.append(json.loads(req.content)["response_format"]["type"] if req.url.path.endswith("completions") else "stt")
        if req.url.path.endswith("transcriptions"):
            return httpx.Response(429, headers={"retry-after": "120"}, text="rate limit")
        if len(calls) == 1:
            return httpx.Response(400, text='{"error":{"code":"json_validate_failed"}}')
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"a": 1}'}}]})

    g = groq_with(handler)
    with pytest.raises(RateLimited) as e:
        g.transcribe(np.zeros(1600, dtype=np.float32))
    assert e.value.retry_after == 120
    calls.clear()
    assert g.chat_json("s", "u", {"type": "object"})[0] == {"a": 1}
    assert calls == ["json_schema", "json_object"]


class FakeGroqSTT:
    def __init__(self, fail=None):
        self.fail, self.calls = fail, 0

    def transcribe(self, audio, model, lang, prompt):
        self.calls += 1
        if self.fail:
            raise self.fail
        return [(0.0, 1.0, "groq heard this", -0.1, 0.0)], "en"


class FakeLocal:
    def segments(self, audio, lang, beam, prompt):
        return [(0.0, 1.0, "local heard this", -0.3, 0.0)], "en"


def test_engine_uses_groq_then_falls_back_on_limit(fresh_db):
    from pa import config
    from pa.groq_api import RateLimited
    from pa.transcribe import Engine
    cfg = {**config.load(), "stt_engine": "groq"}
    eng = Engine(cfg, groq_client=FakeGroqSTT())
    eng._local[cfg["whisper_model"]] = FakeLocal()
    assert eng.transcribe_segments(np.zeros(16000, np.float32))[0][0][2] == "groq heard this"
    assert eng.last_engine == "groq:whisper-large-v3" and eng.cloud_ready()
    eng._groq = FakeGroqSTT(RateLimited("limit", 300))
    assert eng.transcribe_segments(np.zeros(16000, np.float32))[0][0][2] == "local heard this"
    assert "free limit" in eng.last_error and not eng.cloud_ready()
    # while paused, Groq is not even asked
    eng._groq = FakeGroqSTT()
    eng.transcribe_segments(np.zeros(16000, np.float32))
    assert eng._groq.calls == 0 and eng.last_engine.startswith("local:")


def test_engine_local_setting_never_calls_groq(fresh_db):
    from pa import config
    from pa.transcribe import Engine
    cfg = {**config.load(), "stt_engine": "local"}
    fake = FakeGroqSTT()
    eng = Engine(cfg, groq_client=fake)
    eng._local[cfg["whisper_model"]] = FakeLocal()
    eng.transcribe_segments(np.zeros(16000, np.float32))
    assert fake.calls == 0 and not eng.cloud_ready()


def test_office_only_skips_personal(fresh_db):
    from pa import extract
    db = fresh_db
    conv_id = seed_conversation(db)
    personal = {"conversation": {"title": "Family call", "summary": "Talked about dinner.", "people": [],
                                 "project": "Personal", "category": "personal"},
                "notes": SAMPLE_EXTRACTION["notes"], "todos": SAMPLE_EXTRACTION["todos"]}
    r = extract.import_extraction(conv_id, personal)
    assert r["skipped"] and r["category"] == "personal"
    assert db.get_conversation(conv_id)["status"] == "skipped"
    assert not db.list_notes() and not db.list_todos()
    assert db.list_segments(conversation_id=conv_id)          # text kept by default
    r = extract.import_extraction(conv_id, personal, force=True)  # "make notes anyway"
    assert r["notes"] == 2 and db.get_conversation(conv_id)["status"] == "processed"


def test_delete_non_work_text(fresh_db):
    from pa import config, extract
    db = fresh_db
    conv_id = seed_conversation(db)
    cfg = {**config.load(), "delete_non_work_text": True}
    extract.import_extraction(conv_id, {"conversation": {"title": "t", "summary": "s", "people": [],
                                                         "project": "Other", "category": "small_talk"},
                                        "notes": [], "todos": []}, cfg=cfg)
    assert db.list_segments(conversation_id=conv_id) == []


def test_short_talk_skipped_without_ai(fresh_db):
    from pa import extract
    db = fresh_db
    seg = db.add_segment("2026-10-15T09:00:00", "2026-10-15T09:00:03", "me", "chai piyoge?")
    db.close_idle_conversations(0)

    class Boom:
        def chat_json(self, *a, **k):
            raise AssertionError("AI must not be called for tiny talks")
    r = extract.extract_conversation(seg["conversation_id"], client=Boom())
    assert r["skipped"] and r["category"] == "small_talk"


def test_groq_notes_flat_schema_flow(fresh_db):
    from pa import config, extract
    from pa.schema import build_flat_schema
    db = fresh_db
    conv_id = seed_conversation(db)
    db.close_idle_conversations(0)
    flat_note = {k: None for k in build_flat_schema(["X"])["properties"]["notes"]["items"]["properties"]}
    flat_note.update({"type": "decision", "project": "Product A", "title": "", "decision": "Deploy DB first",
                      "modules_or_files": [], "acceptance_criteria": [], "screens_apis_db": [],
                      "evidence_quote": "run the database script first"})
    answer = {**SAMPLE_EXTRACTION, "conversation": {**SAMPLE_EXTRACTION["conversation"], "category": "work"},
              "notes": [flat_note]}

    class FakeGroqLLM:
        def __init__(self):
            self.models = []

        def chat_json(self, system, user, schema, model):
            self.models.append(model)
            assert "anyOf" not in json.dumps(schema) and "{{PROFILE}}" not in system
            return answer, {"prompt_tokens": 500, "completion_tokens": 100}
    fake = FakeGroqLLM()
    r = extract.process_pending({**config.load(), "llm_engine": "groq"}, client=fake)
    assert r["processed"] == [conv_id] and fake.models == ["openai/gpt-oss-120b"]
    note = db.list_notes()[0]
    assert note["title"] == "Deploy DB first" and set(note["body"]) == {"decision", "evidence_found"}
    assert db.usage_today()["input_tokens"] == 500


def test_groq_notes_falls_back_to_smaller_model(fresh_db):
    from pa import config, extract
    from pa.groq_api import RateLimited
    db = fresh_db
    conv_id = seed_conversation(db)

    class Limited:
        def __init__(self):
            self.models = []

        def chat_json(self, s, u, schema, model):
            self.models.append(model)
            if model.endswith("120b"):
                raise RateLimited("daily limit", 3600)
            return {**SAMPLE_EXTRACTION, "conversation": {**SAMPLE_EXTRACTION["conversation"], "category": "work"}}, {}
    fake = Limited()
    extract.extract_conversation(conv_id, {**config.load(), "llm_engine": "groq"}, client=fake)
    assert fake.models == ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"]   # qwen is the tested 2nd choice


def test_empty_backup_answer_keeps_old_notes(fresh_db):
    from pa import config, extract
    from pa.groq_api import RateLimited
    db = fresh_db
    conv_id = seed_conversation(db)
    cfg = {**config.load(), "llm_engine": "groq"}
    db.add_note("2026-10-15", "decision", "Old note", {"what": "keep me"}, conversation_id=conv_id)
    for i in range(20):    # make it a long talk
        db.add_segment(f"2026-10-15T10:03:{i:02d}", f"2026-10-15T10:03:{i:02d}", "me",
                       "we need to finish the report filter work before the QA build today")

    class EmptyBackup:
        def chat_json(self, s, u, schema, model):
            if model.endswith("120b"):
                raise RateLimited("daily limit", 3600)
            return {"conversation": {"title": "Talk", "summary": "No action items.", "people": [],
                                     "project": "Other", "category": "work"},
                    "topics": [], "notes": [], "todos": []}, {}
    try:
        extract.extract_conversation(conv_id, cfg, client=EmptyBackup())
        raise AssertionError("empty backup answer must not be saved")
    except RuntimeError as e:
        assert "limit reached for" in str(e)
    assert [n["title"] for n in db.list_notes(conversation_id=conv_id)] == ["Old note"]


def test_flat_schema_is_strict_safe():
    from pa.schema import build_flat_schema

    def walk(s):
        if isinstance(s, dict):
            if s.get("type") == "object":
                assert s["additionalProperties"] is False and set(s["required"]) == set(s["properties"])
            assert "anyOf" not in s
            for v in s.values():
                walk(v)
        elif isinstance(s, list):
            for v in s:
                walk(v)
    walk(build_flat_schema(["Product A"]))


def test_bench_scoring_and_run(fresh_db, tmp_path):
    from pa import bench
    assert bench.wer("deploy to QA today", "deploy to QA today") == 0
    assert bench.wer("deploy to QA today", "deploy QA today") == 0.25
    assert bench.keyword_recall("P0 bug in Dapper", "p0 bug in tapper", {"p0", "dapper"}) == (1, 2)
    bench.add_clip(FIXTURES / "hinglish_romanized_zira.wav", "Rahul lead conversion bug P0 hai")
    bench.add_clip(FIXTURES / "en_crm_bugfix.wav")  # no text yet = draft, not scored
    clips = bench.load_clips()
    assert [c["corrected"] for c in clips] == [False, True]

    class FakeEngine:
        def transcribe_segments(self, audio, engine=None):
            return [(0, 1, "Rahul lead conversion bug P0 hai", 0)], "en"
    r = bench.run(["local:fake"], engine_obj=FakeEngine(), log=lambda *_: None)
    assert r["clips"] == 1 and r["summary"][0]["wer"] == 0
    from pathlib import Path
    assert Path(r["html"]).exists()


def test_long_talk_is_cut_into_parts(fresh_db):
    db = fresh_db
    ids = set()
    for i in range(50):   # one line every minute for 50 minutes, never a 4-minute gap
        t = f"2026-10-16T11:{i:02d}:00"
        ids.add(db.add_segment(t, t[:-2] + "30", "call", f"line {i}", gap_min=4, max_min=20)["conversation_id"])
    assert len(ids) == 3


def test_split_existing_long_conversation(fresh_db):
    db = fresh_db
    for i in range(45):
        t = f"2026-10-16T12:{i:02d}:00"
        db.add_segment(t, t[:-2] + "30", "call", f"line {i}", gap_min=4, max_min=999)
    assert len(db.list_conversations()) == 1
    assert db.split_long_conversations(20) == 2
    convs = db.list_conversations()
    assert [c["segment_count"] for c in convs] == [20, 20, 5]
    assert convs[0]["status"] == "ended" and convs[-1]["status"] == "open"


def test_groq_waits_on_minute_limit_and_splits_parts(fresh_db):
    from pa import config, extract
    from pa.groq_api import RateLimited
    db = fresh_db
    for i in range(30):
        t = f"2026-10-16T10:{i:02d}:00"
        db.add_segment(t, t[:-2] + "20", "call", "deploy the API to QA and update the tracking sheet " * 8)
    conv = db.list_conversations()[0]
    slept, calls = [], []

    class Busy:
        def chat_json(self, s, u, schema, model):
            calls.append(model)
            if len(calls) == 1:
                raise RateLimited("tpm", 7)
            cat = "work" if len(calls) == 3 else "small_talk"
            return {"conversation": {"title": f"part{len(calls)}", "summary": "s", "people": ["Amit"],
                                     "project": "Product A", "category": cat},
                    "notes": [], "todos": [{**SAMPLE_EXTRACTION["todos"][0], "title": f"todo {len(calls)}"}]}, \
                   {"prompt_tokens": 100, "completion_tokens": 10}
    cfg = {**config.load(), "groq_chunk_tokens": 1200}
    data, used = extract.call_groq(conv, cfg, client=Busy(), sleep=slept.append)
    assert slept == [9] and calls[0] == calls[1] == "openai/gpt-oss-120b"   # waited, same model
    assert len(calls) >= 3 and used["input_tokens"] == 100 * (len(calls) - 1)
    assert data["conversation"]["category"] == "work" and data["conversation"]["title"] == "part3"
    assert len(data["todos"]) == len(calls) - 1


def test_too_large_part_is_split_in_half(fresh_db):
    from pa import config, extract
    from pa.groq_api import GroqError
    db = fresh_db
    for i in range(12):
        t = f"2026-10-16T10:{i:02d}:00"
        db.add_segment(t, t[:-2] + "20", "call", "QA deploy note " * 30)
    conv = db.list_conversations()[0]
    sizes = []

    class Strict:
        def chat_json(self, s, u, schema, model):
            size = extract.est_tokens(u)
            sizes.append(size)
            if size > 900:
                raise GroqError("Groq error 413: Request too large ... tokens per minute")
            return {"conversation": {"title": "t", "summary": "s", "people": [], "project": "Other",
                                     "category": "work"}, "notes": [], "todos": []}, {}
    data, _ = extract.call_groq(conv, {**config.load(), "groq_chunk_tokens": 1800}, client=Strict(), sleep=lambda s: None)
    assert sizes[0] > 900 and all(s <= 900 for s in sizes[-2:])
    assert data["conversation"]["category"] == "work"


def test_notes_merge_and_todo_merge(fresh_db):
    from pa import extract
    db = fresh_db
    a = seed_conversation(db, "2026-10-16")
    note = {"type": "bug_explanation", "project": "Product A", "title": "Duplicate back button on Defect Master screen",
            "problem": "Two back buttons.", "root_cause": None, "fix": "Planned: remove one", "modules_or_files": ["DefectMaster"],
            "how_to_verify": None, "evidence_quote": "the deal amount was not getting saved"}
    base = {"conversation": {"title": "t", "summary": "s", "people": [], "project": "Product A", "category": "work"}, "todos": []}
    extract.import_extraction(a, {**base, "notes": [note]})
    db.add_segment("2026-10-16T10:30:00", "2026-10-16T10:30:20", "call", "breadcrumb used as title, fixed")
    b = db.list_conversations()[-1]["id"]
    later = {**note, "title": "Duplicate back button and breadcrumb on Defect Master", "fix": "Used breadcrumb as title",
             "modules_or_files": ["Breadcrumb"], "evidence_quote": "breadcrumb used as title"}
    extract.import_extraction(b, {**base, "notes": [later]})
    notes = db.list_notes()
    assert len(notes) == 1
    assert notes[0]["body"]["fix"] == "Used breadcrumb as title" and notes[0]["body"]["modules_or_files"] == ["DefectMaster", "Breadcrumb"]

    t1 = db.add_todo("2026-10-16", "Implement screen capture shortcut with helpdesk", origin="groq")
    t2 = db.add_todo("2026-10-16", "Add helpdesk screenshot shortcut and recording toggle", origin="groq", due="2026-10-20")
    t3 = db.add_todo("2026-10-16", "Ask Puneet for report files", origin="groq")

    class FakeMerge:
        def chat_json(self, system, user, schema, model, name=None):
            assert f"{t1['id']}:" in user and name == "merge"
            return {"groups": [{"keep_id": t1["id"], "merge_ids": [t2["id"]], "reason": "same feature"}]}, {}
    assert extract.merge_duplicate_todos(client=FakeMerge()) == 1
    assert db.get_todo(t2["id"])["status"] == "dropped"
    kept = db.get_todo(t1["id"])
    assert kept["due"] == "2026-10-20" and "Merged T" in kept["log"][-1]["text"]
    assert db.get_todo(t3["id"])["status"] == "open"


def test_groq_limit_messages_are_classified():
    import httpx as hx
    from pa.groq_api import Groq, RateLimited, _wait_in_text
    assert _wait_in_text("Please try again in 1m26.4s. Need more tokens?") == 86.4
    assert _wait_in_text("Please try again in 7.5s") == 7.5
    assert _wait_in_text("Please try again in 350ms") == 0.35
    bodies = {
        "day": '{"error":{"message":"Rate limit reached for model `x` on tokens per day (TPD): Limit 200000, Used 199000, Requested 3000. Please try again in 9m12s."}}',
        "minute": '{"error":{"message":"Rate limit reached for model `x` on tokens per minute (TPM): Limit 8000, Used 7112, Requested 2000. Please try again in 9.3s."}}',
        "large": '{"error":{"message":"Request too large for model `qwen` on output tokens per minute (OTPM): Limit 1000, Requested 1800, please reduce your message size and try again."}}',
    }
    for kind, body in bodies.items():
        g = Groq("gsk_x", client=hx.Client(transport=hx.MockTransport(lambda req, b=body: hx.Response(429, text=b))))
        try:
            g.chat_json("s", "u", {"type": "object"}, "qwen/qwen3.8-27b")
        except RateLimited as e:
            if kind == "day":
                assert e.kind == "day" and e.retry_after == 552
            elif kind == "minute":
                assert e.kind == "minute" and e.retry_after == 9.3 and not e.too_large
            else:
                assert e.too_large and e.kind == "minute"


def test_qwen_answer_size_is_capped():
    import httpx as hx
    import json as js
    from pa.groq_api import Groq
    seen = {}

    def handler(req):
        seen.update(js.loads(req.content))
        return hx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})
    Groq("gsk_x", client=hx.Client(transport=hx.MockTransport(handler))).chat_json("s", "u", {"type": "object"}, "qwen/qwen3.8-27b")
    assert seen["max_completion_tokens"] == 900 and "reasoning_effort" not in seen


def test_minute_limit_waits_and_daily_pauses(fresh_db):
    from pa import config, extract
    from pa.groq_api import RateLimited
    db = fresh_db
    conv_id = seed_conversation(db)
    db.close_idle_conversations(0)
    slept, calls = [], []

    class Busy:
        def chat_json(self, s, u, schema, model):
            calls.append(model)
            if len(calls) == 1:
                raise RateLimited("tpm", 12.0, kind="minute")
            return {**SAMPLE_EXTRACTION, "conversation": {**SAMPLE_EXTRACTION["conversation"], "category": "work"}}, {}
    extract.call_groq(db.get_conversation(conv_id), config.load(), client=Busy(), sleep=slept.append)
    assert slept == [14.0] and calls[0] == calls[1]

    class AllDaily:
        def chat_json(self, s, u, schema, model):
            raise RateLimited("tpd", 900, kind="day")
    extract._paused_until = 0.0
    r = extract.process_pending({**config.load(), "llm_engine": "groq"}, client=AllDaily())
    assert r["failed"] and extract._paused_until > 0
    assert extract.process_pending({**config.load(), "llm_engine": "groq"}, client=AllDaily())["paused"] is True
    extract._paused_until = 0.0
