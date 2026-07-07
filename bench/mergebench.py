#!/usr/bin/env python3
"""MergeBench — replay a real agent fleet through two integration strategies.

Everything that can be real is real:
  - the changesets are a real uncoordinated fleet (phase0/agent_corpus.py),
  - per-changeset CI durations are measured with real pytest runs,
  - conflict structure is measured (528 analyzed pairs),
  - conflicting pairs land via the reconciler's stored regenerated merges,
  - the final cafecito-landed main is materialized and its combined test
    union is executed — the green-main invariant is checked, not assumed.

What is computed, and labeled as such: queue *schedules*. Given measured
per-changeset CI durations (and a parameterized full-suite duration for
projection), wall-clock and compute follow arithmetically from each
strategy's landing schedule:

  serial queue   — one candidate at a time; wall = Σ ci_i. Baseline agent
                   policy re-validates every in-flight changeset whenever
                   main moves: O(k²) compute.
  file-locking   — Perforce-style: pairs sharing any file serialize; waves
                   from greedy coloring of the file-overlap graph.
  cafecito       — waves from coloring of the ORACLE graph (symbol overlap
                   or textual conflict); commuting changesets land in
                   parallel, verification facts are memoized (re-validation
                   only on write-set intersection), true conflicts pay the
                   measured regeneration time and one re-verify.

Each cafecito wave also pays one integration verify of the wave's combined
state (compute: the wave's tests once more; wall: the slowest of them) —
the safety model is never assumed away.

Usage:
  python3 mergebench.py --repo ../phase0/workdir/repos/sympy [--ci-minutes 10]
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "phase0"))

import ast  # noqa: E402

from experiment_b import (MAX_REGIONS, PROMPT_HEADER, REGION_BLOCK_RE,  # noqa: E402
                          REGION_TEMPLATE, Region, context_of, diff3_segments,
                          run_reconciler)
from gitutil import git, git_rc, show  # noqa: E402
from validate_b import ensure_venv  # noqa: E402

AGENT_DIR = HERE.parent / "phase0" / "workdir" / "agent" / "sympy"
RESULTS_DIR = HERE.parent / "phase0" / "workdir" / "results" / "agent"
OUT_DIR = HERE / "results"


# ---------------------------------------------------------------- corpus ----

def load_corpus(repo: str):
    changesets = []
    for f in ("changesets-run1.json", "changesets.json"):
        p = AGENT_DIR / f
        if p.exists():
            changesets += [c for c in json.loads(p.read_text()) if c.get("head")]
    seen: set[str] = set()
    changesets = [c for c in changesets
                  if not (c["head"] in seen or seen.add(c["head"]))]

    pairs = {}
    for row in json.loads((RESULTS_DIR / "sympy_pairs.json").read_text()):
        key = frozenset((row["a"]["head"], row["b"]["head"]))
        pairs[key] = {"textual": row["textual_conflict"],
                      "sym": bool(row["symbol_overlap"]),
                      "file": bool(row["file_overlap"])}

    regens = {}
    for rec in json.loads((RESULTS_DIR / "sympy_b.json").read_text()):
        sim = rec["pair"]["sim"]
        key = frozenset((sim["ours"], sim["theirs"]))
        regens[key] = rec

    base = json.loads((RESULTS_DIR / "sympy_a.json").read_text())["since"]
    base = base.split("@")[1]
    base = git(repo, "rev-parse", base).strip()
    return changesets, pairs, regens, base


def tests_for(repo: str, cs: dict) -> list[str]:
    out = []
    for f in cs["files"]:
        p = pathlib.Path(f)
        cands = [f] if ("/tests/" in f or p.name.startswith("test_")) else \
                [str(p.parent / "tests" / f"test_{p.stem}.py")]
        for t in cands:
            code, _, _ = git_rc(repo, "cat-file", "-e", f"{cs['head']}:{t}")
            if code == 0 and t not in out:
                out.append(t)
    return out


# ------------------------------------------------------------ measurement ---

def run_tests(py, worktree: pathlib.Path, files: list[str], scratch, timeout=900):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(scratch),
           "PYTHONDONTWRITEBYTECODE": "1", "MPLBACKEND": "Agg", "LC_ALL": "C.UTF-8"}
    t0 = time.time()
    r = subprocess.run([str(py), "-m", "pytest", "-q", "--tb=line",
                        "-p", "no:cacheprovider", *files],
                       cwd=worktree, env=env, capture_output=True, text=True,
                       timeout=timeout)
    return r.returncode == 0, round(time.time() - t0, 2)


def measure_ci(repo: str, changesets: list[dict], py) -> None:
    """Real per-changeset CI: worktree at head, run its impact tests."""
    cache = OUT_DIR / "ci_measurements.json"
    if cache.exists():
        m = json.loads(cache.read_text())
        for c in changesets:
            c.update(m.get(c["head"], {}))
        if all("ci_s" in c for c in changesets):
            return
    scratch = pathlib.Path(tempfile.mkdtemp(prefix="mergebench-"))
    m = {}
    for i, c in enumerate(changesets):
        c["tests"] = tests_for(repo, c)
        wt = scratch / f"wt{i}"
        git(repo, "worktree", "add", "--detach", "--quiet", str(wt), c["head"])
        try:
            if c["tests"]:
                ok, dur = run_tests(py, wt, c["tests"], scratch)
            else:
                ok, dur = True, 0.5  # doc-only changeset: trivial check
            c["home_ok"], c["ci_s"] = ok, dur
        finally:
            git_rc(repo, "worktree", "remove", "--force", str(wt))
        m[c["head"]] = {"tests": c["tests"], "home_ok": ok, "ci_s": dur}
        print(f"  measured {i+1}/{len(changesets)}: {dur}s "
              f"{'ok' if ok else 'FAIL-AT-HOME'} — {c['title'][:50]}",
              file=sys.stderr)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(m, indent=1))


# ------------------------------------------------------------- scheduling ---

def color_waves(nodes: list[dict], edge) -> list[list[dict]]:
    """Greedy coloring, highest degree first; deterministic."""
    deg = {c["head"]: sum(edge(c, d) for d in nodes if d is not c) for c in nodes}
    waves: list[list[dict]] = []
    for c in sorted(nodes, key=lambda c: (-deg[c["head"]], c["idx"])):
        for w in waves:
            if not any(edge(c, d) for d in w):
                w.append(c)
                break
        else:
            waves.append([c])
    return waves


def schedule(nodes, pairs, regens, mode: str, ci):
    """Returns dict(wall, ci_compute, revalidation_compute, waves, regens)."""
    def rel(a, b):
        return pairs.get(frozenset((a["head"], b["head"])),
                         {"textual": False, "sym": False, "file": False})

    n = len(nodes)
    mean = sum(ci(c) for c in nodes) / n if n else 0.0
    if mode == "serial":
        wall = sum(ci(c) for c in nodes)
        compute = wall
        # naive fleet policy: every landing re-validates every in-flight cs
        reval = sum(i * mean for i in range(n))
        return {"wall": wall, "ci": compute, "reval": reval, "waves": n,
                "regens": 0}

    edge = ((lambda a, b: rel(a, b)["file"]) if mode == "filelock"
            else (lambda a, b: rel(a, b)["sym"] or rel(a, b)["textual"]))
    waves = color_waves(nodes, edge)
    wall = compute = regen_wall = 0.0
    nregen = 0
    landed: list[dict] = []
    for w in waves:
        wall += max(ci(c) for c in w)              # parallel lanes
        compute += sum(ci(c) for c in w)
        wall += max(ci(c) for c in w)              # wave integration verify
        compute += sum(ci(c) for c in w)
        for c in w:
            for d in landed:
                if rel(c, d)["textual"]:
                    rec = regens.get(frozenset((c["head"], d["head"])))
                    regen_wall += (rec or {}).get("elapsed_s", 20.0)
                    compute += ci(c)               # re-verify after regen
                    nregen += 1
                    break
        landed += w
    # memoized re-validation: only on write-set intersection with a landing
    reval = sum(mean for a in nodes for b in nodes
                if a["idx"] < b["idx"] and edge(a, b))
    return {"wall": wall + regen_wall, "ci": compute, "reval": reval,
            "waves": len(waves), "regens": nregen}


# ------------------------------------------------------------ real replay ---

def commit_files(repo: str, tree: str, parent: str, files: dict[str, str]) -> str:
    """New commit = `tree` with `files` overwritten. Pure plumbing, temp index."""
    with tempfile.TemporaryDirectory() as td:
        env = {**os.environ, "GIT_INDEX_FILE": str(pathlib.Path(td) / "idx")}
        def g(*args, inp=None):
            r = subprocess.run(["git", "-C", repo, *args], env=env, input=inp,
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"git {args[0]}: {r.stderr.strip()[:150]}")
            return r.stdout
        g("read-tree", tree)
        for path, content in files.items():
            blob = g("hash-object", "-w", "--stdin", inp=content).strip()
            g("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
        new_tree = g("write-tree").strip()
        return g("commit-tree", new_tree, "-p", parent,
                 "-m", "mergebench landing").strip()


def _test_defs(src: str) -> set[str]:
    return {l.strip().split("(")[0][4:].strip() for l in src.splitlines()
            if l.strip().startswith("def test_")}


def live_regen(repo, base, main, c, landed, conflicted, model="sonnet"):
    """Regenerate the colliding regions against CURRENT main (SPEC §5).

    Returns ({path: merged content}, regen_seconds) or (None, reason).
    3-way per file: base = fleet base, ours = accumulated main, theirs = c.
    """
    file_segments, sections, region_index = {}, [], []
    for p in sorted(conflicted):
        vb = show(repo, base, p) or ""
        va, vt = show(repo, main, p), show(repo, c["head"], p)
        if va is None or vt is None:
            return None, "add/delete conflict"
        segs = diff3_segments(vb, va, vt)
        if segs is None:
            continue
        regions = [i for i, s in enumerate(segs) if isinstance(s, Region)]
        if len(regions) > MAX_REGIONS:
            return None, "too many regions"
        file_segments[p] = segs
        for i in regions:
            n = len(region_index) + 1
            region_index.append((p, i))
            before, after = context_of(segs, i)
            seg = segs[i]
            sections.append(REGION_TEMPLATE.format(
                n=n, path=p, before=before,
                ours=seg.ours or "(deleted)\n", base=seg.base or "(empty)\n",
                theirs=seg.theirs or "(deleted)\n", after=after))
    if not region_index:
        return None, "no regions"
    touched = [d["title"] for d in landed
               if set(d["files"]) & set(file_segments)]
    prompt = PROMPT_HEADER.format(
        intent_a="\n".join(f"- {t}" for t in touched) or "- accumulated mainline",
        intent_b=f"- {c['title']}: {c['brief']}",
    ) + "".join(sections)
    if len(prompt) > 80_000:
        return None, "prompt too large"
    t0 = time.time()
    try:
        output = run_reconciler(prompt, model, 300)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        return None, f"reconciler: {str(e)[:80]}"
    blocks = {int(m.group(1)): m.group(2) for m in REGION_BLOCK_RE.finditer(output)}
    files = {}
    for n, (p, i) in enumerate(region_index, start=1):
        if n not in blocks:
            return None, "missing region in output"
        file_segments[p][i] = blocks[n]
    for p, segs in file_segments.items():
        merged = "".join(s if isinstance(s, str) else s.ours for s in segs)
        if p.endswith(".py"):
            try:
                ast.parse(merged)
            except SyntaxError:
                return None, "regen does not parse"
            union = _test_defs(show(repo, main, p) or "") | _test_defs(
                show(repo, c["head"], p) or "")
            if union - _test_defs(merged):
                return None, "shadowed test defs"
        files[p] = merged
    return (files, round(time.time() - t0, 1)), None


def real_landing(repo, base, waves, pairs, regens, py):
    """Actually land the fleet wave-by-wave with LIVE regeneration on
    conflict, a real test gate per landing candidate, and a final green-main
    verification. This is the product loop, executed for real."""
    main = base
    landed, escalated, regen_log = [], [], []
    gates = {"runs": 0, "seconds": 0.0}
    scratch = pathlib.Path(tempfile.mkdtemp(prefix="mergebench-land-"))
    t_start = time.time()

    def impact_of(paths, rev):
        out = set()
        for p in paths:
            if "/tests/" in p:
                out.add(p)
                continue
            t = str(pathlib.Path(p).parent / "tests" /
                    f"test_{pathlib.Path(p).stem}.py")
            if git_rc(repo, "cat-file", "-e", f"{rev}:{t}")[0] == 0:
                out.add(t)
        return out

    for w in waves:
        for c in w:
            code, out, _ = git_rc(repo, "merge-tree", "--write-tree",
                                  "--name-only", "--no-messages",
                                  f"--merge-base={base}", main, c["head"])
            regen_s, conflicted = None, set()
            if code == 0:
                tree = out.splitlines()[0].strip()
                candidate = git(repo, "commit-tree", tree, "-p", main,
                                "-m", f"land: {c['title'][:60]}").strip()
            elif code == 1:
                conflicted = {p for p in out.splitlines()[1:] if p}
                result, why = live_regen(repo, base, main, c, landed, conflicted)
                if result is None:
                    escalated.append((c, why))
                    continue
                files, regen_s = result
                tree = out.splitlines()[0].strip()
                candidate = commit_files(repo, tree, main, files)
            else:
                escalated.append((c, "merge error"))
                continue
            # The gate gates EVERY landing, clean merges included — a clean
            # textual merge can still break behavior (silent risk). Candidate
            # must pass the changeset's tests plus the impact tests of every
            # file it touches or conflicts on, for real, before landing.
            gate_tests = sorted(set(c["tests"])
                                | impact_of(set(c["files"]) | conflicted, candidate))
            wt = scratch / f"gate-{c['idx']}"
            git(repo, "worktree", "add", "--detach", "--quiet", str(wt), candidate)
            try:
                green, gate_s = run_tests(py, wt, gate_tests, scratch)
            finally:
                git_rc(repo, "worktree", "remove", "--force", str(wt))
            gates["runs"] += 1
            gates["seconds"] += gate_s
            if regen_s is not None:
                regen_log.append({"title": c["title"], "regen_s": regen_s,
                                  "gate_s": gate_s, "gate_green": green,
                                  "files": sorted(conflicted)})
            if not green:
                escalated.append((c, "failed landing gate"))
                continue
            main = candidate
            landed.append(c)

    union = sorted({t for c in landed for t in c["tests"]})
    wt = scratch / "final"
    git(repo, "worktree", "add", "--detach", "--quiet", str(wt), main)
    try:
        green, dur = run_tests(py, wt, union, scratch)
    finally:
        git_rc(repo, "worktree", "remove", "--force", str(wt))
    return {"landed": len(landed), "escalated": [(c["title"], why)
            for c, why in escalated], "final_commit": main,
            "green": green, "union_tests": len(union), "verify_s": dur,
            "regens": regen_log, "gates": gates,
            "landing_wall_s": round(time.time() - t_start, 1)}


# ----------------------------------------------------------------- charts ---

def svg_chart(series: dict[str, list[tuple[float, float]]], title: str,
              ylabel: str, path: pathlib.Path) -> None:
    W, H, ML, MB, MT, MR = 640, 400, 62, 46, 46, 16
    xs = [x for s in series.values() for x, _ in s]
    ys = [y for s in series.values() for _, y in s]
    xmax, ymax = max(xs), max(ys) * 1.08
    colors = {"serial queue": "#d62728", "file locking": "#ff9f40",
              "cafecito": "#2a9d8f"}
    def X(v): return ML + (W - ML - MR) * v / xmax
    def Y(v): return H - MB - (H - MB - MT) * v / ymax
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
             f'font-family="system-ui,sans-serif" font-size="12">',
             f'<rect width="{W}" height="{H}" fill="white"/>',
             f'<text x="{ML}" y="24" font-size="15" font-weight="bold">{title}</text>']
    for i in range(5):
        yv = ymax * i / 4
        parts.append(f'<line x1="{ML}" y1="{Y(yv)}" x2="{W-MR}" y2="{Y(yv)}" '
                     f'stroke="#e5e5e5"/>')
        parts.append(f'<text x="{ML-6}" y="{Y(yv)+4}" text-anchor="end">'
                     f'{yv/3600:.1f}h</text>' if ymax > 7200 else
                     f'<text x="{ML-6}" y="{Y(yv)+4}" text-anchor="end">'
                     f'{yv/60:.0f}m</text>')
    for xv in sorted({x for s in series.values() for x, _ in s}):
        parts.append(f'<text x="{X(xv)}" y="{H-MB+16}" text-anchor="middle">'
                     f'{int(xv)}</text>')
    parts.append(f'<text x="{(W+ML)/2}" y="{H-10}" text-anchor="middle" '
                 f'fill="#555">fleet size (changesets in burst)</text>')
    parts.append(f'<text x="16" y="{(H+MT)/2}" transform="rotate(-90 16 '
                 f'{(H+MT)/2})" text-anchor="middle" fill="#555">{ylabel}</text>')
    for name, pts in series.items():
        d = " ".join(f"{X(x):.1f},{Y(y):.1f}" for x, y in pts)
        c = colors.get(name, "#333")
        parts.append(f'<polyline points="{d}" fill="none" stroke="{c}" '
                     f'stroke-width="2.5"/>')
        lx, ly = X(pts[-1][0]), Y(pts[-1][1])
        parts.append(f'<circle cx="{lx}" cy="{ly}" r="3.5" fill="{c}"/>')
    for i, name in enumerate(series):
        c = colors.get(name, "#333")
        parts.append(f'<rect x="{ML+8}" y="{MT+6+i*18}" width="18" height="4" '
                     f'fill="{c}"/><text x="{ML+32}" y="{MT+12+i*18}">{name}</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts))


# ------------------------------------------------------------------- main ---

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(HERE.parent / "phase0/workdir/repos/sympy"))
    ap.add_argument("--ci-minutes", type=float, default=10.0,
                    help="full-suite CI duration for the projection")
    args = ap.parse_args()
    repo = str(pathlib.Path(args.repo).resolve())

    changesets, pairs, regens, base = load_corpus(repo)
    print(f"corpus: {len(changesets)} changesets @ {base[:12]}", file=sys.stderr)
    py = ensure_venv(HERE.parent / "phase0" / "workdir")
    measure_ci(repo, changesets, py)
    fleet = [c for c in changesets if c.get("home_ok")]
    print(f"green-at-home fleet: {len(fleet)}/{len(changesets)}", file=sys.stderr)
    for i, c in enumerate(fleet):
        c["idx"] = i

    D = args.ci_minutes * 60.0
    measured = lambda c: c["ci_s"]  # noqa: E731
    projected = lambda c: D        # noqa: E731

    ks = [k for k in (4, 8, 12, 16, 20, 24, 28, 33) if k <= len(fleet)]
    if ks[-1] != len(fleet):
        ks.append(len(fleet))
    curves = {m: {"wall": [], "total": []} for m in ("serial", "filelock", "cafecito")}
    table = []
    for k in ks:
        sub = fleet[:k]
        for mode in curves:
            s = schedule(sub, pairs, regens, mode, projected)
            curves[mode]["wall"].append((k, s["wall"]))
            curves[mode]["total"].append((k, s["ci"] + s["reval"]))
            if k == len(fleet):
                sm = schedule(sub, pairs, regens, mode, measured)
                table.append((mode, s, sm))

    waves = color_waves(fleet, lambda a, b: pairs.get(
        frozenset((a["head"], b["head"])), {}).get("sym") or pairs.get(
        frozenset((a["head"], b["head"])), {}).get("textual"))
    landing = real_landing(repo, base, waves, pairs, regens, py)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    names = {"serial": "serial queue", "filelock": "file locking",
             "cafecito": "cafecito"}
    svg_chart({names[m]: curves[m]["wall"] for m in curves},
              f"Time to land an agent burst (projected {args.ci_minutes:.0f}-min CI)",
              "wall-clock to all-landed", OUT_DIR / "mergebench_wall.svg")
    svg_chart({names[m]: curves[m]["total"] for m in curves},
              f"Total CI compute incl. agent re-validation ({args.ci_minutes:.0f}-min CI)",
              "CI compute consumed", OUT_DIR / "mergebench_compute.svg")

    n = len(fleet)
    print(f"""
== MergeBench · real fleet of {n} agent changesets · sympy @ {base[:12]} ====
real landing (cafecito waves): {landing['landed']}/{n} landed, """
          f"""{len(landing['escalated'])} escalated, """
          f"""{len(landing['regens'])} live regenerations
landing wall (real ops, measured): {landing['landing_wall_s']}s
final main GREEN: {landing['green']} ({landing['union_tests']} test files, """
          f"""{landing['verify_s']}s)

strategy      waves  regens   wall({args.ci_minutes:.0f}m CI)   wall(measured)   """
          """CI+reval compute""")
    for mode, s, sm in table:
        print(f"{names[mode]:13} {s['waves']:5} {s['regens']:7} "
              f"{s['wall']/3600:9.2f}h {sm['wall']:13.1f}s "
              f"{(s['ci']+s['reval'])/3600:12.1f}h")
    print(f"""
escalated: {landing['escalated'] or '—'}
charts: {OUT_DIR}/mergebench_wall.svg, mergebench_compute.svg
==========================================================================""")
    (OUT_DIR / "mergebench.json").write_text(json.dumps({
        "fleet": n, "base": base, "ci_minutes": args.ci_minutes,
        "curves": curves, "landing": landing,
        "table": [{"mode": m, "projected": s, "measured": sm}
                  for m, s, sm in table]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
