#!/usr/bin/env python3
"""End-to-end smoke test for the v0.1 engine.

Builds a throwaway repo, then plays an uncoordinated three-agent fleet:
  agent-a  edits add()            → clean landing (gated)
  agent-b  edits mul(), from the ORIGINAL tip (never rebases) → commutes, lands
  agent-c  edits add() differently, also from the original tip → collides with
           agent-a's landed change → LIVE regenerative merge → gate → lands

Asserts: three landings (or c escalated with a sane reason), final tip carries
all landed changes, the materialized branch matches the engine tip, and the
full test file is green at the tip. Requires the `claude` CLI for agent-c's
regeneration; pass --no-regen to skip that leg.

Usage:  python3 -m cafecito.tests.smoke [--no-regen] [--pytest-python /path/to/python]
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys
import tempfile

from cafecito.engine import Engine
from cafecito.gitutil import git

CALC = '''\
def add(a, b):
    return a + b


def mul(a, b):
    return a * b
'''

TEST_CALC = '''\
from calc import add, mul


def test_add():
    assert add(2, 3) == 5


def test_mul():
    assert mul(2, 3) == 6
'''

ADD_V2 = '''\
def add(a, b):
    """Add two numbers."""
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        raise TypeError("add() expects numbers")
    return a + b
'''

MUL_V2 = '''\
def mul(a, b):
    """Multiply two numbers."""
    return a * b
'''

ADD_V3 = '''\
def add(a, b):
    # log-friendly addition
    result = a + b
    return result
'''


def sh(cwd, *args):
    r = subprocess.run(list(args), cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{args}: {r.stderr.strip()[:200]}")
    return r.stdout


def agent_commit(repo_engine: Engine, name: str, edit) -> str:
    """One fleet member: sync worktree → edit → commit → return head sha."""
    wt = repo_engine.sync(agent=name, create_worktree=True)["worktree"]
    edit(pathlib.Path(wt))
    sh(wt, "git", "add", "-A")
    sh(wt, "git", "-c", f"user.name={name}", "-c",
       f"user.email={name}@cafecito.local", "commit", "-q", "-m",
       f"{name}: change")
    head = sh(wt, "git", "rev-parse", "HEAD").strip()
    sh(repo_engine.repo, "git", "worktree", "remove", "--force", wt)
    return head


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-regen", action="store_true")
    ap.add_argument("--pytest-python", default=sys.executable)
    args = ap.parse_args()

    root = pathlib.Path(tempfile.mkdtemp(prefix="cafecito-smoke-"))
    repo = root / "repo"
    repo.mkdir()
    sh(repo, "git", "init", "-q", "-b", "main")
    (repo / "calc.py").write_text(CALC)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(TEST_CALC)
    (repo / "tests" / "__init__.py").write_text("")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "-c", "user.name=smoke", "-c", "user.email=s@s", "commit",
       "-q", "-m", "init")

    eng = Engine(str(repo))
    eng.config["test_cmd"] = [args.pytest_python, "-m", "pytest", "-q",
                              "--tb=line", "-p", "no:cacheprovider"]
    tip0 = eng.sync()["tip"]
    print(f"[smoke] repo at {repo}, tip {tip0[:10]}")

    # Leases: agent-a reserves add(); agent-c's probe must report contention.
    r = eng.reserve(["py:calc.py::add"], agent="agent-a", intent="validate add")
    assert r["granted"], r
    r = eng.reserve(["py:calc.py::add"], agent="agent-c", intent="log add")
    assert not r["granted"] and r["conflicts"][0]["holder"] == "agent-a", r
    print("[smoke] leases: grant + contention detection OK")

    def edit_add_v2(wt):  # noqa: ANN001
        (wt / "calc.py").write_text(ADD_V2 + "\n\ndef mul(a, b):\n    return a * b\n")
        t = (wt / "tests" / "test_calc.py").read_text()
        (wt / "tests" / "test_calc.py").write_text(
            t + "\n\ndef test_add_rejects_strings():\n"
                "    import pytest\n"
                "    with pytest.raises(TypeError):\n"
                "        from calc import add\n"
                "        add('a', 'b')\n")

    def edit_mul_v2(wt):  # noqa: ANN001
        c = (wt / "calc.py").read_text()
        (wt / "calc.py").write_text(c.replace(
            "def mul(a, b):\n    return a * b", MUL_V2.rstrip()))

    def edit_add_v3(wt):  # noqa: ANN001
        c = (wt / "calc.py").read_text()
        (wt / "calc.py").write_text(c.replace(
            "def add(a, b):\n    return a + b", ADD_V3.rstrip()))

    head_a = agent_commit(eng, "agent-a", edit_add_v2)
    head_b = agent_commit(eng, "agent-b", edit_mul_v2)   # from ORIGINAL tip
    head_c = agent_commit(eng, "agent-c", edit_add_v3)   # from ORIGINAL tip

    ra = eng.submit(head_a, agent="agent-a", title="add(): docstring + validation")
    assert ra["verdict"] == "landed", ra
    assert not ra["regenerated"] and not ra["gate"]["no_signal"], ra
    print(f"[smoke] agent-a landed clean, gate {ra['gate']['seconds']}s")
    assert "agent-a" not in {v["agent"] for v in eng._leases().values()}, \
        "leases must be released on landing"

    rb = eng.submit(head_b, agent="agent-b", title="mul(): docstring")
    assert rb["verdict"] == "landed", rb
    assert not rb["regenerated"], "commuting change must not need regeneration"
    print(f"[smoke] agent-b commuted and landed without rebase, gate {rb['gate']['seconds']}s")

    if args.no_regen:
        print("[smoke] skipping regeneration leg (--no-regen)")
    else:
        rc = eng.submit(head_c, agent="agent-c", title="add(): result variable for logging")
        assert rc["verdict"] in ("landed", "escalated"), rc
        if rc["verdict"] == "landed":
            assert rc["regenerated"], "colliding change must land via regeneration"
            print(f"[smoke] agent-c collided, REGENERATED, landed; gate {rc['gate']['seconds']}s")
        else:
            print(f"[smoke] agent-c escalated: {rc['reason']} (acceptable outcome)")

    st = eng.status()
    tip = st["tip"]
    branch_tip = git(str(repo), "rev-parse", eng.config["branch"]).strip()
    assert tip == branch_tip, "materialized branch must match engine tip"
    final_calc = git(str(repo), "show", f"{tip}:calc.py")
    assert "Multiply two numbers" in final_calc, "agent-b's change missing at tip"
    assert "TypeError" in final_calc, "agent-a's change missing at tip"
    r = subprocess.run([args.pytest_python, "-m", "pytest", "-q",
                        "-p", "no:cacheprovider", "tests/test_calc.py"],
                       cwd=repo, capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(root)})
    # the working copy is still at the ORIGINAL main; check the tip instead
    wt = root / "final"
    git(str(repo), "worktree", "add", "--detach", "--quiet", str(wt), tip)
    r = subprocess.run([args.pytest_python, "-m", "pytest", "-q",
                        "-p", "no:cacheprovider", "tests/test_calc.py"],
                       cwd=wt, capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(root)})
    assert r.returncode == 0, f"final tip not green:\n{r.stdout[-400:]}"
    print(f"[smoke] final tip green; landed={st['landed']} escalated={st['escalated']}")
    print("[smoke] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
