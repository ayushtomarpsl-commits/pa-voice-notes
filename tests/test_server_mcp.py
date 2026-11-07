"""Web API (security + main flows) and the MCP server over real stdio."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import ROOT, SAMPLE_EXTRACTION, seed_conversation


@pytest.fixture()
def client(fresh_db):
    from pa.server import create_app, ui_token
    c = TestClient(create_app(None))
    c.headers["X-PA-Token"] = ui_token()
    return c


def test_page_has_token_and_writes_need_it(client, fresh_db):
    from pa.server import ui_token
    html = client.get("/").text
    assert ui_token() in html and "{{TOKEN}}" not in html
    bad = TestClient(client.app)
    assert bad.post("/api/todos", json={"title": "x"}).status_code == 403
    assert bad.get("/api/todos").status_code == 200
    assert bad.get("/api/todos", headers={"host": "evil.example.com"}).status_code == 403


def test_todo_and_note_flow(client):
    t = client.post("/api/todos", json={"title": "Write tests", "priority": "P1", "project": "Other"}).json()
    assert t["priority"] == "P1" and t["origin"] == "manual"
    t = client.patch(f"/api/todos/{t['id']}", json={"status": "done", "comment": "done"}).json()
    assert t["status"] == "done"
    assert client.get("/api/todos?status=today").json()[0]["id"] == t["id"]
    n = client.post("/api/notes", json={"type": "info", "title": "Port", "text": "8765"}).json()
    assert client.patch(f"/api/notes/{n['id']}", json={"pinned": 1}).json()["pinned"] == 1
    assert client.get("/api/search?q=port").json()["notes"]


def test_status_without_listener_and_export(client, fresh_db):
    from pa import extract
    conv_id = seed_conversation(fresh_db)
    extract.import_extraction(conv_id, SAMPLE_EXTRACTION)
    s = client.get("/api/status").json()
    assert s["listening"] is False
    md = client.get("/api/export/2026-10-15").text
    assert "# Work hand-off — 2026-10-15" in md
    path = client.post("/api/export/2026-10-15").json()["path"]
    assert Path(path).exists()
    assert client.post("/api/pause", json={"paused": True}).status_code == 404


def test_key_box_rejects_wrong_company(client, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    r = client.put("/api/apikey", json={"name": "groq", "key": "xai-abc123"})
    assert r.status_code == 400 and "xAI" in r.json()["detail"]
    assert client.put("/api/apikey", json={"name": "groq", "key": "gsk_abc123"}).json()["keys"]["groq"] is True


def test_settings_roundtrip(client):
    r = client.put("/api/settings", json={"config": {"whisper_model": "base", "bogus": 1}, "profile": "# me"}).json()
    assert r["config"]["whisper_model"] == "base" and "bogus" not in r["config"] and r["restart_needed"]
    s = client.get("/api/settings").json()
    assert s["profile"] == "# me"


def test_mcp_stdio_end_to_end(tmp_path):
    """Starts `python -m pa mcp` as Claude Code would, lists tools, saves an extraction."""
    anyio = pytest.importorskip("anyio")
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    # seed a DB in tmp_path for the subprocess
    env = {**os.environ, "PA_DATA_DIR": str(tmp_path)}
    seed = ("import sys; sys.path.insert(0, r'%s'); from pa import db; "
            "[db.add_segment('2026-10-15T10:0%d:00','2026-10-15T10:0%d:20','call','please update the tracking sheet after the release') for i in range(2)]; "
            "db.close_idle_conversations(0)") % (ROOT, 0, 0)
    subprocess.run([sys.executable, "-c", seed], env=env, check=True, cwd=ROOT)

    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "pa", "mcp"], env=env, cwd=str(ROOT))
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                assert {"get_day", "list_todos", "update_todo", "get_unprocessed", "save_extraction"} <= names
                un = await s.call_tool("get_unprocessed", {})
                text = "".join(c.text for c in un.content if getattr(c, "type", "") == "text")
                assert "tracking sheet" in text
                conv_id = json.loads(text)["conversation_id"] if text.strip().startswith("{") else json.loads(text)[0]["conversation_id"]
                saved = await s.call_tool("save_extraction", {"conversation_id": conv_id,
                                                              "extraction_json": json.dumps(SAMPLE_EXTRACTION)})
                assert not saved.is_error
                todos = await s.call_tool("list_todos", {})
                body = "".join(c.text for c in todos.content if getattr(c, "type", "") == "text")
                assert "Update tracking sheet after release" in body
                day = await s.call_tool("get_day", {"day": "2026-10-15"})
                assert "Bug explanations" in day.content[0].text

    anyio.run(run)
