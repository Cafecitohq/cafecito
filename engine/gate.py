"""The landing gate. Every landing candidate — clean textual merges included —
must pass a real test run before it may reach the landed log. This is not
optional equipment: MergeBench landed red mains twice during development, once
via an ungated clean merge (silent risk observed live). The gate is the gate.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import time

from gitutil import git, git_rc


def impact_tests(repo: str, paths: set[str], rev: str) -> set[str]:
    """Test files implied by `paths` at `rev`: touched test files themselves,
    plus the conventional sibling `tests/test_<stem>.py` of each source file."""
    out = set()
    for p in paths:
        name = pathlib.Path(p).name
        if "/tests/" in p or name.startswith("test_"):
            if p.endswith(".py"):
                out.add(p)
            continue
        t = str(pathlib.Path(p).parent / "tests" / f"test_{pathlib.Path(p).stem}.py")
        if git_rc(repo, "cat-file", "-e", f"{rev}:{t}")[0] == 0:
            out.add(t)
    return out


def run_gate(repo: str, candidate: str, test_files: list[str],
             test_cmd: list[str], timeout: int = 900) -> dict:
    """Materialize `candidate` in a throwaway worktree and run the tests.

    Returns {green, seconds, summary, tests}. Empty test set is reported as
    green with `no_signal: True` — landed, but flagged in the log.
    """
    if not test_files:
        return {"green": True, "no_signal": True, "seconds": 0.0,
                "summary": "no test signal", "tests": []}
    scratch = pathlib.Path(tempfile.mkdtemp(prefix="cafecito-gate-"))
    wt = scratch / "wt"
    git(repo, "worktree", "add", "--detach", "--quiet", str(wt), candidate)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(scratch),
           "PYTHONDONTWRITEBYTECODE": "1", "MPLBACKEND": "Agg", "LC_ALL": "C.UTF-8"}
    t0 = time.time()
    try:
        r = subprocess.run([*test_cmd, *test_files], cwd=wt, env=env,
                           capture_output=True, text=True, timeout=timeout)
        tail = "\n".join((r.stdout or r.stderr).strip().splitlines()[-2:])
        return {"green": r.returncode == 0, "no_signal": False,
                "seconds": round(time.time() - t0, 2),
                "summary": tail[-300:], "tests": test_files}
    except subprocess.TimeoutExpired:
        return {"green": False, "no_signal": False,
                "seconds": round(time.time() - t0, 2),
                "summary": f"gate timeout >{timeout}s", "tests": test_files}
    finally:
        git_rc(repo, "worktree", "remove", "--force", str(wt))
