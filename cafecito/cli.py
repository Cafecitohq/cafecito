"""The cafecito command line.

  cafecito init      apply the plane to a repo: gate, MCP registration, hook
  cafecito serve     run the MCP server on stdio (what agents connect to)
  cafecito submit    land a committed changeset from the shell
  cafecito status    tip, counts, recent landings, active leases
  cafecito log       the landed log
  cafecito advance   follow out-of-band commits (move tip to a descendant)
  cafecito swarm     one goal in, a parallel fleet out: plan, build, land
  cafecito ingest    land open GitHub PRs through the plane (the gateway)
  cafecito watch     live dashboard of the fleet and the landed log
  cafecito doctor    environment + control-plane health checks
  cafecito gc        clean stale worktrees, leases, in-flight entries
  cafecito version
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time

from . import __version__
from .engine import DEFAULT_CONFIG, Engine
from .mcp_server import serve
from .onboard import (detect_project, install_post_commit_hook,
                      write_mcp_registration)


def _engine(args) -> Engine:
    return Engine(args.repo)


def cmd_init(args) -> int:
    """One command from a fresh repo to a working plane: configure the gate,
    register the MCP server, install the tip-following hook."""
    eng = Engine(args.repo)
    config_existed = (eng.state_dir / "config.json").exists()
    changed = False
    detected = None

    # Detection fills what the operator didn't state, and never overwrites a
    # plane someone already configured (re-running init must be safe).
    default_gate = eng.config["test_cmd"] == DEFAULT_CONFIG["test_cmd"]
    if not args.test_cmd and (default_gate or args.redetect):
        detected = detect_project(eng.repo)
        if detected["test_cmd"]:
            eng.config["test_cmd"] = detected["test_cmd"]
            eng.config["gate_families"] = detected["gate_families"]
            if detected["setup_cmd"] and not eng.config.get("setup_cmd"):
                eng.config["setup_cmd"] = detected["setup_cmd"]
            for pat, cmd in (detected["generated"] or {}).items():
                eng.config.setdefault("generated", {}).setdefault(pat, cmd)
            changed = True

    if args.branch:
        eng.config["branch"] = args.branch
        changed = True
    if args.test_cmd:
        eng.config["test_cmd"] = shlex.split(args.test_cmd)
        changed = True
    if args.require_signal:
        eng.config["require_signal"] = True
        changed = True
    if args.setup_cmd:
        eng.config["setup_cmd"] = shlex.split(args.setup_cmd)
        changed = True
    for spec in args.generated or []:
        pat, _, cmd = spec.partition("=")
        if not cmd:
            print(f"--generated expects PATTERN=COMMAND, got {spec!r}")
            return 2
        eng.config.setdefault("generated", {})[pat] = shlex.split(cmd)
        changed = True
    if changed or not config_existed:
        (eng.state_dir / "config.json").write_text(json.dumps(eng.config, indent=1))
        eng = Engine(args.repo)  # re-read; branch ref follows on next landing

    st = eng.status(limit=1)
    print(f"cafecito {__version__} on {eng.repo}")
    print(f"  landed branch : {eng.config['branch']}")
    print(f"  tip           : {st['tip'][:12]}")
    print(f"  gate command  : {' '.join(eng.config['test_cmd'])}")
    if detected and detected["test_cmd"]:
        print(f"                  detected {detected['language']} — "
              f"{'; '.join(detected['evidence'])}")
        if detected.get("also_found"):
            print(f"                  also present: "
                  f"{', '.join(detected['also_found'])} "
                  f"(override with --test-cmd)")
    if eng.config.get("setup_cmd"):
        print(f"  setup command : {' '.join(eng.config['setup_cmd'])}")

    if not args.no_mcp:
        verdict, detail = write_mcp_registration(eng.repo)
        note = {"created": "written — commit it so every clone gets the plane",
                "updated": "cafecito added alongside your other servers",
                "present": "already registered",
                "conflict": detail}[verdict]
        print(f"  mcp server    : .mcp.json {note}")
    if not args.no_hook:
        verdict, detail = install_post_commit_hook(eng.repo)
        note = {"installed": "post-commit — the tip follows commits made "
                             "outside the plane",
                "present": "post-commit already installed",
                "skipped": detail, "conflict": detail}[verdict]
        print(f"  advance hook  : {note}")

    # A gate that collects nothing lands everything unverified. Say so.
    if detected is not None and not detected["test_cmd"]:
        print("\n  ! no test runner detected — the gate has no signal and every"
              "\n    landing would be unverified. Set one with"
              "\n      cafecito init --test-cmd \"<your test command>\""
              "\n    and consider --require-signal to refuse blind landings.")
    elif detected is not None and detected["test_files"] == 0:
        print(f"\n  ! {detected['language']} project with 0 test files — the gate"
              f"\n    will report no signal until tests exist. The first thing to"
              f"\n    land in a repo like this is test coverage.")

    print("\nnext: restart your agent session (or approve the server when asked),")
    print("then agents coordinate through sync / reserve / submit / status.")
    print("check anytime with: cafecito doctor")
    return 0


def cmd_serve(args) -> int:
    return serve(_engine(args))


def cmd_sync(args) -> int:
    r = _engine(args).sync(agent=args.agent, create_worktree=args.worktree)
    print(json.dumps(r, indent=1))
    return 0


def cmd_submit(args) -> int:
    r = _engine(args).submit(args.ref, agent=args.agent, title=args.title or "")
    print(json.dumps(r, indent=1))
    return 0 if r["verdict"] == "landed" else 1


def cmd_status(args) -> int:
    st = _engine(args).status(limit=args.n)
    print(f"tip       {st['tip'][:12]}  on {st['branch']}")
    print(f"landed    {st['landed']}    escalated {st['escalated']}")
    if st["active_leases"]:
        print("leases:")
        for k, v in st["active_leases"].items():
            print(f"  {k}  held by {v['agent']}  ({v.get('intent', '')})")
    if st["recent"]:
        print("recent:")
        for e in st["recent"]:
            gate = e.get("gate") or {}
            extra = " no-signal" if gate.get("no_signal") else ""
            extra += f" regen {e['regen_s']}s" if e.get("regen_s") else ""
            extra += f" — {e['reason']}" if e.get("reason") else ""
            print(f"  [{e['verdict']:9}] {e.get('title', '')[:64]}{extra}")
    return 0


def cmd_log(args) -> int:
    for e in _engine(args)._log_entries(args.n):
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.get("at", 0)))
        print(f"{ts}  {e['verdict']:9}  {e.get('id', ''):14} {e.get('title', '')[:60]}")
    return 0


def cmd_swarm(args) -> int:
    from .swarm import run_swarm
    return run_swarm(args)


def cmd_ingest(args) -> int:
    from .ingest import run_ingest
    return run_ingest(args)


def cmd_watch(args) -> int:
    from .watch import run_watch
    return run_watch(args)


def cmd_doctor(args) -> int:
    from .doctor import run_doctor
    return run_doctor(args)


def cmd_gc(args) -> int:
    from .doctor import run_gc
    return run_gc(args)


def cmd_advance(args) -> int:
    r = _engine(args).advance(args.to)
    print(json.dumps(r, indent=1))
    return 0 if r["verdict"] in ("advanced", "noop") else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cafecito", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--repo", default=".", help="repository path (default: .)")

    p = sub.add_parser("init", help="apply the plane to a repo (detects, "
                                    "registers, installs the hook)")
    common(p)
    p.add_argument("--branch", help="landed branch name (default cafecito/main)")
    p.add_argument("--test-cmd", help='gate command, e.g. "python3 -m pytest -q"'
                                      " (default: detected from the repo)")
    p.add_argument("--redetect", action="store_true",
                   help="re-run gate detection over an existing config")
    p.add_argument("--no-mcp", action="store_true",
                   help="don't write .mcp.json (agents won't find the plane)")
    p.add_argument("--no-hook", action="store_true",
                   help="don't install the post-commit advance hook")
    p.add_argument("--require-signal", action="store_true",
                   help="refuse landings with no test signal")
    p.add_argument("--setup-cmd", help='prepare gate worktrees, e.g. "npm ci"')
    p.add_argument("--generated", action="append", metavar="PATTERN=COMMAND",
                   help='deterministic regeneration, e.g. '
                        '"package-lock.json=npm install --package-lock-only"')
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("serve", help="run the MCP server on stdio")
    common(p)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("sync", help="landed tip; --worktree creates a ready worktree")
    common(p)
    p.add_argument("--worktree", action="store_true")
    p.add_argument("--agent", default="cli")
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("submit", help="land a committed changeset")
    common(p)
    p.add_argument("ref", help="commit sha or ref")
    p.add_argument("--title", default="")
    p.add_argument("--agent", default="cli")
    p.set_defaults(fn=cmd_submit)

    p = sub.add_parser("status", help="tip, counts, leases, recent landings")
    common(p)
    p.add_argument("-n", type=int, default=10)
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("log", help="the landed log")
    common(p)
    p.add_argument("-n", type=int, default=20)
    p.set_defaults(fn=cmd_log)

    p = sub.add_parser("advance", help="move the tip to a descendant commit")
    common(p)
    p.add_argument("--to", default="HEAD")
    p.set_defaults(fn=cmd_advance)

    p = sub.add_parser("swarm", help="plan a goal into tasks, build with a parallel fleet, land")
    common(p)
    p.add_argument("goal", help="what the fleet should build")
    p.add_argument("--agents", type=int, default=3, help="fleet size (default 3)")
    p.add_argument("--model", default="sonnet", help="worker agent model")
    p.add_argument("--planner-model", default="sonnet")
    p.add_argument("--timeout", type=int, default=900, help="per-agent seconds")
    p.add_argument("--dry-run", action="store_true",
                   help="plan and print tasks, execute nothing")
    p.add_argument("--retries", type=int, default=1,
                   help="retries per failed/escalated task (default 1)")
    p.add_argument("--drift-wait", type=int, default=120, dest="drift_wait",
                   help="seconds a drifted task waits for contended leases "
                        "before submitting anyway (default 120)")
    p.set_defaults(fn=cmd_swarm)

    p = sub.add_parser("ingest", help="poll open GitHub PRs and land them through the plane")
    common(p)
    p.add_argument("--github", help="owner/repo (default: derived from origin)")
    p.add_argument("--poll", type=int, default=60, help="seconds between polls")
    p.add_argument("--once", action="store_true", help="one poll cycle, then exit")
    p.add_argument("--no-report", action="store_true",
                   help="do not comment/label on GitHub (dry reporting)")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("watch", help="live fleet dashboard")
    common(p)
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--once", action="store_true", help="print one frame and exit")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("doctor", help="environment + control-plane health checks")
    common(p)
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("gc", help="clean stale worktrees, expired leases, dead inflight")
    common(p)
    p.set_defaults(fn=cmd_gc)

    p = sub.add_parser("version", help="print version")
    p.set_defaults(fn=lambda a: print(f"cafecito {__version__}") or 0)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
