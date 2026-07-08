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

from .gitutil import git, git_rc


_CODE_EXTS = (".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts",
              ".cts", ".go")


def _is_test_file(p: str) -> bool:
    name = pathlib.Path(p).name
    return ("/tests/" in p or "/__tests__/" in p or name.startswith("test_")
            or name.endswith("_test.go") or ".test." in name or ".spec." in name)


def impact_tests(repo: str, paths: set[str], rev: str) -> set[str]:
    """Test files implied by `paths` at `rev`: touched test files themselves,
    plus each ecosystem's sibling-test convention —
    python  pkg/mod.py      → pkg/tests/test_mod.py
    js/ts   pkg/mod.ts      → pkg/mod.test.ts · pkg/mod.spec.ts ·
                              pkg/__tests__/mod.test.ts · pkg/__tests__/mod.spec.ts
    go      pkg/mod.go      → pkg/mod_test.go
    Only candidates that exist at `rev` are returned."""
    out = set()
    for p in paths:
        pp = pathlib.Path(p)
        if _is_test_file(p):
            if pp.suffix in _CODE_EXTS:
                out.add(p)
            continue
        cands: list[str] = []
        if pp.suffix == ".py":
            cands.append(str(pp.parent / "tests" / f"test_{pp.stem}.py"))
        elif pp.suffix == ".go":
            cands.append(str(pp.parent / f"{pp.stem}_test.go"))
        elif pp.suffix in _CODE_EXTS:
            for marker in ("test", "spec"):
                cands.append(str(pp.parent / f"{pp.stem}.{marker}{pp.suffix}"))
                cands.append(str(pp.parent / "__tests__" / f"{pp.stem}.{marker}{pp.suffix}"))
        for t in cands:
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
