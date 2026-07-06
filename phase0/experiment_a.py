#!/usr/bin/env python3
"""Experiment A — what fraction of concurrent changes provably commute?

For each pair of branches that were genuinely in flight at the same time:

  textual     — does a real 3-way merge (git merge-tree) conflict?
  file        — do the branches touch any common file?
  symbol      — do their symbol-level write sets intersect? (the oracle)

Today's merge queues serialize 100% of these pairs. The symbol-disjoint rate
is the fraction cafecito would land in parallel with zero rebasing and, under
verification-fact memoization, zero re-testing.

Usage:
  python3 experiment_a.py --repo workdir/repos/numpy [--since 2024-06-01]
                          [--max-pairs 400] [--out workdir/results]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

from attrib import attributed_merge
from mine import concurrent_pairs, is_dependent, mine_branches
from oracle import write_set


def pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:5.1f}%" if d else "  n/a"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--since", default="2024-06-01")
    ap.add_argument("--max-pairs", type=int, default=400)
    ap.add_argument("--out", default=None, help="directory for the JSON result file")
    args = ap.parse_args()

    repo = str(pathlib.Path(args.repo).resolve())
    name = pathlib.Path(repo).name
    t0 = time.time()

    print(f"[{name}] mining branches since {args.since} …", file=sys.stderr)
    branches = mine_branches(repo, args.since)
    pairs = concurrent_pairs(branches, args.max_pairs)
    print(f"[{name}] {len(branches)} branches, {len(pairs)} concurrent pairs sampled",
          file=sys.stderr)

    ws_cache: dict[str, tuple[frozenset, frozenset]] = {}

    def sets_for(br):
        if br.head not in ws_cache:
            ws_cache[br.head] = write_set(repo, br.base, br.head)
        return ws_cache[br.head]

    rows, skipped, drift = [], 0, 0
    for i, (a, b) in enumerate(pairs):
        if is_dependent(repo, a, b):
            skipped += 1
            continue
        att = attributed_merge(repo, a, b)
        if att.status == "error":  # missing history in shallow clone, etc.
            skipped += 1
            continue
        if att.status == "drift":  # branch conflicts with intervening mainline;
            drift += 1             # pair attribution impossible — excluded
            continue
        sym_a, files_a = sets_for(a)
        sym_b, files_b = sets_for(b)
        rows.append({
            "a": {"head": a.head, "base": a.base, "merge": a.merge, "subject": a.subject},
            "b": {"head": b.head, "base": b.base, "merge": b.merge, "subject": b.subject},
            "textual_conflict": att.status == "conflict",
            "file_overlap": sorted(files_a & files_b),
            "symbol_overlap": sorted(sym_a & sym_b),
        })
        if (i + 1) % 50 == 0:
            print(f"[{name}] …{i + 1}/{len(pairs)} pairs", file=sys.stderr)

    n = len(rows)
    sym_disjoint = sum(1 for r in rows if not r["symbol_overlap"])
    file_disjoint = sum(1 for r in rows if not r["file_overlap"])
    textual = sum(1 for r in rows if r["textual_conflict"])
    oracle_win = sum(1 for r in rows if r["file_overlap"] and not r["symbol_overlap"])
    silent_risk = sum(1 for r in rows if not r["textual_conflict"] and r["symbol_overlap"])

    print(f"""
== cafecito phase 0 · experiment A · {name} ==============================
branches reconstructed:        {len(branches)}
concurrent pairs analyzed:     {n}   (skipped: {skipped}, mainline-drift excluded: {drift})
elapsed:                       {time.time() - t0:.0f}s

today's baseline — merge queue serializes:      100.0%   of pairs
symbol-disjoint  — oracle lands IN PARALLEL:    {pct(sym_disjoint, n)}
file-disjoint    — naive file-level would get:  {pct(file_disjoint, n)}
textual conflict — pair-attributed 3-way fails: {pct(textual, n)}

oracle win   (same file, yet symbols disjoint): {pct(oracle_win, n)}
silent risk  (git merges clean, symbols COLLIDE): {pct(silent_risk, n)}
==========================================================================""")

    out_dir = pathlib.Path(args.out or pathlib.Path(__file__).parent / "workdir" / "results")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{name}_a.json"
    out_file.write_text(json.dumps({
        "repo": name, "since": args.since, "pairs": rows,
        "summary": {
            "n": n, "symbol_disjoint": sym_disjoint, "file_disjoint": file_disjoint,
            "textual_conflict": textual, "oracle_win": oracle_win,
            "silent_risk": silent_risk, "drift_excluded": drift,
        },
    }, indent=1))
    print(f"[{name}] wrote {out_file}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
