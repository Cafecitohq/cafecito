#!/usr/bin/env python3
"""Corpus driver: run the full Phase 0 pipeline across many repos.

For each repo it (1) checks the repo actually has a merge-commit workflow
(squash-merge repos reconstruct no branches and are reported and skipped),
(2) runs experiment A, (3) runs the attributed conflict scan, then prints an
aggregated results table ready to paste into README.md.

Experiment B / validate_b are NOT run here — they cost reconciler calls and
per-repo test environments; run them per repo on whatever conflicts this
surfaces.

Usage:
  python3 run_corpus.py --repos workdir/repos/*/ [--since 2024-06-01]
                        [--max-pairs 400] [--min-branches 40]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

from mine import mine_branches

HERE = pathlib.Path(__file__).parent


def run(script: str, repo: str, *extra: str) -> int:
    r = subprocess.run([sys.executable, str(HERE / script), "--repo", repo, *extra])
    return r.returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repos", nargs="+", required=True)
    ap.add_argument("--since", default="2024-06-01")
    ap.add_argument("--max-pairs", type=int, default=400)
    ap.add_argument("--min-branches", type=int, default=40,
                    help="below this, the repo is assumed squash-workflow and skipped")
    args = ap.parse_args()

    results_dir = HERE / "workdir" / "results"
    ran, skipped = [], []
    for raw in args.repos:
        repo = str(pathlib.Path(raw.rstrip("/")).resolve())
        name = pathlib.Path(repo).name
        if (results_dir / f"{name}_a.json").exists():
            print(f"[{name}] already measured — will include in table", file=sys.stderr)
            ran.append(name)
            continue
        n = len(mine_branches(repo, args.since))
        if n < args.min_branches:
            print(f"[{name}] only {n} reconstructable branches — squash workflow? skipping",
                  file=sys.stderr)
            skipped.append((name, n))
            continue
        print(f"[{name}] {n} branches — running pipeline", file=sys.stderr)
        if run("experiment_a.py", repo, "--since", args.since,
               "--max-pairs", str(args.max_pairs)) != 0:
            skipped.append((name, n))
            continue
        run("find_conflicts.py", repo, "--since", args.since)
        ran.append(name)

    # Aggregate table
    print("\n| repo | branches | pairs | symbol-disjoint | file-disjoint | "
          "textual conflicts | oracle win | silent risk | attributed conflicts (scan) |")
    print("|---|---|---|---|---|---|---|---|---|")
    tot = {"n": 0, "sym": 0, "file": 0, "conf": 0, "scan": 0}
    for name in sorted(ran):
        s = json.loads((results_dir / f"{name}_a.json").read_text())["summary"]
        branches = len(mine_branches(str(HERE / "workdir" / "repos" / name), args.since)) \
            if (HERE / "workdir" / "repos" / name).exists() else "?"
        cfile = results_dir / "conflicts" / f"{name}_a.json"
        scan = json.loads(cfile.read_text())["summary"]["n"] if cfile.exists() else "—"
        n = s["n"] or 1
        print(f"| {name} | {branches} | {s['n']} | {100*s['symbol_disjoint']/n:.1f}% "
              f"| {100*s['file_disjoint']/n:.1f}% | {100*s['textual_conflict']/n:.1f}% "
              f"| {100*s['oracle_win']/n:.1f}% | {100*s['silent_risk']/n:.1f}% | {scan} |")
        tot["n"] += s["n"]; tot["sym"] += s["symbol_disjoint"]
        tot["file"] += s["file_disjoint"]; tot["conf"] += s["textual_conflict"]
        if scan != "—":
            tot["scan"] += scan
    if tot["n"]:
        print(f"| **all** | | **{tot['n']}** | **{100*tot['sym']/tot['n']:.1f}%** "
              f"| {100*tot['file']/tot['n']:.1f}% | {100*tot['conf']/tot['n']:.1f}% "
              f"| | | **{tot['scan']}** |")
    for name, n in skipped:
        print(f"skipped: {name} ({n} branches)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
