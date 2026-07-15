import json
import subprocess
import time
import types

import pytest

from cafecito import swarm
from cafecito.engine import Engine
from cafecito.swarm import _contain_drift, run_swarm

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
             planner_model="none", timeout=30, dry_run=False, retries=0,
             drift_wait=0.5)
    d.update(kw)
    return types.SimpleNamespace(**d)


def test_contain_drift_reserves_free_paths(repo):
    eng = Engine(str(repo))
    note = _contain_drift(eng, "swarm/a", ["extra.py"], wait_s=1, poll_s=0.05)
    assert note == ""
    assert eng._leases()["file:extra.py"]["agent"] == "swarm/a"


def test_contain_drift_waits_out_a_holder(repo):
    eng = Engine(str(repo))
    eng.reserve(keys=["file:extra.py"], agent="swarm/other", ttl=1)
    t0 = time.time()
    note = _contain_drift(eng, "swarm/a", ["extra.py"], wait_s=10, poll_s=0.1)
    assert note == ""                       # granted once the holder expired
    assert time.time() - t0 >= 0.8
    assert eng._leases()["file:extra.py"]["agent"] == "swarm/a"


def test_contain_drift_advisory_timeout_names_holder(repo):
    eng = Engine(str(repo))
    eng.reserve(keys=["file:extra.py"], agent="swarm/hog", ttl=600)
    note = _contain_drift(eng, "swarm/a", ["extra.py"], wait_s=0.3, poll_s=0.1)
    assert "drift contended" in note and "swarm/hog" in note


def test_drifting_worker_is_contained_end_to_end(repo, monkeypatch):
    # the fake worker edits its assigned path AND an undeclared one; a fleet
    # sibling holds the undeclared path briefly — the task must wait it out,
    # hold the lease itself at submit time, and still land
    eng = Engine(str(repo))
    eng.reserve(keys=["file:rogue.py"], agent="swarm/sibling", ttl=1)

    def fake_worker(prompt, model, timeout, cwd):
        import pathlib
        pathlib.Path(cwd, "mod.py").write_text("x = 1\n\ndef greet():\n"
                                               "    return 'hi'\n")
        pathlib.Path(cwd, "rogue.py").write_text("y = 2\n")
        return types.SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(swarm, "_claude_plan", lambda p, m: PLAN)
    monkeypatch.setattr(swarm, "_run_worker", fake_worker)
    rc = run_swarm(make_args(repo, drift_wait=10))
    assert rc == 0
    log = eng._log_entries()
    landed = [e for e in log if e.get("verdict") == "landed"]
    assert len(landed) == 1
    shown = subprocess.run(
        ["git", "show", "--name-only", "--format=", "cafecito/main"],
        cwd=repo, capture_output=True, text=True).stdout.split()
    assert "rogue.py" in shown
