"""cafecito doctor / gc — operator diagnostics and housekeeping.

`doctor` checks the environment and the control plane's health and prints a
report (exit 0 = healthy or warnings only; 1 = errors). `gc` cleans what real
usage accumulates: orphaned engine worktrees, expired leases, dead in-flight
entries, and an oversized facts store.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time

from .engine import Engine
from .facts import MAX_FACTS
from .gitutil import git_rc

OK, WARN, ERR = "ok", "warn", "error"


def _check(name: str, status: str, detail: str = "") -> dict:
    return {"name": name, "status": status, "detail": detail}


def _git_version() -> tuple[int, int] | None:
    r = subprocess.run(["git", "--version"], capture_output=True, text=True)
    m = re.search(r"(\d+)\.(\d+)", r.stdout or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _engine_worktrees(repo: str) -> list[str]:
    """Engine-created worktrees (cafecito-* temp paths) still registered."""
    code, out, _ = git_rc(repo, "worktree", "list", "--porcelain")
    if code != 0:
        return []
    paths = [l.split(" ", 1)[1] for l in out.splitlines()
             if l.startswith("worktree ")]
    return [p for p in paths if "/cafecito-" in p]


def collect_checks(repo: str) -> list[dict]:
    checks: list[dict] = []

    v = _git_version()
    if v is None:
        checks.append(_check("git", ERR, "git not found"))
    elif v < (2, 38):
        checks.append(_check("git", ERR,
                             f"{v[0]}.{v[1]} < 2.38 (merge-tree --write-tree)"))
    else:
        checks.append(_check("git", OK, f"{v[0]}.{v[1]}"))

    for tool, needed_for in (("claude", "regeneration, swarm workers"),
                             ("gh", "PR ingest")):
        if shutil.which(tool):
            checks.append(_check(tool, OK, ""))
        else:
            checks.append(_check(tool, WARN, f"not on PATH — {needed_for} "
                                             f"unavailable"))

    try:
        eng = Engine(repo)
    except RuntimeError as exc:
        checks.append(_check("engine", ERR, str(exc)))
        return checks

    tip = eng._tip()
    code, ref, _ = git_rc(eng.repo, "rev-parse",
                          f"refs/heads/{eng.config['branch']}")
    if code != 0:
        checks.append(_check("landed branch", ERR,
                             f"{eng.config['branch']} missing"))
    elif ref.strip() != tip:
        checks.append(_check("landed branch", ERR,
                             f"ref {ref.strip()[:10]} != state tip {tip[:10]}"))
    else:
        checks.append(_check("landed branch", OK,
                             f"{eng.config['branch']} @ {tip[:10]}"))

    argv0 = (eng.config.get("test_cmd") or [""])[0]
    if shutil.which(argv0):
        checks.append(_check("test_cmd", OK, argv0))
    else:
        checks.append(_check("test_cmd", ERR, f"{argv0!r} not executable"))

    stale_wts = _engine_worktrees(eng.repo)
    dead = [p for p in stale_wts
            if not subprocess.run(["test", "-d", p]).returncode == 0]
    checks.append(_check(
        "worktrees", WARN if stale_wts else OK,
        f"{len(stale_wts)} engine worktree(s) registered"
        + (f", {len(dead)} with missing dirs" if dead else "")
        + (" — run `cafecito gc`" if stale_wts else "")))

    leases = eng._leases()
    infl = eng._inflight()
    checks.append(_check("leases", OK, f"{len(leases)} active"))
    checks.append(_check("inflight", OK, f"{len(infl)} gating"))
    facts_file = eng.state_dir / "facts.json"
    try:
        n_facts = len(json.loads(facts_file.read_text()))
    except (OSError, ValueError):
        n_facts = 0
    checks.append(_check("facts", WARN if n_facts > MAX_FACTS else OK,
                         f"{n_facts} verification facts"))
    return checks


def run_doctor(args) -> int:
    checks = collect_checks(args.repo)
    glyph = {OK: "✓", WARN: "!", ERR: "✗"}
    for c in checks:
        print(f"  {glyph[c['status']]} {c['name']:14} {c['detail']}")
    errors = sum(1 for c in checks if c["status"] == ERR)
    warns = sum(1 for c in checks if c["status"] == WARN)
    print(f"\n{len(checks)} checks: {errors} error(s), {warns} warning(s)")
    return 1 if errors else 0


def run_gc(args) -> int:
    eng = Engine(args.repo)
    report: dict[str, int] = {}

    before = _engine_worktrees(eng.repo)
    git_rc(eng.repo, "worktree", "prune")
    for path in _engine_worktrees(eng.repo):
        git_rc(eng.repo, "worktree", "remove", "--force", path)
    git_rc(eng.repo, "worktree", "prune")
    report["worktrees removed"] = len(before) - len(_engine_worktrees(eng.repo))

    with eng._lock():
        raw = {}
        try:
            raw = json.loads((eng.state_dir / "leases.json").read_text())
        except (OSError, ValueError):
            pass
        live = {k: v for k, v in raw.items()
                if v.get("expires", 0) > time.time()}
        report["expired leases dropped"] = len(raw) - len(live)
        eng._save_leases(live)

        infl_raw = {}
        try:
            infl_raw = json.loads((eng.state_dir / "inflight.json").read_text())
        except (OSError, ValueError):
            pass
        live_infl = eng._inflight()
        report["dead inflight dropped"] = len(infl_raw) - len(live_infl)
        eng._save_inflight(live_infl)

    for k, v in report.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("use: cafecito doctor | cafecito gc")
