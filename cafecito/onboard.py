"""Applying cafecito to a project.

Three things stand between a fresh repo and a working plane, and every one of
them used to be the operator's homework: knowing the gate command, registering
the MCP server so agent sessions can find the plane, and keeping the tip
following commits made outside it. `cafecito init` does all three.

Detection is evidence-based and always explained — a plane whose gate collects
no tests lands everything unverified, so init says what it found and how it
knows, and warns loudly when it finds nothing.
"""

from __future__ import annotations

import json
import pathlib
import stat

from .gitutil import git_rc

SKIP_DIRS = {".git", ".cafecito", "node_modules", "vendor", "dist", "build",
             "target", "__pycache__", ".venv", "venv", ".tox", ".next",
             ".pytest_cache", ".mypy_cache", "coverage", ".idea", ".vscode"}

MCP_FILE = ".mcp.json"
HOOK_MARKER = "cafecito advance"

# A post-commit hook is per-clone (.git/hooks is never committed), so init
# installs it. It must never fail a commit and never fire off the deploy
# branch: advance only follows commits the landed tip is an ancestor of.
HOOK_SCRIPT = """#!/bin/sh
# Installed by `cafecito init` — keeps the control plane's tip following
# commits made outside it, so the next landing bases on real history.
# Silent unless it advances; never fails a commit. CAFECITO_NO_HOOK=1 skips.
[ -n "$CAFECITO_NO_HOOK" ] && exit 0
command -v cafecito >/dev/null 2>&1 || exit 0
branch=$(git symbolic-ref --quiet --short HEAD 2>/dev/null) || exit 0
case "$branch" in
  main|master|trunk) ;;
  *) exit 0 ;;
esac
root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
[ -d "$root/.cafecito" ] || exit 0
cafecito advance --repo "$root" --to HEAD >/dev/null 2>&1 || true
exit 0
"""

_TEST_SUFFIXES = {
    "py": ("test_", "_test.py"),
    "js": (".test.", ".spec."),
    "go": ("_test.go",),
}


def _read_json(path: pathlib.Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _walk(root: pathlib.Path, cap: int = 4000):
    """Repo files worth counting — skips the dependency and build noise that
    would otherwise drown a project's real test files."""
    seen = 0
    for path in root.rglob("*"):
        if seen >= cap:
            return
        if not path.is_file():
            continue
        if SKIP_DIRS & set(path.relative_to(root).parts):
            continue
        seen += 1
        yield path


def _count_tests(files: list[pathlib.Path]) -> dict[str, int]:
    counts = {"py": 0, "js": 0, "go": 0}
    for p in files:
        name = p.name
        if name.endswith(".py") and (name.startswith("test_")
                                     or name.endswith("_test.py")):
            counts["py"] += 1
        elif name.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")) \
                and (".test." in name or ".spec." in name):
            counts["js"] += 1
        elif name.endswith("_test.go"):
            counts["go"] += 1
    return counts


def _js_package(root: pathlib.Path, files: list[pathlib.Path]):
    """The package.json that owns the test script: root first, then one level
    down (the app-in-a-subdir layout is common)."""
    candidates = [root / "package.json"]
    candidates += sorted(p for p in files
                         if p.name == "package.json"
                         and len(p.relative_to(root).parts) == 2)
    for path in candidates:
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        script = (data.get("scripts") or {}).get("test", "")
        if script and "no test specified" not in script:
            return path, script
    return None, ""


def detect_project(repo: str) -> dict:
    """How this project tests itself, with the evidence for saying so.

    Returns test_cmd/setup_cmd/generated/gate_families plus `language`,
    `evidence`, and `test_files` (0 means the gate would have no signal —
    the caller must warn)."""
    root = pathlib.Path(repo).resolve()
    files = list(_walk(root))
    names = {str(p.relative_to(root)) for p in files}
    counts = _count_tests(files)
    found: list[dict] = []

    pkg_path, pkg_script = _js_package(root, files)
    if pkg_path is not None:
        prefix = pkg_path.parent.relative_to(root).as_posix()
        cmd = ["npm", "test", "--silent"]
        setup = ["npm", "ci"] if (pkg_path.parent / "package-lock.json").exists() \
            else ["npm", "install"]
        if prefix != ".":
            cmd += ["--prefix", prefix]
            setup += ["--prefix", prefix]
        lock = (f"{prefix}/package-lock.json" if prefix != "."
                else "package-lock.json")
        gen_cmd = ["npm", "install", "--package-lock-only"]
        if prefix != ".":
            gen_cmd += ["--prefix", prefix]
        found.append({
            "language": "js", "test_cmd": cmd, "setup_cmd": setup,
            "gate_families": ["js"],
            "generated": ({lock: gen_cmd}
                          if (pkg_path.parent / "package-lock.json").exists()
                          else {}),
            "test_files": counts["js"],
            "score": counts["js"] * 10 + 3,
            "evidence": [f"{pkg_path.relative_to(root)} scripts.test = "
                         f"{pkg_script!r}",
                         f"{counts['js']} test file(s)"],
        })

    py_manifest = next((n for n in ("pyproject.toml", "setup.py", "setup.cfg",
                                    "requirements.txt") if n in names), "")
    if py_manifest or counts["py"]:
        found.append({
            "language": "py",
            "test_cmd": ["python3", "-m", "pytest", "-q", "--tb=line",
                         "-p", "no:cacheprovider"],
            "setup_cmd": [], "gate_families": ["py"], "generated": {},
            "test_files": counts["py"],
            "score": counts["py"] * 10 + (2 if py_manifest else 0),
            "evidence": ([f"{py_manifest}"] if py_manifest else [])
            + [f"{counts['py']} test file(s)"],
        })

    if "go.mod" in names:
        found.append({
            "language": "go", "test_cmd": ["go", "test", "./..."],
            "setup_cmd": [], "gate_families": ["go"], "generated": {},
            "test_files": counts["go"],
            "score": counts["go"] * 10 + 2,
            "evidence": ["go.mod", f"{counts['go']} test file(s)"],
        })

    if "Cargo.toml" in names:
        found.append({
            "language": "rust", "test_cmd": ["cargo", "test"],
            "setup_cmd": [], "gate_families": [], "generated": {},
            "test_files": 0, "score": 1,
            "evidence": ["Cargo.toml",
                         "symbol analysis falls back to file granularity"],
        })

    if not found:
        return {"language": None, "test_cmd": None, "setup_cmd": [],
                "gate_families": [], "generated": {}, "test_files": 0,
                "evidence": ["no test runner detected"]}
    best = max(found, key=lambda c: c["score"])
    best["also_found"] = [c["language"] for c in found
                          if c["language"] != best["language"]]
    return best


def write_mcp_registration(repo: str, command: str = "cafecito") -> tuple[str, str]:
    """Register the plane in a checked-in .mcp.json so every session, clone,
    and worktree of this repo finds it — local-scope `claude mcp add` binds to
    one directory on one machine, which is how sessions end up silently
    committing around the plane. Merge-safe: other servers are preserved."""
    path = pathlib.Path(repo).resolve() / MCP_FILE
    entry = {"command": command, "args": ["serve", "--repo", "."]}
    if path.exists():
        data = _read_json(path)
        if not isinstance(data, dict):
            return "conflict", f"{MCP_FILE} exists but is not valid JSON"
        servers = data.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            return "conflict", f"{MCP_FILE} has a non-object mcpServers"
        if servers.get("cafecito") == entry:
            return "present", str(path)
        servers["cafecito"] = entry
        path.write_text(json.dumps(data, indent=2) + "\n")
        return "updated", str(path)
    path.write_text(json.dumps({"mcpServers": {"cafecito": entry}},
                               indent=2) + "\n")
    return "created", str(path)


def install_post_commit_hook(repo: str) -> tuple[str, str]:
    """Absorb bypass traffic: commits made without the plane (a maintainer on
    main, a GitHub web edit pulled down, another agent session with no MCP)
    move the tip instead of silently stranding it behind main."""
    code, out, _ = git_rc(repo, "rev-parse", "--git-path", "hooks")
    if code != 0:
        return "skipped", "no git hooks directory"
    hooks = pathlib.Path(repo).resolve() / out.strip()
    if not hooks.is_absolute():
        hooks = pathlib.Path(repo).resolve() / hooks
    hook = hooks / "post-commit"
    if hook.exists():
        existing = ""
        try:
            existing = hook.read_text()
        except OSError:
            pass
        if HOOK_MARKER in existing:
            return "present", str(hook)
        return "conflict", (f"{hook} exists — add this line to it: "
                            f"cafecito advance --repo \"$(git rev-parse "
                            f"--show-toplevel)\" --to HEAD >/dev/null 2>&1 || true")
    hooks.mkdir(parents=True, exist_ok=True)
    hook.write_text(HOOK_SCRIPT)
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return "installed", str(hook)


def mcp_registered(repo: str) -> bool:
    data = _read_json(pathlib.Path(repo).resolve() / MCP_FILE)
    return bool(isinstance(data, dict)
                and (data.get("mcpServers") or {}).get("cafecito"))


def hook_installed(repo: str) -> bool:
    code, out, _ = git_rc(repo, "rev-parse", "--git-path", "hooks")
    if code != 0:
        return False
    hook = pathlib.Path(repo).resolve() / out.strip() / "post-commit"
    try:
        return HOOK_MARKER in hook.read_text()
    except OSError:
        return False
