"""Voice recognition registry, rename/merge everywhere, topics with dated history, complete points."""
import numpy as np

from conftest import SAMPLE_EXTRACTION, seed_conversation


def unit(seed):
    v = np.random.default_rng(seed).normal(size=512).astype(np.float32)
    return v / np.linalg.norm(v)


def near(v, seed, eps=0.3):
    w = v + eps * unit(seed)
    return (w / np.linalg.norm(w)).astype(np.float32)


def test_identify_creates_matches_and_names(fresh_db):
    from pa import speakers
    a, b = unit(1), unit(2)
    sa, _ = speakers.identify(a, 5.0)
    sb, _ = speakers.identify(b, 5.0)
    assert sa != sb
    again, score = speakers.identify(near(a, 9), 4.0)
    assert again == sa and score > 0.55
    names = speakers.names_by_id()
    assert names[sa] in speakers.NAME_POOL and names[sb] in speakers.NAME_POOL and names[sa] != names[sb]
    # a short unclear piece never creates a new person
    short, _ = speakers.identify(unit(3), 1.5, prev_id=sa)
    assert short == sa and len(speakers.active_speakers()) == 2


def test_rename_and_merge_update_all_text(fresh_db):
    from pa import speakers
    db = fresh_db
    s1, _ = speakers.identify(unit(1), 5.0)
    s2, _ = speakers.identify(unit(2), 5.0)
    n1, n2 = speakers.get(s1)["name"], speakers.get(s2)["name"]
    t = db.add_todo("2026-10-16", f"Ask {n1} about the report", owner=n1)
    note = db.add_note("2026-10-16", "info", f"{n2} explained the API", {"text": f"{n2}: use QueryAsync"})
    speakers.rename(s1, "Puneet")
    assert db.get_todo(t["id"])["owner"] == "Puneet" and "Ask Puneet" in db.get_todo(t["id"])["title"]
    assert speakers.get(s1)["kind"] == "named"
    speakers.merge(s2, s1)   # same person heard as two voices
    assert "Puneet explained" in db.get_note(note["id"])["title"]
    assert db.get_note(note["id"])["body"]["text"].startswith("Puneet:")
    assert speakers.resolve(s2) == s1 and len(speakers.active_speakers()) == 1


def test_self_enrolment_from_microphone(fresh_db):
    from pa import speakers
    db = fresh_db
    me, _ = speakers.identify(unit(5), 5.0)
    other, _ = speakers.identify(unit(6), 5.0)
    for i in range(30):   # 30 x 5 s from the mic, mostly one voice
        sid = me if i % 6 else other
        db.add_segment(f"2026-10-16T10:{i:02d}:00", f"2026-10-16T10:{i:02d}:05", "me", f"line {i}", speaker_id=sid)
    assert speakers.maybe_enroll_me("Rahul") == me
    assert speakers.get(me)["name"] == "Rahul" and speakers.get(me)["kind"] == "me"
    assert speakers.maybe_enroll_me("Rahul") is None   # only once


def test_transcript_shows_names(fresh_db):
    from pa import speakers
    db = fresh_db
    sid, _ = speakers.identify(unit(1), 5.0)
    speakers.rename(sid, "Meera")
    seg = db.add_segment("2026-10-16T10:00:00", "2026-10-16T10:00:05", "call", "deploy today", speaker_id=sid)
    assert "[10:00:00 Meera (call)] deploy today" in db.conversation_transcript(seg["conversation_id"])
    assert db.list_segments()[0]["speaker_name"] == "Meera"


def extraction(topic_title, points, same_as=None, summary="State now."):
    return {**SAMPLE_EXTRACTION,
            "conversation": {**SAMPLE_EXTRACTION["conversation"], "category": "work"},
            "topics": [{"title": topic_title, "same_as": same_as, "summary": summary, "points": points}],
            "notes": [{**SAMPLE_EXTRACTION["notes"][0], "topic": topic_title}],
            "todos": [{**SAMPLE_EXTRACTION["todos"][0], "topic": topic_title}]}


def test_topic_history_keeps_old_sections(fresh_db):
    from pa import extract
    db = fresh_db
    c1 = seed_conversation(db, "2026-10-15")
    extract.import_extraction(c1, extraction("Deal amount bug", ["Meera: amount saved as null.", "Rahul: will fix."],
                                             summary="Bug found."))
    topic = db.list_topics()[0]
    c2 = seed_conversation(db, "2026-10-16")
    extract.import_extraction(c2, extraction("Deal amount bug", ["Rahul: fixed and on QA."], same_as=str(topic["id"]),
                                             summary="Fixed, on QA."))
    topics = db.list_topics()
    assert len(topics) == 1 and topics[0]["updates"] == 2 and topics[0]["days"] == 2
    d = db.topic_detail(topic["id"])
    assert [u["day"] for u in d["updates"]] == ["2026-10-16", "2026-10-15"]
    assert d["updates"][1]["points"] == ["Meera: amount saved as null.", "Rahul: will fix."]   # old section untouched
    assert d["summary"] == "Fixed, on QA." and d["first_day"] == "2026-10-15" and d["last_day"] == "2026-10-16"
    assert d["notes"] and d["todos"]
    conv = db.get_conversation(c2)
    assert conv["points"][0]["text"] == "Rahul: fixed and on QA."


def test_similar_title_continues_topic_and_reprocess_is_clean(fresh_db):
    from pa import extract
    db = fresh_db
    c1 = seed_conversation(db, "2026-10-15")
    extract.import_extraction(c1, extraction("Defect Master duplicate back button", ["a"]))
    c2 = seed_conversation(db, "2026-10-16")
    extract.import_extraction(c2, extraction("Defect Master duplicate back buttons", ["b"]))
    assert len(db.list_topics()) == 1
    extract.import_extraction(c2, extraction("Defect Master duplicate back buttons", ["b2"]))   # redo notes
    d = db.topic_detail(db.list_topics()[0]["id"])
    assert sorted(p for u in d["updates"] for p in u["points"]) == ["a", "b2"]


def test_merge_parts_joins_topics():
    from pa import extract
    p1 = extraction("Helpdesk screen capture", ["Vinod: add a shortcut."])
    p2 = extraction("Helpdesk screen capture", ["Vinod: keep videos two weeks."], summary="Latest.")
    m = extract.merge_parts([p1, p2])
    assert len(m["topics"]) == 1
    assert m["topics"][0]["points"] == ["Vinod: add a shortcut.", "Vinod: keep videos two weeks."]
    assert m["topics"][0]["summary"] == "Latest."


def test_topic_merge_moves_history(fresh_db):
    from pa import extract
    db = fresh_db
    c1 = seed_conversation(db, "2026-10-15")
    extract.import_extraction(c1, extraction("Report repo setup", ["x"]))
    c2 = seed_conversation(db, "2026-10-16")
    extract.import_extraction(c2, extraction("Totally different: S3 retention", ["y"]))
    a, b = sorted(t["id"] for t in db.list_topics())

    class Fake:
        def chat_json(self, s, u, schema, model, name=None):
            assert name == "merge_topics"
            return {"groups": [{"keep_id": a, "merge_ids": [b], "reason": "test"}]}, {}
    assert extract.merge_duplicate_topics(client=Fake(), days=3650) == 1
    t = db.list_topics()
    assert len(t) == 1 and t[0]["updates"] == 2


def test_api_people_topics_board(fresh_db):
    from fastapi.testclient import TestClient
    from pa import extract, speakers
    from pa.server import create_app, ui_token
    db = fresh_db
    c = TestClient(create_app(None))
    c.headers["X-PA-Token"] = ui_token()
    sid, _ = speakers.identify(unit(1), 5.0)
    db.add_segment("2026-10-16T10:00:00", "2026-10-16T10:00:05", "call", "hello there", speaker_id=sid)
    assert c.get("/api/people").json()[0]["id"] == sid
    assert c.patch(f"/api/people/{sid}", json={"name": "Vinod"}).json()["name"] == "Vinod"
    assert c.patch(f"/api/people/{sid}", json={"me": True}).json()["kind"] == "me"
    conv = seed_conversation(db, "2026-10-16")
    extract.import_extraction(conv, extraction("Board test", ["p"]))
    tid = db.list_topics()[0]["id"]
    detail = c.get(f"/api/topics/{tid}").json()
    assert detail["updates"][0]["points"] == ["p"]
    todo = detail["todos"][0]
    moved = c.patch(f"/api/todos/{todo['id']}", json={"stage": "doing"}).json()
    assert moved["stage"] == "doing" and "moved to doing" in moved["log"][-1]["text"]
    ov = c.get("/api/overview?day=2026-10-16").json()
    assert ov["topics"][0]["id"] == tid and ov["people"][0]["name"] == "Vinod"
    assert c.get("/api/search?q=Board").json()["topics"]
    assert c.get(f"/api/conversations/{conv}").json()["topics"][0]["id"] == tid
