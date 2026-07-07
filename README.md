# cafecito ☕

**An integration control plane for AI agent fleets.**
*Prove independence when you can. Re-derive when you can't. Never resolve a conflict.*

> ⚠️ **Status: Phase 0 — validating the physics.** Nothing here is production software yet.
> What exists today is the [protocol draft](SPEC.md) and two reproducible experiments
> ([phase0/](phase0/)) that test whether the core bets hold on real repositories.

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

## Repository layout

| Path | What | Status |
|---|---|---|
| [phase0/](phase0/) | Falsification experiments A (commutativity rate) and B (regenerative-merge success rate) on real repos | **active** |
| [SPEC.md](SPEC.md) | Protocol draft: changesets, leases, landed log, MCP surface | draft v0 |
| [oracle/](oracle/) | Conflict oracle — symbol-level write-set derivation | pending Phase 0 results |
| [engine/](engine/) | Control plane: intake, merge planner, landed log | design |
| [mcp/](mcp/) | MCP server (`reserve` / `submit` / `status` / `sync`) | design |
| [sdk/](sdk/) | TypeScript / Python client SDKs | design |
| [gateway/](gateway/) | Git materialization of the landed log | design |
| [bench/](bench/) | MergeBench — N agents vs. a merge queue, publicly reproducible | design |
| [PLAN.md](PLAN.md) | Full project plan, roadmap, and competitive analysis | living doc |

## Run the Phase 0 experiments

```sh
cd phase0
python3 experiment_a.py --repo <path-to-clone> --since 2024-06-01   # commutativity rate
python3 find_conflicts.py --repo <path-to-clone>                    # attributed conflict corpus
python3 experiment_b.py --repo <path-to-clone> --max-pairs 5        # regenerative merge
python3 validate_b.py --repo <path-to-clone>                        # dual test-suite validation
```

Python 3.11+ and git ≥ 2.38. Stdlib only — no dependencies. See [phase0/README.md](phase0/README.md)
for methodology and current numbers.

## License

[Apache-2.0](LICENSE). Contributions require DCO sign-off — see [CONTRIBUTING.md](CONTRIBUTING.md).

*"cafecito" is a codename; the coffee is load-bearing.*
