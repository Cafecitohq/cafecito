"""The cafecito command line.

  cafecito init      set up the control plane on a repo
  cafecito serve     run the MCP server on stdio (what agents connect to)
  cafecito submit    land a committed changeset from the shell
  cafecito status    tip, counts, recent landings, active leases
  cafecito log       the landed log
  cafecito advance   follow out-of-band commits (move tip to a descendant)
  cafecito swarm     one goal in, a parallel fleet out: plan, build, land
  cafecito watch     live dashboard of the fleet and the landed log
  cafecito version
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time

from . import __version__
from .engine import Engine
from .mcp_server import serve


def _engine(args) -> Engine:
    return Engine(args.repo)


def cmd_init(args) -> int:
    eng = Engine(args.repo)
    changed = False
    if args.branch:
        eng.config["branch"] = args.branch
        changed = True
    if args.test_cmd:
        eng.config["test_cmd"] = shlex.split(args.test_cmd)
        changed = True
    if args.require_signal:
        eng.config["require_signal"] = True
        changed = True
    for spec in args.generated or []:
        pat, _, cmd = spec.partition("=")
        if not cmd:
            print(f"--generated expects PATTERN=COMMAND, got {spec!r}")
            return 2
        eng.config.setdefault("generated", {})[pat] = shlex.split(cmd)
        changed = True
    if changed:
        (eng.state_dir / "config.json").write_text(json.dumps(eng.config, indent=1))
        eng = Engine(args.repo)  # re-read; branch ref follows on next landing
    st = eng.status(limit=1)
    print(f"cafecito initialized on {eng.repo}")
    print(f"  landed branch : {eng.config['branch']}")
    print(f"  tip           : {st['tip'][:12]}")
    print(f"  test command  : {' '.join(eng.config['test_cmd'])}")
    print(f"  require signal: {eng.config.get('require_signal', False)}")
    print(f"\nconnect an agent:\n  claude mcp add cafecito -- cafecito serve --repo {eng.repo}")
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


def cmd_watch(args) -> int:
    from .watch import run_watch
    return run_watch(args)


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

    p = sub.add_parser("init", help="set up the control plane on a repo")
    common(p)
    p.add_argument("--branch", help="landed branch name (default cafecito/main)")
    p.add_argument("--test-cmd", help='gate command, e.g. "python3 -m pytest -q"')
    p.add_argument("--require-signal", action="store_true",
                   help="refuse landings with no test signal")
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
    p.set_defaults(fn=cmd_swarm)

    p = sub.add_parser("watch", help="live fleet dashboard")
    common(p)
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--once", action="store_true", help="print one frame and exit")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("version", help="print version")
    p.set_defaults(fn=lambda a: print(f"cafecito {__version__}") or 0)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
