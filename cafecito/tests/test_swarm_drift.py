import json
import subprocess
import time
import types

import pytest

from cafecito import swarm
from cafecito.engine import Engine, key_path, keys_overlap
from cafecito.swarm import _contain_drift, _drift_keys, run_swarm

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
    note = _contain_drift(eng, "swarm/a", ["file:extra.py"], wait_s=1, poll_s=0.05)
    assert note == ""
    assert eng._leases()["file:extra.py"]["agent"] == "swarm/a"


def test_contain_drift_waits_out_a_holder(repo):
    eng = Engine(str(repo))
    eng.reserve(keys=["file:extra.py"], agent="swarm/other", ttl=1)
    t0 = time.time()
    note = _contain_drift(eng, "swarm/a", ["file:extra.py"], wait_s=10, poll_s=0.1)
    assert note == ""                       # granted once the holder expired
    assert time.time() - t0 >= 0.8
    assert eng._leases()["file:extra.py"]["agent"] == "swarm/a"


def test_contain_drift_advisory_timeout_names_holder(repo):
    eng = Engine(str(repo))
    eng.reserve(keys=["file:extra.py"], agent="swarm/hog", ttl=600)
    note = _contain_drift(eng, "swarm/a", ["file:extra.py"], wait_s=0.3, poll_s=0.1)
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


# ------------------------------------------------- symbol-level containment ---

ROGUE = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
ROGUE_B_EDIT = "def a():\n    return 1\n\n\ndef b():\n    return 20\n"


@pytest.fixture()
def repo_with_rogue(repo):
    def sh(*args):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)

    (repo / "rogue.py").write_text(ROGUE)
    sh("git", "add", "-A")
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
       "-m", "rogue base")
    return repo


def test_key_path_forms():
    assert key_path("file:a/b.py") == "a/b.py"
    assert key_path("py:a/b.py::C.m") == "a/b.py"
    assert key_path("js:src/x.ts::f") == "src/x.ts"
    assert key_path("go:pkg/y.go::F") == "pkg/y.go"
    assert key_path("opaque-key") == "opaque-key"


def test_keys_overlap_matrix():
    assert keys_overlap("file:m.py", "file:m.py")
    assert keys_overlap("file:m.py", "py:m.py::f")      # file covers symbols
    assert keys_overlap("py:m.py::f", "file:m.py")
    assert keys_overlap("py:m.py::f", "py:m.py::f")
    assert not keys_overlap("py:m.py::f", "py:m.py::g")  # disjoint symbols
    assert not keys_overlap("file:m.py", "file:n.py")
    assert not keys_overlap("py:m.py::f", "py:n.py::f")


def test_reserve_file_lease_blocks_symbol_key(repo):
    eng = Engine(str(repo))
    eng.reserve(keys=["file:rogue.py"], agent="swarm/other", ttl=600)
    r = eng.reserve(keys=["py:rogue.py::b"], agent="swarm/me")
    assert not r["granted"]
    assert r["conflicts"][0]["holder"] == "swarm/other"


def test_reserve_disjoint_symbol_leases_commute(repo):
    eng = Engine(str(repo))
    eng.reserve(keys=["py:rogue.py::a"], agent="swarm/other", ttl=600)
    assert eng.reserve(keys=["py:rogue.py::b"], agent="swarm/me")["granted"]
    assert not eng.reserve(keys=["py:rogue.py::a"], agent="swarm/me")["granted"]


def test_drift_keys_come_from_the_oracle(repo_with_rogue):
    def sh(*args):
        return subprocess.run(args, cwd=repo_with_rogue, check=True,
                              capture_output=True, text=True).stdout.strip()

    base = sh("git", "rev-parse", "HEAD")
    (repo_with_rogue / "rogue.py").write_text(ROGUE_B_EDIT)
    sh("git", "add", "-A")
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "-m", "edit b"], cwd=repo_with_rogue,
                   check=True)
    head = sh("git", "rev-parse", "HEAD")
    keys = _drift_keys(str(repo_with_rogue), base, head, ["rogue.py"])
    assert keys == ["py:rogue.py::b"]


def test_drift_keys_widen_on_unanalyzable(repo_with_rogue):
    # oracle failure (bogus revs) must widen to file granularity, never narrow
    keys = _drift_keys(str(repo_with_rogue), "nope", "alsonope", ["x.bin"])
    assert keys == ["file:x.bin"]


def test_symbol_drift_does_not_contend_with_sibling_symbol(repo_with_rogue,
                                                           monkeypatch):
    # a sibling holds a DIFFERENT symbol of the drifted file — symbol-level
    # containment reserves only rogue.py::b, so nothing contends and the
    # task lands without burning its drift-wait
    eng = Engine(str(repo_with_rogue))
    eng.reserve(keys=["py:rogue.py::a"], agent="swarm/sibling", ttl=600)

    def fake_worker(prompt, model, timeout, cwd):
        import pathlib
        pathlib.Path(cwd, "mod.py").write_text("x = 1\n\ndef greet():\n"
                                               "    return 'hi'\n")
        pathlib.Path(cwd, "rogue.py").write_text(ROGUE_B_EDIT)
        return types.SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(swarm, "_claude_plan", lambda p, m: PLAN)
    monkeypatch.setattr(swarm, "_run_worker", fake_worker)
    t0 = time.time()
    rc = run_swarm(make_args(repo_with_rogue, drift_wait=30))
    elapsed = time.time() - t0
    assert rc == 0
    landed = [e for e in eng._log_entries() if e.get("verdict") == "landed"]
    assert len(landed) == 1
    assert elapsed < 20, "symbol-disjoint drift must not burn the drift-wait"
    # the drift lease that was taken is the symbol, not the file
    leases = eng._leases()
    assert "py:rogue.py::b" not in {
        k for k, v in leases.items() if v["agent"] == "swarm/sibling"}
