import json
import pathlib
import subprocess
import sys
import types

import pytest

from cafecito import swarm
from cafecito.engine import Engine
from cafecito.swarm import _drifted, run_swarm

PLAN = json.dumps([
    {"id": "greet", "title": "Add greet()", "brief": "Add greet() to mod.py.",
     "paths": ["mod.py"]},
])


@pytest.fixture()
def repo(tmp_path):
    def sh(*args):
        subprocess.run(args, cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / "mod.py").write_text("x = 1\n")
    sh("git", "init", "-q", "-b", "main")
    sh("git", "add", "-A")
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "i")
    return tmp_path


def make_args(repo, **kw):
    d = dict(repo=str(repo), goal="add a greeting", agents=1, model="none",
             planner_model="none", timeout=30, dry_run=False, retries=1)
    d.update(kw)
    return types.SimpleNamespace(**d)


def test_drifted():
    assert _drifted(["a.py", "b.py"], ["a.py"]) == ["b.py"]
    assert _drifted(["a.py"], ["a.py", "t.py"]) == []


def test_dry_run_plans_but_executes_nothing(repo, monkeypatch):
    monkeypatch.setattr(swarm, "_claude_plan", lambda p, m: PLAN)
    rc = run_swarm(make_args(repo, dry_run=True))
    assert rc == 0
    eng = Engine(str(repo))
    assert eng.status()["landed"] == 0          # nothing executed
    assert json.loads((repo / ".cafecito" / "swarm.json").read_text())["done"]


def test_retry_feeds_failure_back_and_lands(repo, monkeypatch):
    monkeypatch.setattr(swarm, "_claude_plan", lambda p, m: PLAN)
    attempts = []

    def fake_worker(prompt, model, timeout, cwd):
        attempts.append(prompt)
        if len(attempts) > 1:  # first attempt produces nothing → failed
            pathlib.Path(cwd, "mod.py").write_text(
                "x = 1\n\n\ndef greet():\n    return 'hi'\n")
        return subprocess.CompletedProcess(args=[], returncode=0,
                                           stdout="", stderr="")

    monkeypatch.setattr(swarm, "_run_worker", fake_worker)
    eng = Engine(str(repo))
    eng.config["test_cmd"] = [sys.executable, "-c", "pass"]
    (eng.state_dir / "config.json").write_text(json.dumps(eng.config))

    rc = run_swarm(make_args(repo))
    assert rc == 0
    assert len(attempts) == 2
    assert "PREVIOUS ATTEMPT FAILED" in attempts[1]      # feedback delivered
    assert "no changes" in attempts[1]
    assert Engine(str(repo)).status()["landed"] == 1


def test_retries_zero_fails_fast(repo, monkeypatch):
    monkeypatch.setattr(swarm, "_claude_plan", lambda p, m: PLAN)
    calls = []

    def never_works(prompt, model, timeout, cwd):
        calls.append(1)
        return subprocess.CompletedProcess(args=[], returncode=0,
                                           stdout="", stderr="")

    monkeypatch.setattr(swarm, "_run_worker", never_works)
    rc = run_swarm(make_args(repo, retries=0))
    assert rc == 1
    assert len(calls) == 1
