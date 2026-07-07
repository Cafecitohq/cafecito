#!/usr/bin/env python3
"""Semantic validation for Experiment B: dual test-suite execution.

The incorporation heuristic can't tell a correct regeneration that *rewrites*
code (e.g. under a rename) from one that dropped an intent. This runner
adjudicates by executing tests, with a control for pre-existing breakage:

  state OURS   — one branch's changes replayed onto the other's base
  state THEIRS — the other branch's head
  state MERGED — the pair-attributed merge tree with the reconciler's
                 regenerated regions spliced into the conflicted files

  1. Each side's tests (test files that side itself changed, plus the sibling
     `tests/test_<stem>.py` of every conflicted source file) run in that
     side's HOME state. Only tests that pass at home count as signal —
     flaky/broken/env-incompatible tests are excluded, per side, per file.
  2. All surviving tests run in MERGED.

  semantic_pass = every surviving test passes in MERGED, with ≥1 surviving
  test on EACH side. If a side contributes no surviving tests the pair is
  reported as `partial` (that side is unvalidated), never as a pass.

Sandboxing (v1, documented honestly): process-level isolation — dedicated
venv, detached git worktrees, temp HOME, CPU rlimit and wall-clock timeout
per test file. This is the same trust level as a developer running an OSS
project's test suite locally. Container isolation is future hardening for
untrusted corpora.

Usage:
  python3 validate_b.py --repo workdir/repos/sympy
  (reads and updates workdir/results/conflicts/<repo>_b.json in place)
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import resource
import shutil
import subprocess
import sys
import tempfile
import time
import venv

from experiment_b import MAX_REGIONS, Region, diff3_segments
from gitutil import git, git_rc, show

TEST_DEPS = ["pytest", "hypothesis", "mpmath"]


def ensure_venv(workdir: pathlib.Path) -> pathlib.Path:
    """Create (once) the isolated venv used for all test runs."""
    env_dir = workdir / "venv-test"
    py = env_dir / "bin" / "python"
    if not py.exists():
        print(f"[venv] creating {env_dir} …", file=sys.stderr)
        venv.create(env_dir, with_pip=True)
        subprocess.run([str(py), "-m", "pip", "-q", "install", *TEST_DEPS], check=True)
    return py


def is_test_file(p: str) -> bool:
    return p.endswith(".py") and ("/tests/" in p or p.rsplit("/", 1)[-1].startswith("test_"))


def side_test_files(repo: str, base: str, head: str) -> list[str]:
    names = git(repo, "diff", "--name-only", "--no-renames", base, head).splitlines()
    return sorted({n for n in names if is_test_file(n)})


def impact_test_files(conflicted: list[str]) -> list[str]:
    """Convention-based: pkg/mod.py → pkg/tests/test_mod.py (existence checked later)."""
    out = []
    for src in conflicted:
        p = pathlib.Path(src)
        out.append(str(p.parent / "tests" / f"test_{p.stem}.py"))
    return out


def _limits() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (1200, 1200))
    resource.setrlimit(resource.RLIMIT_NOFILE, (2048, 2048))


def run_pytest(py: pathlib.Path, worktree: pathlib.Path, test_file: str,
               scratch: pathlib.Path, timeout: int) -> dict:
    """One pytest process per test file; returns {status, detail}."""
    if not (worktree / test_file).exists():
        return {"status": "absent", "detail": ""}
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(scratch),
        "PYTHONDONTWRITEBYTECODE": "1",
        "MPLBACKEND": "Agg",
        "LC_ALL": "C.UTF-8",
    }
    try:
        r = subprocess.run(
            [str(py), "-m", "pytest", "-q", "--tb=line", "-p", "no:cacheprovider",
             test_file],
            cwd=worktree, env=env, capture_output=True, text=True,
            timeout=timeout, preexec_fn=_limits,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "detail": f">{timeout}s"}
    tail = "\n".join((r.stdout or r.stderr).strip().splitlines()[-3:])
    if r.returncode == 0:
        return {"status": "pass", "detail": tail}
    if r.returncode == 1:
        return {"status": "fail", "detail": tail}
    # 2=interrupted, 3=internal, 4=usage, 5=no tests collected, else import crash
    return {"status": "error", "detail": f"rc={r.returncode} {tail[-400:]}"}


class Worktrees:
    """Detached worktrees for the three states; always cleaned up."""

    def __init__(self, repo: str, root: pathlib.Path):
        self.repo, self.root, self.dirs = repo, root, []

    def add(self, name: str, committish: str) -> pathlib.Path:
        path = self.root / name
        git(self.repo, "worktree", "add", "--detach", "--quiet", str(path), committish)
        self.dirs.append(path)
        return path

    def cleanup(self) -> None:
        for d in self.dirs:
            git_rc(self.repo, "worktree", "remove", "--force", str(d))
        self.dirs.clear()


def merged_commit(repo: str, sim: dict) -> str:
    """Commit holding the pair-attributed merge tree (conflict markers and all)."""
    code, out, _ = git_rc(repo, "merge-tree", "--write-tree", "--no-messages",
                          f"--merge-base={sim['base']}", sim["ours"], sim["theirs"])
    if code not in (0, 1):
        raise RuntimeError("merge-tree failed while rebuilding merged state")
    tree = out.splitlines()[0].strip()
    return git(repo, "commit-tree", tree, "-p", sim["ours"],
               "-m", "cafecito merged-state").strip()


def spliced_files(repo: str, rec: dict) -> dict[str, str]:
    """Reconstruct {path: merged content} from the stored regenerated regions.

    Mirrors experiment_b's segmentation exactly (same inputs, deterministic
    `git merge-file`), so stored region numbers line up.
    """
    sim = rec["pair"]["sim"]
    regions = {int(k): v for k, v in rec["merged_regions"].items()}
    out: dict[str, str] = {}
    n = 0
    for p in rec["paths"]:
        vb = show(repo, sim["base"], p) or ""
        va, vt = show(repo, sim["ours"], p), show(repo, sim["theirs"], p)
        if va is None or vt is None:
            raise RuntimeError(f"{p}: side version vanished")
        segs = diff3_segments(vb, va, vt)
        if segs is None:
            continue
        if sum(isinstance(s, Region) for s in segs) > MAX_REGIONS:
            raise RuntimeError(f"{p}: region count changed since experiment run")
        pieces = []
        for s in segs:
            if isinstance(s, Region):
                n += 1
                if regions.get(n) is None:
                    raise RuntimeError(f"{p}: stored output missing region {n}")
                pieces.append(regions[n])
            else:
                pieces.append(s)
        out[p] = "".join(pieces)
    return out


def validate_pair(repo: str, rec: dict, py: pathlib.Path, timeout: int) -> dict:
    sim = rec["pair"]["sim"]
    if sim["replayed"] == rec["pair"]["a"]["head"]:
        side_ours, side_theirs = rec["pair"]["a"], rec["pair"]["b"]
    else:
        side_ours, side_theirs = rec["pair"]["b"], rec["pair"]["a"]

    impact = impact_test_files(rec["paths"])
    sides = {
        "ours": {"state": sim["ours"],
                 "tests": sorted(set(side_test_files(repo, side_ours["base"],
                                                     side_ours["head"]) + impact))},
        "theirs": {"state": sim["theirs"],
                   "tests": sorted(set(side_test_files(repo, side_theirs["base"],
                                                       side_theirs["head"]) + impact))},
    }

    root = pathlib.Path(tempfile.mkdtemp(prefix="cafecito-validate-"))
    wt = Worktrees(repo, root)
    try:
        merged_dir = wt.add("merged", merged_commit(repo, sim))
        for path, content in spliced_files(repo, rec).items():
            (merged_dir / path).write_text(content)

        survivors: dict[str, list[str]] = {}
        home_results: dict[str, dict] = {}
        for name, side in sides.items():
            home = wt.add(name, side["state"])
            kept, results = [], {}
            for tf in side["tests"]:
                res = run_pytest(py, home, tf, root, timeout)
                results[tf] = res
                if res["status"] == "pass":
                    kept.append(tf)
            survivors[name] = kept
            home_results[name] = results

        merged_results: dict[str, dict] = {}
        for tf in sorted(set(survivors["ours"]) | set(survivors["theirs"])):
            merged_results[tf] = run_pytest(py, merged_dir, tf, root, timeout)

        merged_ok = all(r["status"] == "pass" for r in merged_results.values())
        both_signal = bool(survivors["ours"]) and bool(survivors["theirs"])
        verdict = ("pass" if merged_ok and both_signal else
                   "partial" if merged_ok else "fail")
        return {
            "verdict": verdict,
            "home": home_results,
            "survivors": survivors,
            "merged": merged_results,
        }
    finally:
        wt.cleanup()
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--timeout", type=int, default=900, help="per test file, seconds")
    ap.add_argument("--results", default=None)
    args = ap.parse_args()

    repo = str(pathlib.Path(args.repo).resolve())
    name = pathlib.Path(repo).name
    here = pathlib.Path(__file__).parent
    results_dir = pathlib.Path(args.results or here / "workdir" / "results" / "conflicts")
    b_file = results_dir / f"{name}_b.json"
    if not b_file.exists():
        print(f"run experiment_b.py first — missing {b_file}", file=sys.stderr)
        return 1

    py = ensure_venv(here / "workdir")
    recs = json.loads(b_file.read_text())
    for rec in recs:
        if not rec.get("merged_regions"):
            rec["semantic"] = {"verdict": "skipped", "reason": "no stored regions"}
            continue
        label = (f"{rec['pair']['a']['subject'][:38]!r} × "
                 f"{rec['pair']['b']['subject'][:38]!r}")
        print(f"[{name}] validating {label}", file=sys.stderr)
        t0 = time.time()
        try:
            rec["semantic"] = validate_pair(repo, rec, py, args.timeout)
        except (RuntimeError, OSError, subprocess.CalledProcessError) as e:
            rec["semantic"] = {"verdict": "error", "reason": str(e)[:300]}
        rec["semantic"]["elapsed_s"] = round(time.time() - t0, 1)
        print(f"[{name}]   → {rec['semantic']['verdict'].upper()} "
              f"({rec['semantic']['elapsed_s']}s)", file=sys.stderr)

    b_file.write_text(json.dumps(recs, indent=1))

    n = len(recs)
    verdicts = [r.get("semantic", {}).get("verdict", "skipped") for r in recs]
    print(f"""
== cafecito phase 0 · experiment B semantic validation · {name} =========
pairs:            {n}
semantic PASS:    {verdicts.count("pass")}   (all surviving tests green in merged state, both sides had signal)
partial:          {verdicts.count("partial")}   (merged green, but one side contributed no surviving tests)
FAIL:             {verdicts.count("fail")}   (a home-passing test broke in the merged state)
error/skipped:    {verdicts.count("error") + verdicts.count("skipped")}
==========================================================================""")
    for r, v in zip(recs, verdicts):
        for tf, res in (r.get("semantic", {}).get("merged") or {}).items():
            print(f"  [{v}] {tf}: {res['status']}")
    print(f"[{name}] updated {b_file}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
