# mcp — the agent surface (v0.1)

Zero-dependency MCP server (stdio, newline-delimited JSON-RPC). Exposes
SPEC §7: `sync` · `reserve` · `submit` · `status`. One process per agent
session; engine state is shared through `<repo>/.cafecito/`.

Register with Claude Code:

```sh
claude mcp add cafecito -- python3 /abs/path/to/cafecito/mcp/server.py --repo /abs/path/to/your/repo
```

Agent loop: `sync` (get tip / worktree) → `reserve` symbols you'll touch →
work, commit → `submit` your HEAD sha → `landed` or `escalated` (+reason).
Never `git rebase`, never resolve a conflict.
