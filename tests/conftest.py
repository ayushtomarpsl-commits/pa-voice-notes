import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURES = ROOT / "tests" / "fixtures"

# every test run gets its own data folder, never the real one
os.environ["PA_DATA_DIR"] = tempfile.mkdtemp(prefix="pa-test-")


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """A new empty database per test."""
    from pa import config, db
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "pa.sqlite")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(config, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(config, "AUDIO_DIR", tmp_path / "audio")
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "exports")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "SECRET_PATH", tmp_path / "api_key.dpapi")
    from pa import extract
    monkeypatch.setattr(extract, "PROFILE_PATH", tmp_path / "profile.md")
    db.close_all()
    yield db
    db.close_all()


SAMPLE_EXTRACTION = {
    "conversation": {"title": "Deal amount bug fix", "summary": "How the deal amount bug was fixed.",
                     "people": ["call"], "project": "Product A"},
    "notes": [
        {"type": "bug_explanation", "project": "Product A", "title": "Deal amount not saved on stage change",
         "problem": "Amount not saved when stage changes.", "root_cause": "Angular sent a string; API wanted decimal.",
         "fix": "Convert to number; validate in API; SQL repair script.", "modules_or_files": ["Angular deal form", "API controller"],
         "how_to_verify": "Change stage on QA and check amount.", "evidence_quote": "the deal amount was not getting saved"},
        {"type": "decision", "project": "Product A", "decision": "Deploy DB, then API, then MCP, then Angular.",
         "by_whom": None, "reason": None, "evidence_quote": "run the database script first"},
    ],
    "todos": [
        {"title": "Update tracking sheet after release", "detail": "", "kind": "follow_up", "priority": "P0",
         "owner": "me", "due": None, "project": "Product A", "status": "open", "same_as": None,
         "evidence_quote": "please update the tracking sheet after the release", "confidence": 0.9},
        {"title": "Check lead conversion screen in ERP", "detail": "Same problem as deal amount.", "kind": "bug_fix",
         "priority": "none", "owner": "me", "due": "2026-10-16", "project": "ERP PPC/MRP", "status": "open",
         "same_as": None, "evidence_quote": "this text was never said", "confidence": 0.8},
    ],
}


def seed_conversation(db, day="2026-10-15"):
    lines = ["OK Rahul, the deal amount was not getting saved when the stage changed.",
             "For deployment, run the database script first, then deploy the API.",
             "Priya marked this as P0, so please update the tracking sheet after the release."]
    seg = None
    for i, t in enumerate(lines):
        seg = db.add_segment(f"{day}T10:0{i}:00", f"{day}T10:0{i}:20", "call", t)
    return seg["conversation_id"]
