#!/usr/bin/env python3
"""Experiment A with the PRODUCT oracle (multi-language write sets).

Same methodology as experiment_a.py — branch reconstruction (mine.py),
pair-attributed conflicts (attrib.py) — but write sets come from the shipped
`cafecito.writeset` (Python ast + js/ts/go span scanners) instead of the
frozen phase0 copy. New script rather than a flag on the old one: phase0
experiments stay frozen so published numbers remain reproducible bit-for-bit.

Extra per-run stat: symbol coverage — the fraction of changed code files the
oracle analyzed at symbol level (vs. whole-file fallback), the number that
tells us whether the language scanners are actually engaging.

Usage:
  python3 experiment_a_multilang.py --repo workdir/repos/prometheus
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from attrib import attributed_merge  # noqa: E402
from mine import concurrent_pairs, is_dependent, mine_branches  # noqa: E402

from cafecito.writeset import write_set  # noqa: E402


def pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:5.1f}%" if d else "  n/a"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--since", default="2024-06-01")
    ap.add_argument("--max-pairs", type=int, default=400)
    args = ap.parse_args()

    repo = str(pathlib.Path(args.repo).resolve())
    name = pathlib.Path(repo).name
    t0 = time.time()

    branches = mine_branches(repo, args.since)
    pairs = concurrent_pairs(branches, args.max_pairs)
    print(f"[{name}] {len(branches)} branches, {len(pairs)} pairs sampled",
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
        if att.status == "error":
            skipped += 1
            continue
        if att.status == "drift":
            drift += 1
            continue
        sym_a, files_a = sets_for(a)
        sym_b, files_b = sets_for(b)
        rows.append({
            "textual_conflict": att.status == "conflict",
            "file_overlap": bool(files_a & files_b),
            "symbol_overlap": bool(sym_a & sym_b),
        })
        if (i + 1) % 100 == 0:
            print(f"[{name}] …{i + 1}/{len(pairs)}", file=sys.stderr)

    # symbol coverage: how often did the scanners engage vs file: fallback?
    code_exts = (".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go")
    analyzed = fallback = 0
    for syms, files in ws_cache.values():
        code_files = {f for f in files if f.endswith(code_exts)}
        fell_back = {s[5:] for s in syms if s.startswith("file:")}
        analyzed += len(code_files - fell_back)
        fallback += len(code_files & fell_back)

    n = len(rows)
    sym_dis = sum(1 for r in rows if not r["symbol_overlap"])
    file_dis = sum(1 for r in rows if not r["file_overlap"])
    textual = sum(1 for r in rows if r["textual_conflict"])
    win = sum(1 for r in rows if r["file_overlap"] and not r["symbol_overlap"])
    risk = sum(1 for r in rows if not r["textual_conflict"] and r["symbol_overlap"])
    cov_d = analyzed + fallback
    print(f"""
== experiment A (product oracle, multi-language) · {name} ================
branches {len(branches)} · pairs {n} (skipped {skipped}, drift {drift}) · {time.time()-t0:.0f}s
symbol coverage: {pct(analyzed, cov_d)} of changed code files analyzed at symbol level

symbol-disjoint (lands in parallel):   {pct(sym_dis, n)}
file-disjoint:                         {pct(file_dis, n)}
pair-attributed textual conflicts:     {pct(textual, n)}
oracle win (same file, disjoint syms): {pct(win, n)}
silent risk (clean merge, syms collide): {pct(risk, n)}
==========================================================================""")
    out = pathlib.Path(__file__).parent / "workdir" / "results" / f"{name}_a_multilang.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"repo": name, "n": n, "symbol_disjoint": sym_dis,
                               "file_disjoint": file_dis, "textual": textual,
                               "oracle_win": win, "silent_risk": risk,
                               "coverage_analyzed": analyzed,
                               "coverage_fallback": fallback}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
