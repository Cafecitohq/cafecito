import json
import subprocess
import sys
import time
import types

import pytest

from cafecito.doctor import ERR, OK, collect_checks, run_gc
from cafecito.engine import Engine


@pytest.fixture()
def repo(tmp_path):
    def sh(*args):
        subprocess.run(args, cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / "a.py").write_text("x = 1\n")
    sh("git", "init", "-q", "-b", "main")
    sh("git", "add", "-A")
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "i")
    return tmp_path


def by_name(checks):
    return {c["name"]: c for c in checks}


def test_doctor_healthy_repo(repo):
    eng = Engine(str(repo))
    eng.config["test_cmd"] = [sys.executable, "-c", "pass"]
    (eng.state_dir / "config.json").write_text(json.dumps(eng.config))
    checks = by_name(collect_checks(str(repo)))
    assert checks["git"]["status"] == OK
    assert checks["landed branch"]["status"] == OK
    assert checks["test_cmd"]["status"] == OK


def test_doctor_flags_bad_test_cmd_and_ref_drift(repo):
    eng = Engine(str(repo))
    eng.config["test_cmd"] = ["definitely-not-a-binary"]
    (eng.state_dir / "config.json").write_text(json.dumps(eng.config))
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "--allow-empty", "-m", "drift"],
                   cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "update-ref", "refs/heads/cafecito/main", "HEAD"],
                   cwd=repo, check=True, capture_output=True)
    checks = by_name(collect_checks(str(repo)))
    assert checks["test_cmd"]["status"] == ERR
    assert checks["landed branch"]["status"] == ERR


def test_gc_cleans_leases_inflight_and_worktrees(repo):
    eng = Engine(str(repo))
    (eng.state_dir / "leases.json").write_text(json.dumps({
        "file:x": {"agent": "a", "expires": time.time() - 10},
        "file:y": {"agent": "b", "expires": time.time() + 500}}))
    (eng.state_dir / "inflight.json").write_text(json.dumps({
        "cs_dead": {"symbols": [], "files": [], "agent": "ghost",
                    "started": time.time() - 99999}}))
    wt = eng.sync(agent="gc-test", create_worktree=True)["worktree"]
    import shutil
    shutil.rmtree(wt)   # simulate a crashed agent leaving a registered ghost

    rc = run_gc(types.SimpleNamespace(repo=str(repo)))
    assert rc == 0
    assert set(eng._leases()) == {"file:y"}
    assert eng._inflight() == {}
    out = subprocess.run(["git", "worktree", "list"], cwd=repo,
                         capture_output=True, text=True).stdout
    assert "cafecito-" not in out
