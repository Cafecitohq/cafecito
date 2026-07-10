import json
import subprocess
import types

import pytest

from cafecito import mcp_server
from cafecito.engine import Engine
from cafecito.mcp_server import TOOLS, handle


@pytest.fixture()
def engine(tmp_path):
    def sh(*args):
        subprocess.run(args, cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / "a.py").write_text("x = 1\n")
    sh("git", "init", "-q", "-b", "main")
    sh("git", "add", "-A")
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "i")
    return Engine(str(tmp_path))


def test_initialize_and_tool_list(engine):
    r = handle(engine, "initialize", {"protocolVersion": "2024-11-05"})
    assert r["serverInfo"]["name"] == "cafecito"
    tools = [t["name"] for t in handle(engine, "tools/list", {})["tools"]]
    assert tools == ["sync", "reserve", "submit", "swarm", "status"]


def test_sync_and_status_roundtrip(engine):
    r = handle(engine, "tools/call", {"name": "sync", "arguments": {}})
    tip = json.loads(r["content"][0]["text"])["tip"]
    r = handle(engine, "tools/call", {"name": "status", "arguments": {}})
    assert json.loads(r["content"][0]["text"])["tip"] == tip


def test_unknown_tool_is_error_result(engine):
    r = handle(engine, "tools/call", {"name": "nope", "arguments": {}})
    assert r.get("isError") is True


def test_swarm_spawns_detached(engine, monkeypatch):
    spawned = {}

    def fake_popen(cmd, **kw):
        spawned["cmd"] = cmd
        spawned["kw"] = kw
        return types.SimpleNamespace(pid=4242)

    monkeypatch.setattr(mcp_server.subprocess, "Popen", fake_popen)
    r = handle(engine, "tools/call", {"name": "swarm", "arguments": {
        "goal": "do the thing", "agents": 2}})
    out = json.loads(r["content"][0]["text"])
    assert out["started"] is True and out["pid"] == 4242
    assert "do the thing" in spawned["cmd"]
    assert "--agents" in spawned["cmd"] and "2" in spawned["cmd"]
    assert spawned["kw"]["start_new_session"] is True
