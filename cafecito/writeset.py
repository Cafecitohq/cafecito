"""Write-set derivation: which symbols does a change touch?

v0 granularity:
  - Python files: innermost enclosing def/class of each changed line, via `ast`
    (qualified name, e.g. `py:pkg/mod.py::Class.method`). Lines outside any
    def/class attribute to `py:path::<module>`.
  - Everything else (and any file that fails to parse): whole file, `file:path`.

Uncertainty always widens the write set — a parse failure degrades to file
granularity, never to "no symbols".
"""

from __future__ import annotations

import ast
import re

from .gitutil import git, show
from .spans import LANG_BY_EXT, PREFIX_BY_EXT, symbol_spans

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

Range = tuple[int, int]


def diff_ranges(repo: str, base: str, head: str) -> dict[str, dict]:
    """Parse `git diff -U0 base head` into per-file changed line ranges.

    Returns {path: {"new": [Range in head version], "old": [Range in base
    version], "binary": bool, "old_path_missing": bool}}.
    """
    out = git(repo, "diff", "--no-renames", "-U0", base, head)
    files: dict[str, dict] = {}
    cur: dict | None = None
    for line in out.splitlines():
        if line.startswith("diff --git "):
            # `diff --git a/<path> b/<path>` (renames disabled, paths match)
            path = line.split(" b/", 1)[-1]
            cur = files.setdefault(path, {"new": [], "old": [], "binary": False})
        elif cur is None:
            continue
        elif line.startswith("Binary files "):
            cur["binary"] = True
        else:
            m = HUNK_RE.match(line)
            if m:
                old_start, old_n = int(m.group(1)), int(m.group(2) or "1")
                new_start, new_n = int(m.group(3)), int(m.group(4) or "1")
                if new_n > 0:
                    cur["new"].append((new_start, new_start + new_n - 1))
                if old_n > 0:
                    cur["old"].append((old_start, old_start + old_n - 1))
    return files


def python_symbols(source: str) -> list[tuple[str, int, int]]:
    """All def/class spans in `source` as (qualname, start_line, end_line).

    Raises SyntaxError on unparseable source (caller degrades to file level).
    """
    tree = ast.parse(source)
    out: list[tuple[str, int, int]] = []

    def collect(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = prefix + child.name
                start = child.lineno
                if child.decorator_list:
                    start = min(d.lineno for d in child.decorator_list)
                out.append((qual, start, child.end_lineno or child.lineno))
                collect(child, qual + ".")

    collect(tree, "")
    return out


def _attribute(path: str, ranges: list[Range], symbols: list[tuple[str, int, int]]) -> set[str]:
    """Map changed line ranges to the innermost enclosing symbol (Python)."""
    return _attribute_lang(path, ranges, symbols, "py")


def _attribute_lang(path: str, ranges: list[Range],
                    symbols: list[tuple[str, int, int]], prefix: str) -> set[str]:
    touched: set[str] = set()
    for lo, hi in ranges:
        best: tuple[int, str] | None = None  # (span_size, qualname) — smallest span wins
        hit_any = False
        for qual, s, e in symbols:
            if s <= hi and lo <= e:
                hit_any = True
                span = e - s
                if best is None or span < best[0]:
                    best = (span, qual)
        if best is not None:
            touched.add(f"{prefix}:{path}::{best[1]}")
        if not hit_any:
            touched.add(f"{prefix}:{path}::<module>")
    return touched


def write_set(repo: str, base: str, head: str) -> tuple[frozenset[str], frozenset[str]]:
    """(symbol write set, changed file set) for the change base..head."""
    symbols: set[str] = set()
    files: set[str] = set()
    parsed_cache: dict[tuple[str, str], list | None] = {}

    def syms_at(rev: str, path: str, lang: str) -> list | None:
        key = (rev, path)
        if key not in parsed_cache:
            src = show(repo, rev, path)
            if src is None:
                parsed_cache[key] = None
            elif lang == "python":
                try:
                    parsed_cache[key] = python_symbols(src)
                except (SyntaxError, ValueError, RecursionError):
                    parsed_cache[key] = None
            else:
                try:
                    parsed_cache[key] = symbol_spans(src, lang)
                except (ValueError, RecursionError):
                    parsed_cache[key] = None
        return parsed_cache[key]

    for path, info in diff_ranges(repo, base, head).items():
        files.add(path)
        dot = path.rfind(".")
        ext = path[dot:] if dot != -1 else ""
        lang = LANG_BY_EXT.get(ext)
        if info["binary"] or lang is None:
            symbols.add(f"file:{path}")
            continue
        prefix = PREFIX_BY_EXT[ext]
        resolved = False
        for rev, side in ((head, "new"), (base, "old")):
            if not info[side]:
                continue
            table = syms_at(rev, path, lang)
            if table is None:
                # deleted/added file on this side, or unanalyzable → try other side
                continue
            symbols |= _attribute_lang(path, info[side], table, prefix)
            resolved = True
        if not resolved:
            symbols.add(f"file:{path}")
    return frozenset(symbols), frozenset(files)
