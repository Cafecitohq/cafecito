"""Input closures for test files — the sound half of memoization.

A verification fact `(closure hash, check) → verdict` is only inheritable if
the closure is COMPLETE: every repo file whose content can affect the test's
outcome. We compute it by resolving the test's import graph against the
candidate tree (no checkout, everything via git blobs) — `ast` for Python,
static specifier scanning for JS/TS, import-declaration scanning plus
whole-package membership for Go — together with the files each ecosystem's
runner injects (ancestor conftest.py / package+runner configs) and build
manifests.

The safety rule is absolute: any confusion — unparseable file, more imports
than the cap, an unresolvable relative specifier, resolution machinery we
can't see through statically (tsconfig `paths`/`baseUrl`, bundler aliases,
package `workspaces`/`imports`, go workspaces, nested modules, vendoring,
`go:embed`, cgo) — returns None, and the caller runs the test instead of
inheriting a fact. Over-inclusion is always safe (a spurious closure entry
merely re-runs a test); omission never is. Memoization is an optimization;
the gate is the gate.
"""

from __future__ import annotations

import ast
import posixpath
import re

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


# ------------------------------------------------------------------ JS / TS

_JS_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
# ESM-style TS: a specifier written "./x.js" may mean the source file x.ts
_JS_EXT_SWAP = {".js": (".ts", ".tsx"), ".jsx": (".tsx",),
                ".mjs": (".mts",), ".cjs": (".cts",)}

# match only the specifier tail, so multi-line import/export bodies don't
# matter; over-matching (comments, embedded code strings) is sound — it can
# only ADD closure entries, never hide one
_JS_SPECIFIER = re.compile(
    r"""\bfrom\s*["']([^"'\n]+)["']
      | \bimport\s*["']([^"'\n]+)["']
      | \bimport\s*\(\s*["']([^"'\n]+)["']\s*\)
      | \brequire\s*\(\s*["']([^"'\n]+)["']\s*\)""", re.VERBOSE)

# per-directory runner/build configs, walked like conftest.py ancestors
_JS_DIR_CONFIGS = (
    "package.json", "tsconfig.json", "jsconfig.json",
    "vitest.config.ts", "vitest.config.js", "vitest.config.mts",
    "vitest.config.mjs", "vite.config.ts", "vite.config.js",
    "vite.config.mts", "vite.config.mjs", "jest.config.ts",
    "jest.config.js", "jest.config.mjs", "jest.config.cjs",
    "jest.config.json", "babel.config.js", "babel.config.json", ".babelrc")
_JS_ROOT_MANIFESTS = ("package-lock.json", "pnpm-lock.yaml", "yarn.lock",
                      "bun.lock", "bun.lockb")
_TS_EXTENDS = re.compile(r'"extends"\s*:\s*"([^"]+)"')


def _js_config_confused(name: str, src: str) -> bool:
    """Does this config enable resolution we can't see through statically?
    tsconfig paths/baseUrl and bundler aliases map bare specifiers onto repo
    files; package workspaces/imports do the same via node_modules."""
    if name == "package.json":
        kws = ('"workspaces"', '"imports"')
    elif name in ("tsconfig.json", "jsconfig.json"):
        kws = ('"paths"', '"baseUrl"')
    else:
        kws = ("alias", "moduleNameMapper")
    return any(k in src for k in kws)


def _resolve_js(spec: str, importer_dir: str, listing: set[str]) -> str | None:
    """Repo path a relative specifier resolves to, per node/TS resolution:
    exact file, TS source for a .js-suffixed specifier, extension inference,
    directory index. None if nothing in the tree matches."""
    raw = posixpath.normpath(posixpath.join(importer_dir, spec))
    if raw.startswith(".."):
        return None
    cands = [raw]
    for ext, swaps in _JS_EXT_SWAP.items():
        if raw.endswith(ext):
            cands += [raw[: -len(ext)] + s for s in swaps]
    cands += [raw + e for e in _JS_EXTS]
    cands += [f"{raw}/index{e}" for e in _JS_EXTS]
    for c in cands:
        if c in listing:
            return c
    return None


def js_closure(repo: str, rev: str, test_path: str,
               listing: set[str]) -> frozenset[str] | None:
    """All repo files that can affect a JS/TS test file, or None."""
    if not test_path.endswith(_JS_EXTS):
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
        for m in _JS_SPECIFIER.finditer(src):
            spec = next(g for g in m.groups() if g)
            if spec.startswith(("./", "../")):
                resolved = _resolve_js(spec, posixpath.dirname(path), listing)
                if resolved is None:
                    return None       # relative but absent: generated? alias?
                if resolved.endswith(_JS_EXTS):
                    queue.append(resolved)
                else:
                    seen.add(resolved)    # css/json/asset leaf
            elif spec.startswith(("#", "/")):
                return None           # package-imports map / absolute path
            # bare specifier: external package — sound because any config
            # that could remap it onto repo files returns None below
    d = posixpath.dirname(test_path)
    while True:
        for name in _JS_DIR_CONFIGS:
            c = f"{d}/{name}" if d else name
            if c not in listing:
                continue
            cfg_src = show(repo, rev, c)
            if cfg_src is None or _js_config_confused(name, cfg_src):
                return None
            seen.add(c)
            if name in ("tsconfig.json", "jsconfig.json"):
                ext = _TS_EXTENDS.search(cfg_src)
                if ext and ext.group(1).startswith("."):
                    base = posixpath.normpath(posixpath.join(d, ext.group(1)))
                    if not base.endswith(".json"):
                        base += ".json"
                    base_src = show(repo, rev, base)
                    if base_src is None or '"paths"' in base_src \
                            or '"baseUrl"' in base_src:
                        return None
                    seen.add(base)
        if not d:
            break
        d = posixpath.dirname(d)
    for m in _JS_ROOT_MANIFESTS:
        if m in listing:
            seen.add(m)
    return frozenset(seen) if len(seen) <= MAX_CLOSURE else None


# ---------------------------------------------------------------------- Go

_GO_IMPORT_SINGLE = re.compile(r'^\s*import\s+(?:[\w.]+\s+)?["`]([^"`\n]+)["`]',
                               re.MULTILINE)
_GO_IMPORT_BLOCK = re.compile(r'^\s*import\s*\(([^)]*)\)',
                              re.MULTILINE | re.DOTALL)
_GO_BLOCK_PATH = re.compile(r'["`]([^"`\n]+)["`]')
_GO_MODULE = re.compile(r'^module\s+(\S+)', re.MULTILINE)
_GO_COMMENT = re.compile(r'//[^\n]*|/\*.*?\*/', re.DOTALL)


def _go_imports(src: str) -> list[str] | None:
    """Import paths declared by a .go file; None on cgo/embed, which pull in
    inputs (C sources, data files) we don't model. Comment stripping is safe
    for the import section: Go allows only comments between package clause
    and declarations, so no string literal can hide an import block there."""
    if "//go:embed" in src:
        return None
    code = _GO_COMMENT.sub(" ", src)
    out: list[str] = []
    for m in _GO_IMPORT_BLOCK.finditer(code):
        out.extend(p.group(1) for p in _GO_BLOCK_PATH.finditer(m.group(1)))
    out.extend(m.group(1) for m in _GO_IMPORT_SINGLE.finditer(code))
    if "C" in out:
        return None
    return out


def go_closure(repo: str, rev: str, test_path: str,
               listing: set[str]) -> frozenset[str] | None:
    """All repo files that can affect a Go test file, or None. Go compiles
    per package, so membership is by directory: the whole package rides
    along, and module-internal imports pull in whole packages."""
    if not test_path.endswith(".go"):
        return None
    if "go.work" in listing or any(p.startswith("vendor/") for p in listing):
        return None
    if any(p.endswith("/go.mod") for p in listing) or "go.mod" not in listing:
        return None                       # nested modules, or not a module
    mod_src = show(repo, rev, "go.mod")
    m = _GO_MODULE.search(mod_src or "")
    if not m:
        return None
    module = m.group(1)
    by_dir: dict[str, list[str]] = {}
    for p in listing:
        if p.endswith(".go"):
            by_dir.setdefault(posixpath.dirname(p), []).append(p)
    seen: set[str] = set()
    queue = [posixpath.dirname(test_path)]
    seen_dirs: set[str] = set()
    while queue:
        d = queue.pop()
        if d in seen_dirs:
            continue
        seen_dirs.add(d)
        files = by_dir.get(d)
        if not files:
            return None                   # imported package with no sources
        for f in files:
            seen.add(f)
            if len(seen) > MAX_CLOSURE:
                return None
            src = show(repo, rev, f)
            if src is None:
                return None
            imports = _go_imports(src)
            if imports is None:
                return None
            for imp in imports:
                if imp == module:
                    queue.append("")
                elif imp.startswith(module + "/"):
                    queue.append(imp[len(module) + 1:])
    for man in ("go.mod", "go.sum"):
        if man in listing:
            seen.add(man)
    return frozenset(seen)


# ---------------------------------------------------------------- dispatch

def input_closure(repo: str, rev: str, test_path: str,
                  listing: set[str]) -> frozenset[str] | None:
    """The language-appropriate closure for `test_path`, or None (test runs,
    no fact inherited or recorded) for anything we can't analyze."""
    if test_path.endswith(".py"):
        return python_closure(repo, rev, test_path, listing)
    if test_path.endswith(_JS_EXTS):
        return js_closure(repo, rev, test_path, listing)
    if test_path.endswith(".go"):
        return go_closure(repo, rev, test_path, listing)
    return None
