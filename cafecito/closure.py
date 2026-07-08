"""Input closures for Python test files — the sound half of memoization.

A verification fact `(closure hash, check) → verdict` is only inheritable if
the closure is COMPLETE: every repo file whose content can affect the test's
outcome. We compute it by resolving the test's import graph with `ast`
against the candidate tree (no checkout, everything via git blobs), plus the
files pytest itself injects (ancestor conftest.py) and root build manifests.

The safety rule is absolute: any confusion — unparseable file, more imports
than the cap, anything unresolvable that *might* be a repo file — returns
None, and the caller runs the test instead of inheriting a fact. Memoization
is an optimization; the gate is the gate.
"""

from __future__ import annotations

import ast
import posixpath

from .gitutil import show

MAX_CLOSURE = 200

ROOT_MANIFESTS = ("pyproject.toml", "setup.cfg", "setup.py", "requirements.txt",
                  "conftest.py", "tox.ini", "pytest.ini")


def _module_candidates(module: str) -> list[str]:
    base = module.replace(".", "/")
    return [f"{base}.py", f"{base}/__init__.py"]


def _imports_of(source: str, importer: str) -> list[str] | None:
    """Repo-relative path candidates imported by `source` (file at `importer`).
    None if the source doesn't parse."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None
    out: list[str] = []
    pkg_dir = posixpath.dirname(importer)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.extend(_module_candidates(alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import: resolve against the importer
                anchor = pkg_dir
                for _ in range(node.level - 1):
                    anchor = posixpath.dirname(anchor)
                base = f"{anchor}/{node.module.replace('.', '/')}" if node.module \
                    else anchor
                base = base.strip("/")
                out.extend([f"{base}.py", f"{base}/__init__.py"])
                for alias in node.names:
                    out.extend([f"{base}/{alias.name}.py",
                                f"{base}/{alias.name}/__init__.py"])
            else:
                if node.module:
                    out.extend(_module_candidates(node.module))
                    for alias in node.names:
                        out.extend(_module_candidates(f"{node.module}.{alias.name}"))
    return out


def python_closure(repo: str, rev: str, test_path: str,
                   listing: set[str]) -> frozenset[str] | None:
    """All repo files (present in `listing` for `rev`) that can affect
    `test_path`, or None if analysis can't be trusted."""
    if not test_path.endswith(".py"):
        return None
    seen: set[str] = set()
    queue = [test_path]
    while queue:
        path = queue.pop()
        if path in seen:
            continue
        seen.add(path)
        if len(seen) > MAX_CLOSURE:
            return None
        src = show(repo, rev, path)
        if src is None:
            return None
        cands = _imports_of(src, path)
        if cands is None:
            return None
        for c in cands:
            if c in listing and c not in seen:
                queue.append(c)
    # pytest injects ancestor conftest.py files; build config affects collection
    d = posixpath.dirname(test_path)
    while True:
        c = f"{d}/conftest.py" if d else "conftest.py"
        if c in listing:
            seen.add(c)
        if not d:
            break
        d = posixpath.dirname(d)
    for m in ROOT_MANIFESTS:
        if m in listing:
            seen.add(m)
    return frozenset(seen)
