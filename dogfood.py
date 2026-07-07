#!/usr/bin/env python3
"""Dogfood: build cafecito through cafecito.

A fleet driver that routes every operation through the real MCP server on
THIS repo: reserve leases → sync a worktree → a headless coding agent does a
real backlog task → commit → submit for landing. Commuting changesets land on
`cafecito/main`; collisions regenerate; everything passes the gate.

The first backlog is self-referential on purpose: cafecito has no unit tests,
so its own landing gate has no signal on its own repo — the first changesets
cafecito lands into cafecito are the tests that give its gate teeth, plus the
commit-trailer improvement the engine was missing.

Usage:  python3 dogfood.py [--model sonnet] [--pytest-python PATH]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent

TASKS = [
    {
        "id": "tests-writeset",
        "title": "oracle: unit tests for symbol extraction",
        "paths": ["oracle/tests/test_writeset.py"],
        "brief": (
            "Create oracle/tests/test_writeset.py with deterministic unit tests "
            "for writeset.python_symbols() and writeset._attribute(). Cover: "
            "nested def/class qualified names (Class.method), decorator lines "
            "included in a symbol's span, and _attribute() mapping a changed "
            "line range to the innermost enclosing symbol vs '<module>'. Use "
            "small inline source strings; no git, no filesystem."),
    },
    {
        "id": "tests-regen",
        "title": "engine: unit tests for diff3 segmentation",
        "paths": ["engine/tests/test_regen.py"],
        "brief": (
            "Create engine/tests/test_regen.py testing regen.diff3_segments() "
            "and regen._test_defs(). Cover: identical/cleanly-merging inputs "
            "return None; a genuine 3-way collision yields alternating "
            "text/Region segments whose ours/base/theirs match the inputs; "
            "_test_defs extracts test function names. diff3_segments shells "
            "out to `git merge-file` on temp files — that is fine and needs "
            "no repository."),
    },
    {
        "id": "tests-gate",
        "title": "engine: unit tests for impact-test mapping",
        "paths": ["engine/tests/test_gate.py"],
        "brief": (
            "Create engine/tests/test_gate.py for gate.impact_tests(). Build a "
            "tiny throwaway git repo in tmp_path (subprocess git init/add/"
            "commit) containing pkg/mod.py and pkg/tests/test_mod.py. Assert: "
            "a source file maps to its sibling tests/test_<stem>.py at the "
            "given rev; a touched test file maps to itself; non-.py files "
            "produce nothing."),
    },
    {
        "id": "land-trailers",
        "title": "engine: landed commits carry Changeset-Id and Signed-off-by trailers",
        "paths": ["engine/landing.py", "engine/tests/test_landing_message.py"],
        "brief": (
            "In engine/landing.py the landed commit message is built inline at "
            "two sites (clean candidate and regenerated candidate). Extract a "
            "module-level helper _land_message(title, cs_id, regenerated) -> "
            "str used by both sites, producing:\n"
            "  land: <title truncated to 70 chars>\\n\\n"
            "  Changeset-Id: <cs_id>\\n"
            "  Regenerated: true   (this line only when regenerated)\\n"
            "  Signed-off-by: cafecito-engine <engine@cafecito.local>\\n"
            "Add engine/tests/test_landing_message.py with tests for both "
            "shapes (import landing; call the helper directly)."),
    },
]

AGENT_PROMPT = """\
You are a coding agent contributing to the cafecito project, working alone in \
this repository checkout.

TASK: {title}
{brief}

Rules: create or modify ONLY these paths: {paths}. Tests must be deterministic \
and use only the standard library + pytest. Product code lives in the \
`cafecito` package: `from cafecito import writeset, regen, gate, engine`. Keep \
the diff small and match the existing style. Do not run tests or any shell \
commands. Make the edits, then stop and summarize in one sentence.
"""


class MCPClient:
    """Minimal MCP stdio client — the same wire an IDE agent uses."""

    def __init__(self, repo: str):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "cafecito.mcp_server", "--repo", repo],
            cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True)
        self._id = 0
        r = self._rpc("initialize", {"protocolVersion": "2024-11-05"})
        assert r["serverInfo"]["name"] == "cafecito"
        self._notify("notifications/initialized")

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self.proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": self._id, "method": method,
             "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        resp = json.loads(self.proc.stdout.readline())
        if "error" in resp:
            raise RuntimeError(resp["error"]["message"])
        return resp["result"]

    def _notify(self, method: str) -> None:
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def tool(self, name: str, **args) -> dict:
        r = self._rpc("tools/call", {"name": name, "arguments": args})
        text = r["content"][0]["text"]
        if r.get("isError"):
            raise RuntimeError(f"{name}: {text}")
        return json.loads(text)

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


def run_agent(worktree: str, task: dict, model: str, timeout: int = 900) -> None:
    prompt = AGENT_PROMPT.format(title=task["title"], brief=task["brief"],
                                 paths=", ".join(task["paths"]))
    r = subprocess.run(
        ["claude", "-p", prompt, "--model", model,
         "--permission-mode", "acceptEdits",
         "--allowedTools", "Edit,Write,Read,Grep,Glob", "--max-turns", "25"],
        cwd=worktree, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"agent failed: {r.stderr.strip()[:200]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--pytest-python", default=str(
        ROOT / "phase0" / "workdir" / "venv-test" / "bin" / "python"))
    args = ap.parse_args()

    state = ROOT / ".cafecito"
    state.mkdir(exist_ok=True)
    cfg = state / "config.json"
    base_cfg = json.loads(cfg.read_text()) if cfg.exists() else {}
    base_cfg["test_cmd"] = [args.pytest_python, "-m", "pytest", "-q",
                            "--tb=line", "-p", "no:cacheprovider"]
    base_cfg.setdefault("reconciler_model", args.model)
    cfg.write_text(json.dumps(base_cfg, indent=1))

    mcp = MCPClient(str(ROOT))
    print(f"[dogfood] tip {mcp.tool('sync')['tip'][:10]} — {len(TASKS)} tasks")
    results = []
    try:
        for task in TASKS:
            agent = f"dogfood/{task['id']}"
            lease = mcp.tool("reserve", keys=[f"file:{p}" for p in task["paths"]],
                             agent=agent, intent=task["title"])
            if not lease.get("granted"):
                print(f"[dogfood] {task['id']}: lease contention {lease['conflicts']}")
            wt = mcp.tool("sync", agent=agent, create_worktree=True)["worktree"]
            print(f"[dogfood] {task['id']}: agent working in {wt}")
            t0 = time.time()
            run_agent(wt, task, args.model)
            status = subprocess.run(["git", "-C", wt, "status", "--porcelain"],
                                    capture_output=True, text=True).stdout.strip()
            if not status:
                results.append((task["id"], "no-changes", None))
                print(f"[dogfood] {task['id']}: agent produced no changes")
                continue
            subprocess.run(["git", "-C", wt, "add", "-A"], check=True)
            subprocess.run(
                ["git", "-C", wt, "-c", "user.name=cafecito-agent",
                 "-c", "user.email=agent@cafecito.local", "commit", "-q",
                 "-s", "-m", f"{task['title']}\n\n{task['brief'][:400]}"],
                check=True)
            head = subprocess.run(["git", "-C", wt, "rev-parse", "HEAD"],
                                  capture_output=True, text=True).stdout.strip()
            verdict = mcp.tool("submit", ref=head, agent=agent,
                               title=task["title"])
            results.append((task["id"], verdict["verdict"], verdict))
            gate = verdict.get("gate", {})
            print(f"[dogfood] {task['id']}: {verdict['verdict'].upper()} "
                  f"(agent {time.time()-t0:.0f}s, gate {gate.get('seconds')}s, "
                  f"regen={verdict.get('regenerated', False)}, "
                  f"reason={verdict.get('reason', '—')})")
            subprocess.run(["git", "-C", str(ROOT), "worktree", "remove",
                            "--force", wt], capture_output=True)
        st = mcp.tool("status")
    finally:
        mcp.close()

    print(f"""
== dogfood · cafecito through cafecito =====================================
tasks:     {len(TASKS)}
landed:    {sum(1 for _, v, _ in results if v == 'landed')}
escalated: {sum(1 for _, v, _ in results if v == 'escalated')}
other:     {sum(1 for _, v, _ in results if v not in ('landed', 'escalated'))}
tip:       {st['tip'][:12]} on {st['branch']}
next:      git merge --ff-only {st['branch']}   (deploy the landings to main)
============================================================================""")
    for tid, v, detail in results:
        if v == "escalated":
            print(f"  escalated {tid}: {detail.get('reason')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
