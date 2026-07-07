"""Regenerative merge — the reconciler, extracted from phase0/experiment_b.py
and bench/mergebench.py where it was validated (12/14 semantic PASS on the
agent corpus; MergeBench landed 30/33 with green main).

When a changeset collides with the landed tip, nobody resolves the conflict:
the colliding regions are re-derived from both sides' intents by a fresh
agent, then the result must pass the landing gate. Region-scoped — real
conflicts cluster in large hotspot files.
"""

from __future__ import annotations

import ast
import pathlib
import re
import subprocess
import tempfile
import time

from gitutil import show

MAX_REGIONS = 8
MAX_PROMPT = 80_000
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

INTENT OF CHANGE A (already landed):
{intent_a}

INTENT OF CHANGE B (the incoming changeset):
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


def diff3_segments(base: str, ours: str, theirs: str) -> list | None:
    """git merge-file --diff3 parsed into alternating text/Region segments.
    None if the file merges cleanly after all."""
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

    segments: list = []
    plain: list[str] = []
    state = None
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


def _context_of(segments: list, idx: int) -> tuple[str, str]:
    before = segments[idx - 1] if idx > 0 and isinstance(segments[idx - 1], str) else ""
    after = segments[idx + 1] if idx + 1 < len(segments) and isinstance(segments[idx + 1], str) else ""
    return ("".join(before.splitlines(keepends=True)[-CONTEXT_LINES:]),
            "".join(after.splitlines(keepends=True)[:CONTEXT_LINES]))


def _test_defs(src: str) -> set[str]:
    return {l.strip().split("(")[0][4:].strip() for l in src.splitlines()
            if l.strip().startswith("def test_")}


def run_reconciler(prompt: str, model: str, timeout: int = 300) -> str:
    r = subprocess.run(["claude", "-p", "--model", model],
                       input=prompt, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {r.stderr.strip()[:200]}")
    return r.stdout


def live_regen(repo: str, base: str, tip: str, head: str, conflicted: set[str],
               intent_landed: str, intent_incoming: str, model: str = "sonnet"):
    """Regenerate colliding regions of `head` against the landed `tip`.

    Returns ({path: merged content}, seconds) or (None, reason). Includes the
    shadowing guard: same-named test defs must survive by name (they shadow
    silently in Python otherwise).
    """
    file_segments, sections, region_index = {}, [], []
    for p in sorted(conflicted):
        vb = show(repo, base, p) or ""
        va, vt = show(repo, tip, p), show(repo, head, p)
        if va is None or vt is None:
            return None, "add/delete conflict"
        segs = diff3_segments(vb, va, vt)
        if segs is None:
            continue
        regions = [i for i, s in enumerate(segs) if isinstance(s, Region)]
        if len(regions) > MAX_REGIONS:
            return None, "too many conflict regions"
        file_segments[p] = segs
        for i in regions:
            n = len(region_index) + 1
            region_index.append((p, i))
            before, after = _context_of(segs, i)
            seg = segs[i]
            sections.append(REGION_TEMPLATE.format(
                n=n, path=p, before=before,
                ours=seg.ours or "(deleted)\n", base=seg.base or "(empty)\n",
                theirs=seg.theirs or "(deleted)\n", after=after))
    if not region_index:
        return None, "no regenerable regions"
    prompt = PROMPT_HEADER.format(intent_a=intent_landed or "- accumulated mainline",
                                  intent_b=intent_incoming) + "".join(sections)
    if len(prompt) > MAX_PROMPT:
        return None, "prompt too large"
    t0 = time.time()
    try:
        output = run_reconciler(prompt, model)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        return None, f"reconciler: {str(e)[:120]}"
    blocks = {int(m.group(1)): m.group(2) for m in REGION_BLOCK_RE.finditer(output)}
    files = {}
    for n, (p, i) in enumerate(region_index, start=1):
        if n not in blocks:
            return None, "missing region in reconciler output"
        file_segments[p][i] = blocks[n]
    for p, segs in file_segments.items():
        merged = "".join(s if isinstance(s, str) else s.ours for s in segs)
        if p.endswith(".py"):
            try:
                ast.parse(merged)
            except SyntaxError:
                return None, "regenerated file does not parse"
            union = _test_defs(show(repo, tip, p) or "") | _test_defs(show(repo, head, p) or "")
            if union - _test_defs(merged):
                return None, "shadowed test defs"
        files[p] = merged
    return (files, round(time.time() - t0, 1)), None
