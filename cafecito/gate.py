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

from . import isolation
from .closure import python_closure
from .facts import fact_key
from .gitutil import git, git_rc


_CODE_EXTS = (".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts",
              ".cts", ".go")


def _is_test_file(p: str) -> bool:
    """A RUNNABLE test file — directory membership alone is not enough
    (tests/__init__.py and helpers collect zero tests and must not gate)."""
    name = pathlib.Path(p).name
    if name.endswith("_test.go") or ".test." in name or ".spec." in name:
        return True
    if p.endswith(".py"):
        return name.startswith("test_") or name.endswith("_test.py")
    return False


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


def blob_map(repo: str, rev: str) -> dict[str, str]:
    """{path: blob sha} for the whole tree at rev — git's content addressing,
    one call."""
    out = git(repo, "ls-tree", "-r", rev)
    m: dict[str, str] = {}
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob":
            m[path] = parts[2]
    return m


def _run_setup(worktree: pathlib.Path, setup_cmd: list[str],
               timeout: int) -> str | None:
    """Prepare a bare worktree (npm ci, pip install, …). Runs with the REAL
    environment — installs need caches and network — unlike the tests, which
    keep their restricted env. Returns an error string or None."""
    try:
        r = subprocess.run(setup_cmd, cwd=worktree, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"setup timeout >{timeout}s"
    except OSError as e:
        return f"setup failed to start: {e}"
    if r.returncode != 0:
        return f"setup failed: {(r.stderr or r.stdout).strip()[-200:]}"
    return None


def run_gate(repo: str, candidate: str, test_files: list[str],
             test_cmd: list[str], timeout: int = 900, facts=None,
             setup_cmd: list[str] | None = None,
             setup_timeout: int = 600, isolation_mode: str = "none",
             container_image: str = "",
             container_runtime: str = "") -> dict:
    """Materialize `candidate` in a throwaway worktree and run the tests.

    With a FactsStore, runs are per test file and memoized: a file whose
    input closure (import graph, conftest chain, build manifests — see
    closure.py) is content-identical to a previously green run inherits the
    fact instead of executing. Closure confusion → the file always runs.
    Only green verdicts are recorded; reds re-run every time.

    Test invocations run under `isolation_mode` (see isolation.py); an
    unavailable backend reddens the gate rather than running unisolated.
    Setup keeps the real environment either way — installs need network.

    Returns {green, seconds, summary, tests, memo?}. Empty test set is
    reported green with `no_signal: True` — landed, but flagged in the log.
    """
    if not test_files:
        return {"green": True, "no_signal": True, "seconds": 0.0,
                "summary": "no test signal", "tests": []}
    scratch = pathlib.Path(tempfile.mkdtemp(prefix="cafecito-gate-"))
    wt = scratch / "wt"
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(scratch),
           "PYTHONDONTWRITEBYTECODE": "1", "MPLBACKEND": "Agg", "LC_ALL": "C.UTF-8"}
    iso_err = isolation.unavailable(isolation_mode, container_image,
                                    container_runtime)
    if isolation_mode == "sandbox" and not iso_err:
        (scratch / "tmp").mkdir()
        env["TMPDIR"] = str(scratch / "tmp")
    def _wrap():
        return isolation.wrap(test_cmd, isolation_mode, worktree=str(wt),
                              write_roots=[str(scratch)],
                              image=container_image,
                              runtime=container_runtime)

    t0 = time.time()

    def finish(green, summary, extra=None):
        out = {"green": green, "no_signal": False,
               "seconds": round(time.time() - t0, 2),
               "summary": summary[-300:], "tests": test_files}
        if extra:
            out.update(extra)
        return out

    try:
        if facts is None:
            if iso_err:
                return finish(False, f"isolation unavailable: {iso_err}")
            git(repo, "worktree", "add", "--detach", "--quiet", str(wt), candidate)
            if setup_cmd:
                err = _run_setup(wt, setup_cmd, setup_timeout)
                if err:
                    return finish(False, err)
            run_cmd = _wrap()
            try:
                r = subprocess.run([*run_cmd, *test_files], cwd=wt, env=env,
                                   capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                return finish(False, f"gate timeout >{timeout}s")
            tail = "\n".join((r.stdout or r.stderr).strip().splitlines()[-2:])
            return finish(r.returncode == 0, tail)

        blobs = blob_map(repo, candidate)
        listing = set(blobs)
        # facts recorded under isolation are distinct facts — a green run
        # with network open must not be inherited by a sandboxed gate
        key_cmd = (test_cmd if isolation_mode == "none"
                   else [f"isolation:{isolation_mode}", *test_cmd])
        plan = []  # (file, key or None)
        hits = 0
        for f in test_files:
            closure = python_closure(repo, candidate, f, listing)
            key = None
            if closure is not None:
                key = fact_key(key_cmd, f, [(p, blobs[p]) for p in sorted(closure)])
                if facts.green(key):
                    hits += 1
                    continue
            plan.append((f, key))
        if not plan:
            return finish(True, f"all {hits} facts inherited",
                          {"memo": {"hits": hits, "runs": 0}})
        if iso_err:
            return finish(False, f"isolation unavailable: {iso_err}",
                          {"memo": {"hits": hits, "runs": 0}})
        git(repo, "worktree", "add", "--detach", "--quiet", str(wt), candidate)
        if setup_cmd:
            err = _run_setup(wt, setup_cmd, setup_timeout)
            if err:
                return finish(False, err,
                              {"memo": {"hits": hits, "runs": 0}})
        run_cmd = _wrap()
        green, last = True, ""
        runs = 0
        for f, key in plan:
            try:
                r = subprocess.run([*run_cmd, f], cwd=wt, env=env,
                                   capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                return finish(False, f"gate timeout >{timeout}s in {f}",
                              {"memo": {"hits": hits, "runs": runs}})
            runs += 1
            last = "\n".join((r.stdout or r.stderr).strip().splitlines()[-2:])
            if r.returncode == 5:      # pytest: no tests collected — not a failure,
                continue               # but no fact either
            if r.returncode != 0:
                green = False
                break
            if key is not None:
                facts.record_green(key, f)
        return finish(green, last, {"memo": {"hits": hits, "runs": runs}})
    finally:
        git_rc(repo, "worktree", "remove", "--force", str(wt))
