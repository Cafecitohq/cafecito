#!/usr/bin/env python3
"""Experiment B — can a fresh agent regenerate real merge conflicts away?

Takes the textually-conflicting pairs found by find_conflicts.py (or
experiment A). No one resolves the conflict: a reconciler agent regenerates
the colliding code from both sides' intents.

Regeneration is REGION-SCOPED (this matches SPEC §5): the first corpus showed
real conflicts cluster in large hotspot files (80-200KB), so whole-file
regeneration doesn't fly. We produce a diff3 conflict file with
`git merge-file`, hand the reconciler each conflict region (ours/base/theirs)
with surrounding context plus both branches' intents, and splice its
replacement regions back into the file.

v0 validation (honest about its limits — see README):
  parse          — the spliced full file must be valid Python
  incorporation  — per region, fraction of each side's added lines present in
                   the replacement; a pair passes if every region's
                   min(side A, side B) ≥ 0.6 and every file parses. Proxy for
                   "both intents survived", NOT semantic correctness.

Full validation (run both sides' test suites against the merged state) needs a
sandboxed runner; that's the next milestone, tracked in README.

Requires the `claude` CLI on PATH (headless mode).

Usage:
  python3 find_conflicts.py --repo workdir/repos/sympy
  python3 experiment_b.py   --repo workdir/repos/sympy \
                            --results workdir/results/conflicts [--max-pairs 4]
"""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import time

from gitutil import git, show

MAX_FILES = 3          # conflicted files per pair (v0)
MAX_REGIONS = 8        # conflict regions per file (v0)
MAX_PROMPT = 80_000    # bytes; guard against pathological regions
CONTEXT_LINES = 25

REGION_BLOCK_RE = re.compile(r"===REGION (\d+)===\n(.*?)===END REGION===", re.DOTALL)

PROMPT_HEADER = """\
You are a reconciler agent performing a REGENERATIVE MERGE. Two changes were \
developed in parallel from a common BASE and collide in the regions below. Do \
NOT pick one side and do NOT output conflict markers. For each region, write \
the code as if a single author had implemented BOTH change sets' intents \
together. Match the surrounding style and indentation exactly; the replacement \
is spliced verbatim between the given context lines.

Output ONLY the regions, each wrapped exactly like:
===REGION <n>===
<replacement lines for region n>
===END REGION===

INTENT OF CHANGE A (commit messages):
{intent_a}

INTENT OF CHANGE B (commit messages):
{intent_b}
"""

REGION_TEMPLATE = """
################ REGION {n} · file: {path} ################
----- context before -----
{before}
----- side A version -----
{ours}
----- common BASE version -----
{base}
----- side B version -----
{theirs}
----- context after -----
{after}
"""


class Region:
    __slots__ = ("ours", "base", "theirs")

    def __init__(self, ours: str, base: str, theirs: str):
        self.ours, self.base, self.theirs = ours, base, theirs


def diff3_segments(base: str, ours: str, theirs: str) -> list[str | Region] | None:
    """Run git merge-file --diff3; parse into alternating text/Region segments.

    Returns None if the merge is clean (no conflict in this file after all).
    """
    with tempfile.TemporaryDirectory() as td:
        paths = []
        for tag, content in (("ours", ours), ("base", base), ("theirs", theirs)):
            p = pathlib.Path(td) / tag
            p.write_text(content, errors="replace")
            paths.append(str(p))
        r = subprocess.run(
            ["git", "merge-file", "-p", "--diff3", "-L", "A", "-L", "BASE", "-L", "B",
             paths[0], paths[1], paths[2]],
            capture_output=True, text=True, errors="replace",
        )
        if r.returncode == 0:
            return None
        if r.returncode < 0 or r.returncode > 127:
            raise RuntimeError(f"git merge-file failed: {r.stderr.strip()[:200]}")
        merged = r.stdout

    segments: list[str | Region] = []
    plain: list[str] = []
    state = None  # None | "ours" | "base" | "theirs"
    bufs = {"ours": [], "base": [], "theirs": []}
    for line in merged.splitlines(keepends=True):
        if line.startswith("<<<<<<<"):
            segments.append("".join(plain))
            plain, state = [], "ours"
        elif line.startswith("|||||||") and state == "ours":
            state = "base"
        elif line.startswith("=======") and state in ("ours", "base"):
            state = "theirs"
        elif line.startswith(">>>>>>>") and state == "theirs":
            segments.append(Region(*("".join(bufs[k]) for k in ("ours", "base", "theirs"))))
            bufs = {"ours": [], "base": [], "theirs": []}
            state = None
        elif state:
            bufs[state].append(line)
        else:
            plain.append(line)
    segments.append("".join(plain))
    return segments


def context_of(segments: list, idx: int) -> tuple[str, str]:
    before = segments[idx - 1] if idx > 0 and isinstance(segments[idx - 1], str) else ""
    after = segments[idx + 1] if idx + 1 < len(segments) and isinstance(segments[idx + 1], str) else ""
    before_lines = before.splitlines(keepends=True)[-CONTEXT_LINES:]
    after_lines = after.splitlines(keepends=True)[:CONTEXT_LINES]
    return "".join(before_lines), "".join(after_lines)


def added_lines(base: str, changed: str) -> set[str]:
    base_set = {l.strip() for l in base.splitlines()}
    return {l.strip() for l in changed.splitlines()
            if len(l.strip()) > 3 and l.strip() not in base_set}


def usable_paths(row: dict) -> list[str] | None:
    paths = row.get("conflicted") or []
    if not paths or len(paths) > MAX_FILES or not all(p.endswith(".py") for p in paths):
        return None
    return paths


def intents(repo: str, base: str, head: str, cap: int = 15) -> str:
    lines = git(repo, "log", "--format=- %s", f"{base}..{head}").splitlines()[:cap]
    return "\n".join(lines) or "- (no commit messages)"


def run_reconciler(prompt: str, model: str, timeout: int) -> str:
    r = subprocess.run(["claude", "-p", "--model", model],
                       input=prompt, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {r.stderr.strip()[:200]}")
    return r.stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--max-pairs", type=int, default=4)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--results", default=None,
                    help="directory holding <repo>_a.json (from find_conflicts.py)")
    args = ap.parse_args()

    repo = str(pathlib.Path(args.repo).resolve())
    name = pathlib.Path(repo).name
    results_dir = pathlib.Path(args.results or pathlib.Path(__file__).parent
                               / "workdir" / "results" / "conflicts")
    a_file = results_dir / f"{name}_a.json"
    if not a_file.exists():
        print(f"run find_conflicts.py first — missing {a_file}", file=sys.stderr)
        return 1

    conflicts = [r for r in json.loads(a_file.read_text())["pairs"] if r["textual_conflict"]]
    print(f"[{name}] {len(conflicts)} textually-conflicting pairs available", file=sys.stderr)

    results = []
    for row in conflicts:
        if len(results) >= args.max_pairs:
            break
        paths = usable_paths(row)
        if paths is None:
            continue
        # Pair-attributed 3-way (see attrib.py): ours holds one branch's
        # changes replayed onto the other's base; intents map via `replayed`.
        sim = row["sim"]
        base_rev, ours_rev, theirs_rev = sim["base"], sim["ours"], sim["theirs"]
        if sim["replayed"] == row["a"]["head"]:
            side_ours, side_theirs = row["a"], row["b"]
        else:
            side_ours, side_theirs = row["b"], row["a"]

        # Build per-file segment lists and the flat region list for the prompt.
        file_segments: dict[str, list] = {}
        region_index: list[tuple[str, int]] = []  # (path, segment idx) per region n
        region_sections = []
        ok = True
        for p in paths:
            vb = show(repo, base_rev, p) or ""
            va, vt = show(repo, ours_rev, p), show(repo, theirs_rev, p)
            if va is None or vt is None:
                ok = False  # add/delete conflict — out of scope v0
                break
            segs = diff3_segments(vb, va, vt)
            if segs is None:
                continue  # merge-tree flagged it, merge-file didn't — treat as clean
            regions = [i for i, s in enumerate(segs) if isinstance(s, Region)]
            if not regions or len(regions) > MAX_REGIONS:
                ok = False
                break
            file_segments[p] = segs
            for i in regions:
                n = len(region_index) + 1
                region_index.append((p, i))
                before, after = context_of(segs, i)
                seg = segs[i]
                region_sections.append(REGION_TEMPLATE.format(
                    n=n, path=p, before=before, ours=seg.ours or "(side A deleted this)\n",
                    base=seg.base or "(empty in BASE)\n",
                    theirs=seg.theirs or "(side B deleted this)\n", after=after))
        if not ok or not region_index:
            continue

        prompt = PROMPT_HEADER.format(
            intent_a=intents(repo, side_ours["base"], side_ours["head"]),
            intent_b=intents(repo, side_theirs["base"], side_theirs["head"]),
        ) + "".join(region_sections)
        if len(prompt) > MAX_PROMPT:
            continue

        print(f"[{name}] reconciling {len(region_index)} region(s) in {list(file_segments)} "
              f"({row['a']['subject'][:40]!r} × {row['b']['subject'][:40]!r})…", file=sys.stderr)
        t0 = time.time()
        rec = {"pair": row, "paths": paths, "regions": len(region_index), "model": args.model}
        try:
            output = run_reconciler(prompt, args.model, args.timeout)
            blocks = {int(m.group(1)): m.group(2) for m in REGION_BLOCK_RE.finditer(output)}
            region_scores, missing = [], False
            for n, (p, i) in enumerate(region_index, start=1):
                if n not in blocks:
                    missing = True
                    continue
                seg = file_segments[p][i]
                repl_set = {l.strip() for l in blocks[n].splitlines()}
                fracs = []
                for side in (seg.ours, seg.theirs):
                    added = added_lines(seg.base, side)
                    fracs.append(1.0 if not added else len(added & repl_set) / len(added))
                region_scores.append(round(min(fracs), 3))
                file_segments[p][i] = blocks[n]  # splice
            parse_ok = True
            if not missing:
                for p, segs in file_segments.items():
                    merged = "".join(s if isinstance(s, str) else s.ours for s in segs)
                    try:
                        ast.parse(merged)
                    except SyntaxError:
                        parse_ok = False
            rec.update({
                "elapsed_s": round(time.time() - t0, 1),
                "merged_regions": {n: blocks.get(n) for n in range(1, len(region_index) + 1)},
                "missing_regions": missing,
                "parse_ok": parse_ok,
                "region_scores": region_scores,
                "pass": (not missing) and parse_ok and bool(region_scores)
                        and min(region_scores) >= 0.6,
            })
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            rec["error"] = str(e)[:300]
            rec["pass"] = False
        results.append(rec)
        print(f"[{name}]   → {'PASS' if rec['pass'] else 'FAIL'} "
              f"scores={rec.get('region_scores')} ({rec.get('elapsed_s', '?')}s)",
              file=sys.stderr)

    n = len(results)
    passed = sum(1 for r in results if r["pass"])
    print(f"""
== cafecito phase 0 · experiment B · {name} ==============================
conflicting pairs attempted:   {n}
regenerative merge PASS (v0):  {passed}/{n}
  (PASS = spliced file parses + every region incorporates ≥60% of BOTH
   sides' added lines; dual test-suite execution is the next milestone)
==========================================================================""")

    out_file = results_dir / f"{name}_b.json"
    out_file.write_text(json.dumps(results, indent=1))
    print(f"[{name}] wrote {out_file}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
