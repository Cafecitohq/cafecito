# cafecito ☕

**An integration control plane for AI agent fleets.**
*Prove independence when you can. Re-derive when you can't. Never resolve a conflict.*

> **Status: v0.1 — usable on your laptop.** The physics is validated ([phase0/](phase0/),
> [bench/](bench/)); the landing engine and MCP server now run for real. Not yet: multi-repo,
> GitHub App, hosted anything. Expect sharp edges.

## Quickstart (v0.1)

```sh
pipx install git+https://github.com/cafecitohq/cafecito   # or: pip install . from a clone
cafecito init --repo /path/to/your/repo --test-cmd "python3 -m pytest -q"
claude mcp add cafecito -- cafecito serve --repo /path/to/your/repo
```

Any MCP-capable agent then coordinates through four tools: `sync` (get the landed tip or a
ready worktree), `reserve` (advisory leases on symbols before starting work), `submit` (land a
committed changeset), `status`. Commuting changesets land immediately; collisions are
regenerated from both intents by a reconciler; **every** landing passes a real test gate; main
is materialized as a normal git branch (`cafecito/main`). Agents never rebase and never see a
conflict marker. Humans drive it from the shell: `cafecito submit | status | log | advance`.
Prove it locally: `python3 -m cafecito.tests.smoke`.

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
| [SPEC.md](SPEC.md) | Protocol draft: changesets, leases, landed log, MCP surface | draft v0 |
| [cafecito/](cafecito/) | The product: oracle write sets, landing engine (merge/regenerate/gate/landed log), MCP server, CLI — `pip install`able, zero dependencies | **v0.1** |
| [sdk/](sdk/) | TypeScript / Python client SDKs | design |
| [gateway/](gateway/) | Full git gateway (v0.1 materializes a branch + `advance` ingestion; PR ingestion pending) | design |
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

Python 3.11+ and git ≥ 2.38. Stdlib only — no dependencies. See [phase0/README.md](phase0/README.md)
for methodology and current numbers.

## License

[Apache-2.0](LICENSE). Contributions require DCO sign-off — see [CONTRIBUTING.md](CONTRIBUTING.md).

*"cafecito" started as the codename and won the vote to stay. The coffee is load-bearing.*
*Home: [cafeci.to](https://cafeci.to) · code: [github.com/Cafecitohq/cafecito](https://github.com/Cafecitohq/cafecito)*
