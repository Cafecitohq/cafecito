import json
import subprocess
import sys
import threading
import time

import pytest

from cafecito.engine import Engine

SLEEP = 1.5  # per-suite think time; parallel wall must beat 2x this + slack

MOD = "VALUE = 1\n"
TEST = ("import time\n\nfrom pkg_{n}.mod import VALUE\n\n\n"
        f"def test_value_{{n}}():\n    time.sleep({SLEEP})\n    assert VALUE == 1\n")


@pytest.fixture()
def repo(tmp_path):
    def sh(*args):
        subprocess.run(args, cwd=tmp_path, check=True, capture_output=True)

    for n in ("a", "b"):
        d = tmp_path / f"pkg_{n}"
        (d / "tests").mkdir(parents=True)
        (d / "__init__.py").write_text("")
        (d / "mod.py").write_text(MOD)
        (d / "tests" / "__init__.py").write_text("")
        (d / "tests" / "test_mod.py").write_text(TEST.format(n=n))
    (tmp_path / "conftest.py").write_text(
        "import pathlib, sys\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).parent))\n")
    sh("git", "init", "-q", "-b", "main")
    sh("git", "add", "-A")
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "i")
    return tmp_path


def branch_edit(repo, name, path, content, msg):
    def sh(*args):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)

    sh("git", "checkout", "-q", "-b", name, "main")
    (repo / path).write_text(content)
    sh("git", "add", "-A")
    sh("git", "-c", "user.name=a", "-c", "user.email=a@a", "commit", "-q", "-m", msg)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    sh("git", "checkout", "-q", "main")
    return head


def make_engine(repo):
    eng = Engine(str(repo))
    eng.config["test_cmd"] = [sys.executable, "-m", "pytest", "-q",
                              "-p", "no:cacheprovider"]
    return eng


def test_stale_inflight_entries_are_pruned(repo):
    eng = make_engine(repo)
    (repo / ".cafecito" / "inflight.json").write_text(json.dumps({
        "cs_ghost": {"symbols": [], "files": ["x.py"], "agent": "ghost",
                     "started": time.time() - eng.config["gate_timeout_s"] * 3}}))
    assert eng._inflight() == {}


def test_admission_blocks_intersecting_then_proceeds(repo):
    eng = make_engine(repo)
    eng.config["test_cmd"] = [sys.executable, "-c", "pass"]
    head = branch_edit(repo, "wa", "pkg_a/mod.py", "VALUE = 1  # doc\n", "touch a")
    blocker = repo / ".cafecito" / "inflight.json"
    blocker.write_text(json.dumps({
        "cs_block": {"symbols": [], "files": ["pkg_a/mod.py"],
                     "agent": "blocker", "started": time.time()}}))
    result: dict = {}
    t = threading.Thread(target=lambda: result.update(
        eng.submit(head, agent="w", title="touch a")))
    t.start()
    time.sleep(1.0)
    assert not result, "must still be waiting in admission"
    blocker.write_text("{}")  # blocker lands/aborts → gone
    t.join(timeout=60)
    assert result.get("verdict") == "landed", result


def test_commuting_submissions_gate_in_parallel(repo):
    eng = make_engine(repo)
    a = branch_edit(repo, "wa", "pkg_a/mod.py", 'VALUE = 1\nNOTE_A = "hi"\n', "a")
    b = branch_edit(repo, "wb", "pkg_b/mod.py", 'VALUE = 1\nNOTE_B = "hi"\n', "b")
    results: dict = {}

    def go(key, head):
        results[key] = eng.submit(head, agent=key, title=f"note {key}")

    t0 = time.time()
    ta = threading.Thread(target=go, args=("a", a))
    tb = threading.Thread(target=go, args=("b", b))
    ta.start(); tb.start(); ta.join(120); tb.join(120)
    wall = time.time() - t0

    assert results["a"]["verdict"] == "landed", results["a"]
    assert results["b"]["verdict"] == "landed", results["b"]
    # guard against vacuous passes: the gates must have actually run tests
    for r in results.values():
        assert not r["gate"].get("no_signal") and r["gate"]["tests"], r["gate"]
    # serial gates would cost >= 2*SLEEP + 2x overhead (~4s+);
    # parallel gates overlap the sleeps — the racer's re-gate is a fact hit
    assert wall < 2 * SLEEP + 0.6, f"gates did not overlap: wall={wall:.2f}s"

    tip = eng.status()["tip"]
    show = lambda p: subprocess.run(["git", "show", f"{tip}:{p}"], cwd=repo,  # noqa: E731
                                    capture_output=True, text=True).stdout
    assert "NOTE_A" in show("pkg_a/mod.py")
    assert "NOTE_B" in show("pkg_b/mod.py")
    # exactly one of them re-landed after racing; its log entry says so
    raced = [e for e in eng._log_entries(10) if e.get("raced")]
    assert len(raced) == 1, [
        (e.get("title"), e.get("raced")) for e in eng._log_entries(10)]
