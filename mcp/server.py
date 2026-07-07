#!/usr/bin/env python3
"""cafecito MCP server v0.1 — stdio transport, zero dependencies.

Exposes the SPEC §7 surface (sync / reserve / submit / status) over the Model
Context Protocol so any MCP-capable agent — Claude Code, Cursor, Antigravity,
Claude Desktop — can coordinate through cafecito. One server process per agent
session; engine state is shared via the repo's .cafecito/ directory with file
locking, so any number of sessions coordinate safely.

Register with Claude Code:
  claude mcp add cafecito -- python3 /path/to/cafecito/mcp/server.py --repo /path/to/repo

Protocol: newline-delimited JSON-RPC 2.0 on stdio (MCP stdio transport).
Logs go to stderr only — stdout belongs to the protocol.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "engine"))

from landing import Engine  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "sync",
        "description": (
            "Get the current landed tip of the repository. Work from this "
            "commit. Set create_worktree=true to get a ready-to-use detached "
            "worktree path. cafecito agents never rebase — when your work is "
            "committed, call submit."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "your agent id"},
                "create_worktree": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "reserve",
        "description": (
            "Take short advisory leases on symbols or files BEFORE starting "
            "work, so contention is discovered before effort is wasted. Keys "
            "like 'file:path/to/mod.py' or 'py:path/to/mod.py::Class.method'. "
            "Leases are advisory: landing never requires one, but if reserve "
            "reports a conflict, pick different work or wait for the holder's "
            "ETA. Your leases are released automatically when you land."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keys": {"type": "array", "items": {"type": "string"}},
                "agent": {"type": "string"},
                "ttl": {"type": "integer", "description": "seconds, default 900"},
                "intent": {"type": "string", "description": "one line: what you're doing"},
            },
            "required": ["keys", "agent"],
        },
    },
    {
        "name": "submit",
        "description": (
            "Submit a committed changeset for landing. Pass the commit sha (or "
            "ref) of your work. cafecito merges it against the landed tip: "
            "commuting changes land immediately; collisions are regenerated "
            "from both intents by a reconciler; everything passes a real test "
            "gate before landing. Returns landed | escalated | rejected. If "
            "escalated, read the reason, rework, and submit again."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "commit sha or ref to land"},
                "agent": {"type": "string"},
                "title": {"type": "string", "description": "one-line summary (default: commit subject)"},
            },
            "required": ["ref"],
        },
    },
    {
        "name": "status",
        "description": (
            "Current landed tip, landing/escalation counts, recent landings "
            "with gate results, and active leases across the fleet."),
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20}},
        },
    },
]


def handle(engine: Engine, method: str, params: dict):
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "cafecito", "version": "0.1.0"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "sync":
                result = engine.sync(agent=args.get("agent"),
                                     create_worktree=bool(args.get("create_worktree")))
            elif name == "reserve":
                result = engine.reserve(keys=list(args.get("keys") or []),
                                        agent=args.get("agent", "anonymous"),
                                        ttl=args.get("ttl"),
                                        intent=args.get("intent", ""))
            elif name == "submit":
                result = engine.submit(ref=args["ref"],
                                       agent=args.get("agent", ""),
                                       title=args.get("title", ""))
            elif name == "status":
                result = engine.status(limit=int(args.get("limit", 20)))
            else:
                return {"content": [{"type": "text",
                                     "text": f"unknown tool {name!r}"}],
                        "isError": True}
            return {"content": [{"type": "text",
                                 "text": json.dumps(result, indent=1)}]}
        except Exception as e:  # tool errors are results, not protocol errors
            return {"content": [{"type": "text", "text": f"error: {e}"}],
                    "isError": True}
    raise KeyError(method)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    args = ap.parse_args()
    engine = Engine(args.repo)
    print(f"cafecito mcp server: repo={engine.repo} "
          f"branch={engine.config['branch']}", file=sys.stderr)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg_id = msg.get("id")
        method = msg.get("method", "")
        if msg_id is None:  # notification — no response
            continue
        try:
            result = handle(engine, method, msg.get("params") or {})
            resp = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        except KeyError:
            resp = {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32601, "message": f"method not found: {method}"}}
        except Exception as e:
            resp = {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32603, "message": str(e)[:300]}}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
