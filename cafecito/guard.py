"""cafecito guard — the plane enforces itself inside the agent's harness.

`init` ships availability (`.mcp.json`), persuasion (the landing stanza), and
post-hoc detection (the post-commit hook, CI's plane-sync job). Nothing
intervened at the moment an agent went wrong, so a human was the enforcement
layer: notice, undo, re-prompt, redo. This is the missing piece.

Run as a harness hook. Payload in on stdin, decision out on stdout:

    python3 -m cafecito.guard          # what a hook registration invokes

Four checks, dispatched off the payload:

  PreToolUse + a file path   deny unless a live lease covers the path
  PreToolUse + a command     deny a push that would carry unlanded commits;
                             nudge a commit taken with no lease held
  Stop                       block once when leases are held and work is dirty
  SessionStart               inject live plane state (survives compaction,
                             which the static stanza does not)

PORTABILITY. Claude Code and Codex CLI share this wire format — Codex's engine
struct is literally named `ClaudeHooksEngine` and every wire type is
`rename_all = "camelCase"`. Two rules keep one program working on both, and
neither may be relaxed:

  * ALLOW IS SILENCE. Exit 0 with no stdout. Codex rejects an explicit
    `permissionDecision: "allow"` (also `"ask"`, `continue: false`, and
    `updatedInput`). Silence is also the fastest path, which is why the hot
    path was written this way before portability required it.
  * EVERY DENY CARRIES A NON-EMPTY REASON. Codex hard-errors without one.

Tool *names* differ between harnesses, so nothing here matches on them. The
edit and shell gates are chosen by payload shape — `tool_input.file_path` vs
`tool_input.command` — which is harness-agnostic by construction.

FAIL OPEN, LOUDLY. Any error at all — unreadable state, a bug in this file —
allows the action and reports itself in `systemMessage`. A guard that fails
closed on its own bug makes every edit in the repo impossible, which is a worse
outage than the drift it prevents. The post-commit hook and CI's plane-sync job
remain the backstop.

Escape hatches: `CAFECITO_GUARD=off`, or `guard.enabled: false` in
`.cafecito/config.json`.

KNOWN GAP: the gate asks whether *a* live lease covers the path, not whether
*this session* holds it, because a hook payload carries a `session_id` while
leases are keyed by the agent id the agent chose at `reserve` time. Binding the
two is follow-up work. The gate's job is making sure `reserve` is called at all;
adjudicating between agents belongs to `reserve` and the landing gate, and
file-granular ownership checks here would contradict symbol-level commuting.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time

from .keys import keys_overlap

DEFAULTS = {
    "enabled": True,
    "protected_refs": ["main", "cafecito/main"],
    "renew_within_s": 300,
}
DEFAULT_TTL_S = 900
DEFAULT_BRANCH = "cafecito/main"
SKIP_DIRS = (".git", ".cafecito")
# Local git operations are left alone: they are reversible and invisible to
# anyone else. `push` is the drift event, so that is where the check lives.
WRITE_OPS = {"push"}


# --------------------------------------------------------------- plane state ---

def find_repo(start: str) -> pathlib.Path | None:
    """Nearest ancestor holding a `.cafecito/` — agents edit from subdirectories."""
    try:
        here = pathlib.Path(start).resolve()
    except OSError:
        return None
    for d in (here, *here.parents):
        if (d / ".cafecito").is_dir():
            return d
    return None


def _read_json(path: pathlib.Path, fallback):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return fallback


def config(repo: pathlib.Path) -> dict:
    cfg = _read_json(repo / ".cafecito" / "config.json", {})
    guard = {**DEFAULTS, **(cfg.get("guard") or {})}
    guard["lease_ttl_s"] = cfg.get("lease_ttl_s", DEFAULT_TTL_S)
    guard["branch"] = cfg.get("branch", DEFAULT_BRANCH)
    guard["test_cmd"] = cfg.get("test_cmd") or []
    return guard


def live_leases(repo: pathlib.Path, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    leases = _read_json(repo / ".cafecito" / "leases.json", {})
    if not isinstance(leases, dict):
        return {}
    return {k: v for k, v in leases.items()
            if isinstance(v, dict) and v.get("expires", 0) > now}


def covering(leases: dict, rel: str) -> dict:
    probe = f"file:{rel}"
    return {k: v for k, v in leases.items() if keys_overlap(probe, k)}


def renew(repo: pathlib.Path, keys, ttl: int) -> None:
    """Extend leases the agent is demonstrably still using.

    The 900s TTL was written for a world where nothing observed the agent
    between `reserve` and `submit`; left alone it would deny an agent its own
    files mid-task. Activity is liveness. Best-effort: the lock is taken
    non-blocking and skipped if busy, because a renewal is an optimization and
    must never stall an edit.
    """
    import fcntl

    path = repo / ".cafecito" / "leases.json"
    try:
        with open(repo / ".cafecito" / "lock", "a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return
            leases = _read_json(path, {})
            expires = time.time() + ttl
            for k in keys:
                if k in leases:
                    leases[k]["expires"] = expires
            path.write_text(json.dumps(leases, indent=1))
    except OSError:
        return


# ------------------------------------------------------------------ decisions ---

def deny(reason: str) -> dict:
    """The only decision shape both harnesses accept. Reason must be non-empty:
    Codex hard-errors on a deny without one, and an agent that is told to stop
    without being told what to do instead goes and asks a human."""
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def context(text: str) -> dict:
    return {"additionalContext": text}


def edit_gate(repo, rel, cfg, leases) -> dict | None:
    held = covering(leases, rel)
    if held:
        near = time.time() + cfg["renew_within_s"]
        stale = [k for k, v in held.items() if v.get("expires", 0) < near]
        if stale:
            renew(repo, stale, cfg["lease_ttl_s"])
        return None
    return deny(
        f"{rel} is not reserved. cafecito leases paths before edits so parallel "
        f"agents surface contention before the work is done, not after. Reserve "
        f"it, then retry:\n"
        f'  reserve(keys=["file:{rel}"], agent="<your id>", intent="<one line>")\n'
        f"Read-only work needs no lease. If the cafecito tools are not in this "
        f"session, say so rather than editing around the plane.")


def git_ops(command: str) -> list[list[str]]:
    """Token runs following each `git` in a shell command, split on separators."""
    import shlex
    try:
        toks = shlex.split(command, comments=True)
    except ValueError:
        toks = command.split()
    ops: list[list[str]] = []
    cur: list[str] | None = None
    for t in toks:
        if t in ("&&", "||", ";", "|", "&"):
            if cur is not None:
                ops.append(cur)
            cur = None
        elif t == "git" or t.endswith("/git"):
            if cur is not None:
                ops.append(cur)
            cur = []
        elif cur is not None:
            cur.append(t)
    if cur is not None:
        ops.append(cur)
    return ops


def subcommand(op: list[str]) -> str:
    for t in op:
        if not t.startswith("-"):
            return t
    return ""


def unlanded(repo: pathlib.Path, ref: str, branch: str) -> bool:
    """True when `ref` carries commits the plane never gated.

    This is the whole shell gate. Pushing `main` right after
    `git merge --ff-only cafecito/main` is the documented deploy step and must
    stay allowed; pushing a `main` that has drifted ahead of the landed branch
    is the drift event. The difference is ancestry, so ask git rather than
    guessing from the branch name.
    """
    import subprocess
    try:
        r = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ref, branch],
            cwd=str(repo), capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False       # can't tell -> fail open
    return r.returncode == 1


def shell_gate(repo, command, cfg, leases) -> dict | None:
    protected = set(cfg["protected_refs"])
    branch = cfg["branch"]
    for op in git_ops(command):
        sub = subcommand(op)
        if sub in WRITE_OPS:
            refs = [t for t in op if t in protected]
            bad = [r for r in refs if unlanded(repo, r, branch)]
            if bad:
                return deny(
                    f"{', '.join(bad)} carries commits that never went through "
                    f"the plane, so this push would bypass the landing gate. "
                    f"Land the work first (commit, then submit the sha); "
                    f"`submit` advances {branch}, and pushing after that is the "
                    f"normal deploy step.")
        elif sub == "commit" and not leases:
            return context(
                "No cafecito lease is held for this work. Committing is fine — "
                "landing is commit-then-submit — but submit the sha through the "
                "plane rather than pushing it, or the gate never runs.")
    return None


def stop_gate(repo, payload, leases) -> tuple[int, str] | None:
    if payload.get("stop_hook_active"):
        return None                      # loop breaker, non-negotiable
    if not leases:
        return None
    import subprocess
    try:
        r = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo),
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    # `.cafecito/` is the plane's own state. It is gitignored in repos that
    # `init` touched, but nothing guarantees that, and untracked plane state
    # would otherwise block every single Stop.
    dirty = [ln for ln in r.stdout.splitlines()
             if ln[3:].strip().strip('"') and
             not ln[3:].strip().strip('"').startswith(".cafecito/")]
    if r.returncode != 0 or not dirty:
        return None
    paths = sorted({key.split(":", 1)[-1].split("::")[0] for key in leases})
    return 2, (
        f"cafecito: you hold {len(leases)} lease(s) on {', '.join(paths[:4])} "
        f"and the worktree still has uncommitted changes. Commit and submit the "
        f"sha so the gate runs, or say what you are leaving unlanded and why.")


def session_context(repo, cfg, leases) -> str:
    state = _read_json(repo / ".cafecito" / "state.json", {})
    tip = (state.get("tip") or "unknown")[:12]
    lines = [f"cafecito plane: tip {tip} on {cfg['branch']}."]
    if cfg["test_cmd"]:
        # Basename the interpreter — a venv path eats the whole line and the
        # agent needs the runner, not where it lives.
        cmd = [pathlib.Path(cfg["test_cmd"][0]).name, *cfg["test_cmd"][1:]]
        shown = " ".join(cmd)
        lines.append(f"Landing gate: {shown[:80]}"
                     + ("…" if len(shown) > 80 else ""))
    if leases:
        by_agent: dict[str, list[str]] = {}
        for k, v in leases.items():
            by_agent.setdefault(v.get("agent", "?"), []).append(k)
        held = "; ".join(f"{a}: {len(ks)} key(s)" for a, ks in
                         sorted(by_agent.items()))
        lines.append(f"Active leases — {held}. Reserve before editing those.")
    else:
        lines.append("No active leases. Reserve paths before editing them.")
    return " ".join(lines)


# ----------------------------------------------------------------- dispatch ---

def decide(payload: dict, repo_hint: str) -> tuple[int, dict | None, str | None]:
    """-> (exit code, stdout JSON or None, stderr or None)."""
    event = payload.get("hook_event_name") or ""
    repo = find_repo(payload.get("cwd") or repo_hint or os.getcwd())
    if repo is None:
        return 0, None, None                      # no plane here
    cfg = config(repo)
    if not cfg["enabled"]:
        return 0, None, None
    leases = live_leases(repo)

    if event == "SessionStart":
        return 0, context(session_context(repo, cfg, leases)), None

    if event == "Stop":
        blocked = stop_gate(repo, payload, leases)
        return (blocked[0], None, blocked[1]) if blocked else (0, None, None)

    if event != "PreToolUse":
        return 0, None, None

    tool_input = payload.get("tool_input") or {}
    path = tool_input.get("file_path")
    if isinstance(path, str) and path:
        rel = _relative(repo, path)
        if rel is None:
            return 0, None, None
        return 0, edit_gate(repo, rel, cfg, leases), None

    command = tool_input.get("command")
    if isinstance(command, str) and command:
        return 0, shell_gate(repo, command, cfg, leases), None

    return 0, None, None


def _relative(repo: pathlib.Path, path: str) -> str | None:
    """Repo-relative path, or None when the guard should not care about it."""
    try:
        p = pathlib.Path(path)
        p = p if p.is_absolute() else (repo / p)
        rel = p.resolve().relative_to(repo)
    except (OSError, ValueError):
        return None
    parts = rel.parts
    if not parts or parts[0] in SKIP_DIRS:
        return None
    return rel.as_posix()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if os.environ.get("CAFECITO_GUARD") == "off":
        return 0
    repo_hint = ""
    if "--repo" in argv:
        i = argv.index("--repo")
        if i + 1 < len(argv):
            repo_hint = argv[i + 1]
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0
        code, out, err = decide(payload, repo_hint)
    except Exception as e:                                       # noqa: BLE001
        # Fail open, loudly. Never let a bug in here stop an agent working.
        json.dump({"systemMessage": f"cafecito guard failed open: {e}"},
                  sys.stdout)
        return 0
    if err:
        sys.stderr.write(err)
    if out:
        json.dump(out, sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
