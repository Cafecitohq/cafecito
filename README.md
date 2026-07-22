# cafecito ☕

[![ci](https://github.com/cafecitohq/cafecito/actions/workflows/ci.yml/badge.svg)](https://github.com/cafecitohq/cafecito/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/cafecito)](https://pypi.org/project/cafecito/)

**An integration control plane for AI agent fleets.**
*Prove independence when you can. Re-derive when you can't. Never resolve a conflict.*

> **Status: pre-1.0 — a working single-repo control plane** (current version: see
> [releases](https://github.com/cafecitohq/cafecito/releases)). The physics is validated
> ([phase0/](phase0/), [bench/](bench/)); the engine, MCP server, fleet (`swarm`/`watch`),
> PR gateway (`ingest`), memoized gates, and wave-parallel landing all run for real — and
> every feature since v0.1 [landed through cafecito itself](docs/building-itself.md).
> Not yet: multi-repo, webhooks/hosted App. Sharp edges remain.

![Three agents land in parallel: two commute, one collision is regenerated live, main ends green](https://raw.githubusercontent.com/cafecitohq/cafecito/main/examples/demo.gif)

*34 unedited seconds: three agents branch from the same commit; two commute and land in
parallel, the third collides and is regenerated from both intents by a live reconciler call —
gated, trailer-stamped, main green. Run it yourself: [`examples/demo.sh`](examples/demo.sh).*

## Quickstart

```sh
pipx install cafecito          # PyPI · or git+https://github.com/cafecitohq/cafecito for main
cd your-repo && cafecito init  # that's it
```

`init` reads your repo and reports what it did — no flags in the common case:

```
cafecito 0.15.0 on /Users/you/your-repo
  landed branch : cafecito/main
  tip           : 6712cbaeb828
  gate command  : npm test --silent
                  detected js — package.json scripts.test = 'vitest run'; 34 test file(s)
  setup command : npm ci
  mcp server    : .mcp.json written — commit it so every clone gets the plane
  advance hook  : post-commit — the tip follows commits made outside the plane
```

It detects your gate (pytest / npm test / go test / cargo test, including an app in a
subdirectory), writes a **checked-in `.mcp.json`** so every session, clone, and worktree
finds the plane, and installs a post-commit hook so commits made *without* the plane still
move its tip. Override anything: `--test-cmd`, `--redetect`, `--no-mcp`, `--no-hook`.
`cafecito doctor` re-checks all of it — including whether your gate can actually collect
tests, because a gate that collects nothing lands everything unverified.

Commit `.mcp.json` and your teammates get the plane on their next session (each approves
it once). `claude mcp add cafecito -- cafecito serve --repo .` still works for a
single machine, but it binds to one directory: worktrees, other clones, and teammates
won't see it, and sessions without the plane quietly commit around it.

**Or skip the wiring and summon the fleet directly:**

```sh
cafecito swarm "add rate limiting to the API, a retry helper, and tests for both" --agents 3
cafecito watch        # in another terminal: the live fleet dashboard
```

`swarm` plans the goal into independent tasks, pre-claims leases, runs the agents in
parallel, and lands everything through the gate — commuting changes in parallel, collisions
regenerated, contradictions escalated to you. Workers that drift outside their assigned
paths get contained at the oracle's granularity: the *symbols* they actually wrote are
leased before the changeset enters the pipeline (whole files only when a file can't be
analyzed), so a fleet never knowingly races itself — and a sibling editing a different
symbol in the same file doesn't wait, because symbol-disjoint writers commute. `watch`
shows it happening live:

![cafecito swarm and watch, split-screen — a real fleet lands while the dashboard streams it](https://raw.githubusercontent.com/cafecitohq/cafecito/main/examples/swarm-split.gif)

*(Real split-screen recording, 35s unedited: `cafecito swarm` on the left — planner, three
real agents, three gated landings, green main — while `cafecito watch` on the right streams
the fleet, the leases, and the landed log live. Reproduce it:
`./examples/demo_swarm_split.sh`.)*

Since v0.1, **every feature of cafecito has been landed through cafecito** — the story
(including the release we broke and what it taught us) is in
[docs/building-itself.md](docs/building-itself.md).

Any MCP-capable agent then coordinates through four tools: `sync` (get the landed tip or a
ready worktree), `reserve` (advisory leases on symbols before starting work), `submit` (land a
committed changeset), `status`. Commuting changesets land immediately; collisions are
regenerated from both intents by a reconciler; **every** landing passes a real test gate; main
is materialized as a normal git branch (`cafecito/main`). Agents never rebase and never see a
conflict marker. Humans drive it from the shell: `cafecito submit | status | log | advance` —
or keep opening ordinary GitHub PRs and let `cafecito ingest` land them through the plane.
Symbol-level write sets for **Python, TypeScript/JavaScript, and Go** (stdlib scanners —
anything unanalyzable widens safely to file granularity); other languages land at file
granularity today. **Verification facts:** with `gate_mode: full`, every landing gates on the whole test
suite — but verdicts are content-addressed by input closure, so only tests the landing
actually touched execute; the rest inherit facts. Closures resolve **Python, TypeScript/
JavaScript, and Go** test inputs (import graphs, runner configs, lockfiles; Go rides whole
packages) — and anything the analysis can't see through statically (tsconfig `paths`,
bundler aliases, workspaces, `go:embed`, …) simply runs the test instead of trusting a fact. **Bare gate worktrees** get prepared by your
`--setup-cmd` (`npm ci`, `pip install -e .`) before tests run. **Gate isolation:** the gate
executes candidate code, so `isolation: sandbox` (macOS) runs every test invocation with the
network denied and file writes confined to the gate's own worktree; a `container` backend
(docker/podman, `--network=none`) ships experimental. Unavailable backends redden the gate —
never a silent fallback to unisolated runs. Facts are keyed by isolation mode, so a green
minted with the network open can't satisfy a sandboxed gate. **Generated files** (lockfiles etc.)
skip merging *and* the reconciler:
declare `cafecito init --generated "package-lock.json=npm install --package-lock-only"` and
conflicts re-run the generator against the merged sources — in our TypeScript corpus that
was 58 of 60 real conflicts. Prove it locally: `python3 -m cafecito.tests.smoke`.

## The problem

Run five coding agents against one repo and you'll watch them gridlock: the first merge to
main forces every other agent to rebase, rerun tests, and rejoin the queue. Merge queues
serialize integration, so fleet throughput is capped at `1 / CI-duration` no matter how many
agents you run — and CI spend grows quadratically as everyone re-tests everyone else's rebases.

The bottleneck isn't git's storage; it's three assumptions from the human era:

1. **Line-based merge semantics** — the system can't distinguish "independent" from
   "colliding," so it assumes collision.
2. **Whole-repo serialization** — every landing invalidates every other candidate.
3. **Integration coupled to CI wall-clock** — position changes in the queue trigger full
   re-tests whose results were already knowable.

## The bet

Agent fleets invert the cost model of software integration: **generation is nearly free;
verification and coherence are scarce.** Once regenerating code costs pennies, merging text is
the wrong operation. cafecito is built on two primitives that follow:

- **Commutativity-proven parallel landing.** Changesets carry symbol-level write sets. Provably
  disjoint changes land in parallel — no rebase, no re-test (verification results are
  content-addressed facts, not rituals). Only true collisions serialize.
- **Regenerative merge.** When changes truly collide, no one "resolves the conflict": a fresh
  agent regenerates the overlapping region once, from both changes' *intents* and acceptance
  tests, gated by CI.

Coordination also moves earlier: agents take short **leases** on symbols at intent time, so
contention is discovered before work is wasted, not at merge time.

Git stays as the interop boundary — main is always materialized as a normal git branch for
humans, CI, and deploy tooling. Agents talk to the control plane through an MCP server and
never run `git rebase`.

Vocabulary, used strictly throughout: changesets **land**; collisions **commute**,
**regenerate**, or **escalate**; *merge* is reserved for git's textual mechanism and the
market category it replaces (see [SPEC.md §1.1](SPEC.md)).

## Repository layout

| Path | What | Status |
|---|---|---|
| [phase0/](phase0/) | Falsification experiments A (commutativity rate) and B (regenerative-merge success rate) on real repos | **active** |
| [SPEC.md](SPEC.md) | Protocol: changesets, leases, landed log, verification facts, MCP surface | v0 — all surfaces implemented |
| [cafecito/](cafecito/) | The product: oracle (py/ts/js/go/json write sets), engine (commute/regenerate/escalate, memoized gates, wave-parallel admission), MCP server, `swarm`/`watch`/`ingest`, CLI — `pip install`able, zero dependencies | **active** |
| [sdk/](sdk/) | TypeScript / Python client SDKs | design |
| [gateway/](gateway/) | Git gateway: materialized branch, `advance`, and PR ingestion (`cafecito ingest`, [proven on PR #1](https://github.com/Cafecitohq/cafecito/pull/1)); webhooks/hosted App pending | **shipped in cafecito/** |
| [bench/](bench/) | MergeBench — a real 33-agent burst: 5.5h serial queue vs **1.37h** cafecito (10-min CI), 93.5 vs 16.2 CI-hours, landed for real with green main | **active** |
| [PLAN.md](PLAN.md) | Full project plan, roadmap, and competitive analysis | living doc |

## Run the Phase 0 experiments

```sh
cd phase0
python3 run_corpus.py --repos <clones...>                           # A + conflict scan, many repos
python3 experiment_a.py --repo <path-to-clone> --since 2024-06-01   # commutativity rate
python3 find_conflicts.py --repo <path-to-clone>                    # attributed conflict corpus
python3 experiment_b.py --repo <path-to-clone> --max-pairs 5        # regenerative merge
python3 validate_b.py --repo <path-to-clone>                        # dual test-suite validation
python3 agent_corpus.py --repo <clone> --targets <files...>         # uncoordinated-fleet corpus
```

Python 3.10+ and git ≥ 2.38. Stdlib only — no dependencies. See [phase0/README.md](phase0/README.md)
for methodology and current numbers.

## License

[Apache-2.0](LICENSE). Contributions require DCO sign-off — see [CONTRIBUTING.md](CONTRIBUTING.md).

*"cafecito" started as the codename and won the vote to stay. The coffee is load-bearing.*
*Home: [cafeci.to](https://cafeci.to) · code: [github.com/Cafecitohq/cafecito](https://github.com/Cafecitohq/cafecito)*
