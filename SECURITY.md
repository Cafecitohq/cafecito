# Security policy

## Reporting a vulnerability

Email **security@cafeci.to**. Please don't open a public issue for anything you believe is
exploitable — reports are acknowledged within 72 hours, and we'll coordinate a fix and
disclosure timeline with you.

There is no bug bounty; credit in the release notes is gladly given.

## Scope notes for v0.x

cafecito v0.x runs entirely on your machine — there is no hosted service and no network
listener. Things worth knowing when you threat-model it:

- The MCP server is **stdio-only** (`cafecito serve`); it never opens a socket.
- State lives in `<repo>/.cafecito/` and the materialized branch `cafecito/main` — both are
  plain files in a repo you already control.
- The **gate executes your configured test command** (from `cafecito init --test-cmd`) in a
  worktree. Treat that command, and the repo's test suite, as code you trust — the gate is a
  code-execution surface by design. To shrink that surface, set `isolation: "sandbox"`
  (macOS) in `.cafecito/config.json`: every test invocation runs under `sandbox-exec` with
  the network denied and file writes confined to the gate's worktree. `"container"` runs
  tests in docker/podman with `--network=none` (experimental). Either way the failure mode
  is closed: an unavailable backend reddens the gate instead of running unisolated. Setup
  commands (`setup_cmd`) still run on the host with the real environment — installs need
  network — so the trust note above applies to them in full.
- The **reconciler invokes your configured agent CLI** (e.g. `claude`) and sends it repo
  content from both colliding changesets. Point it only at an agent/account you trust with
  that code.

Supported: the latest tagged release and `main`.
