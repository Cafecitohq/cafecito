#!/usr/bin/env python3
"""Build Experiment B's corpus: hunt for genuinely-conflicting concurrent pairs.

Textual conflicts are rare (~1-2% of concurrent pairs), so random sampling in
experiment A yields too few. This scans ALL interval-overlapping pairs, cheaply
pre-filters to those sharing at least one changed file, and runs the real
3-way merge check only on those.

Output matches experiment A's JSON shape, written under a separate directory so
experiment_b.py can consume it via --results without touching A's results:

  python3 find_conflicts.py --repo workdir/repos/numpy
  python3 experiment_b.py   --repo workdir/repos/numpy \
                            --results workdir/results/conflicts
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

from attrib import attributed_merge
from gitutil import git_rc
from mine import is_dependent, mine_branches


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--since", default="2024-06-01")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    repo = str(pathlib.Path(args.repo).resolve())
    name = pathlib.Path(repo).name
    t0 = time.time()

    branches = mine_branches(repo, args.since)
    print(f"[{name}] {len(branches)} branches", file=sys.stderr)

    file_sets: dict[str, frozenset[str]] = {}
    for br in branches:
        code, out, _ = git_rc(repo, "diff", "--name-only", br.base, br.head)
        file_sets[br.head] = frozenset(out.splitlines()) if code == 0 else frozenset()

    candidates = [
        (a, b)
        for i, a in enumerate(branches)
        for b in branches[i + 1:]
        if a.start < b.end and b.start < a.end
        and file_sets[a.head] & file_sets[b.head]
    ]
    print(f"[{name}] {len(candidates)} concurrent pairs share files; merge-checking…",
          file=sys.stderr)

    rows = []
    for i, (a, b) in enumerate(candidates):
        if is_dependent(repo, a, b):
            continue
        att = attributed_merge(repo, a, b)
        if att.status == "conflict":
            rows.append({
                "a": {"head": a.head, "base": a.base, "merge": a.merge, "subject": a.subject},
                "b": {"head": b.head, "base": b.base, "merge": b.merge, "subject": b.subject},
                "sim": {"ours": att.ours, "theirs": att.theirs, "base": att.base,
                        "replayed": att.replayed},
                "conflicted": att.conflicted,
                "textual_conflict": True,
                "file_overlap": sorted(file_sets[a.head] & file_sets[b.head]),
                "symbol_overlap": [],  # not computed here; B doesn't need it
            })
        if (i + 1) % 500 == 0:
            print(f"[{name}] …{i + 1}/{len(candidates)} ({len(rows)} conflicts)",
                  file=sys.stderr)

    out_dir = pathlib.Path(args.out or pathlib.Path(__file__).parent
                           / "workdir" / "results" / "conflicts")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{name}_a.json"
    out_file.write_text(json.dumps({"repo": name, "since": args.since, "pairs": rows,
                                    "summary": {"n": len(rows)}}, indent=1))
    print(f"[{name}] {len(rows)} conflicting pairs in {time.time() - t0:.0f}s → {out_file}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
