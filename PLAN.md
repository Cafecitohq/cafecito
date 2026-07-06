# Cafecito — an integration control plane for AI agent fleets

**Codename:** cafecito (working title; candidate product names: Tributary, Braid, Mainline)
**Status:** Plan v0.1 — 2026-07-06
**One-liner:** Git serializes integration; agent fleets need it parallelized. Cafecito is the
merge-and-deploy control plane that lets hundreds of coding agents land changes into one
codebase without rebase storms, redundant CI, or pipeline gridlock.

---

## 1. The problem

Merging and deploying with multiple agents in parallel is an unsolved problem today:

- When one agent merges to main, every other in-flight agent must rebase, rerun tests, and
  restart its merge attempt. With N agents, the last one in line does O(N) rebase+test cycles.
- Merge queues (GitHub, Bors, Mergify, Aviator) serialize integration: throughput is capped at
  `1 / (CI duration)` merges regardless of how many agents you run.
- CI cost explodes: most reruns re-test code that didn't change, against changes that couldn't
  possibly conflict.
- Agents are faster than humans at producing changes but *worse* at resolving integration
  contention — they retry aggressively, lock pipelines, and stomp each other.

The bottleneck is not git's storage. It is three design assumptions baked into git-era tooling:

1. **Line-based merge semantics** — conflicts are detected by textual overlap, so the system
   can't tell "these two changes are independent" from "these collide," and must assume collision.
2. **Whole-repo serialization** — main is a single linear history; every landing invalidates
   every other candidate.
3. **Integration coupled to wall-clock CI** — every position change in the queue triggers a full
   re-test, even when the result is already knowable.

Humans generate ~5 PRs/week each, so these assumptions were tolerable. An agent fleet generates
hundreds of changesets per hour. The serialization point has moved from *writing code* to
*landing code*. That's the market.

## 2. Thesis

> Coordination should happen at **intent time** (before work is wasted) and merges should be
> ordered by **semantic commutativity** (what actually conflicts), not textual overlap or
> queue position. CI results should be **memoized facts**, not rituals repeated per rebase.

Three moves follow from this:

1. **Changes become data.** Agents submit *changesets*: the edit itself plus a machine-derived
   read/write set at symbol level (functions, types, config keys, schema objects) and declared
   intent. Not an opaque diff.
2. **A conflict oracle replaces the queue.** Changesets with disjoint read/write sets commute —
   they land in parallel with zero rebasing. Only true semantic collisions serialize, and those
   are detected at submission (or earlier, via leases), not at merge time.
3. **Git becomes an interop boundary, not the coordination medium.** Main is still materialized
   as a git branch for humans, CI runners, and deploy tooling. But agents never run
   `git rebase`; the control plane owns integration and emits git commits as an export format.

### 2.5 The bet, sharpened (competitive reality — added 2026-07-06)

A landscape scan shows the *mechanical* layer is already crowded: agent merge queues (ctx,
Gas Town's Refinery, Overstory), orchestrators with conflict-fixing agents (Agent Orchestrator,
Conductor, Cursor 3), speculative queue testing (Aviator, GitHub merge queue), and agent-native
VCS rethinks (Atomic, Freestyle, Jujutsu-adjacent essays). Shipping only the Phase-1 wedge makes
us a nicer Aviator — a feature, not a company.

What nobody ships (verified July 2026): **(a)** commutativity-proven *parallel landing* from
symbol-level read/write sets — competitors speculate on orderings of a serial queue, none prove
independence; **(b)** **regenerative merge** as a landing primitive — when two changesets truly
collide, a fresh agent regenerates the overlapping region once from *both intents + both
acceptance-test sets*, gated by CI. No rebase, no "fix the conflicts" prompt, no human-style
resolution. The idea appears in essays; no one has shipped it or published a success rate.

The category claim, stated as physics: agent fleets invert the cost model — generation is
nearly free, verification and coherence are scarce. Therefore merging text is the wrong
operation. **Prove independence when you can (commute); re-derive when you can't (regenerate);
never "resolve a conflict" at all.** The diff is a human-era artifact; the durable unit of
contribution is intent + acceptance tests, with code as a derived artifact.

Consequences for this plan:
- The merge queue is the **trojan horse**, not the product. The product is the two primitives.
- Durable moats, in order: (1) **protocol neutrality** — an MCP coordination layer spanning
  Claude/Cursor/Antigravity fleets, a position no agent vendor can take; (2) the **conflict
  corpus flywheel** — every lease collision, failed speculation, and regenerated merge is
  labeled data about what actually conflicts, compounding oracle accuracy; (3) **publishing
  first** — the regenerative-merge success-rate benchmark names the category.

## 3. Product

### What it is

An open-source **integration control plane** with three surfaces:

- **Agent SDK + MCP server** — how agents interact with the codebase: `sync`, `reserve`,
  `submit`, `status`. An agent working through Cafecito never sees a merge conflict; it sees
  "your reservation on `auth/session.go:RefreshToken` conflicts with agent-42's lease, ETA 90s"
  *before* it starts work.
- **Merge planner** — the server that turns a stream of incoming changesets into a
  commutativity DAG, speculatively builds/tests combinations, and lands independent changes in
  parallel.
- **Deploy trains** — batched, continuously-departing deploys with automatic bisection: when a
  train fails, the DAG structure identifies the culprit changeset without reverting the batch.

### Core components

| Component | Role | Innovation |
|---|---|---|
| Conflict oracle | Derives symbol-level read/write sets per changeset (tree-sitter parsing + import graph); decides commutativity | Turns "assume everything conflicts" into "prove independence"; falls back to file-level when analysis is uncertain |
| Lease service | Short-lived advisory reservations on symbols/paths, taken at intent time | Moves coordination from merge time (late, wasteful) to planning time (early, cheap) |
| Merge planner | Schedules the changeset DAG; speculatively tests combinations ahead of confirmation (Uber SubmitQueue-style, generalized) | Throughput scales with available CI compute, not CI latency |
| Test memoization | Content-addressed test results (Bazel-style hashing of inputs → verdict) | A rebase that doesn't change a test's input closure doesn't rerun it; kills the O(N) rerun tax |
| Git gateway | Materializes the landed log as a real git branch; ingests human PRs as ordinary changesets | Zero migration for humans, CI, and deploy infra; adoption wedge |
| Fleet console | Web UI: live DAG, per-agent throughput, contention hotspots, wasted-compute metrics | The dashboard a platform team screenshots into their exec deck |

### Safety model (important)

The conflict oracle is an **optimization, not the safety mechanism**. Two changesets with
disjoint symbols can still interfere behaviorally (shared global state, wire formats). So:

- Speculative CI on the *combined* state is always the final gate before landing.
- The oracle's job is to decide what to *try in parallel* and what to serialize — a wrong
  "commutes" answer costs a failed speculation (compute), never a broken main.
- Every landing is bisectable: the DAG records exactly which changesets entered each tested state.

## 4. Architecture sketch

```
 agents ──MCP/SDK──▶ ┌──────────────────────────────────────────┐
                     │  Control plane (Rust)                     │
 humans ──GitHub──▶  │  intake → conflict oracle → lease svc     │
                     │  → merge planner (speculation DAG)        │
                     │  → landed log (source of truth, append-   │
                     │    only, content-addressed)               │
                     └──────┬──────────────┬─────────────────────┘
                            │              │
                     git gateway     test memoization cache
                     (materialized   (content-addressed verdicts,
                      main branch)    remote-execution friendly)
                            │
                      CI / deploy trains
```

- **Language:** Rust for the engine (perf, single static binary, credibility in infra OSS);
  TypeScript for SDK/MCP server and console.
- **Storage:** landed log in Postgres + object store (content-addressed blobs); no custom DB.
- **Parsing:** tree-sitter grammars for symbol extraction (start: TS/JS, Python, Go, Rust).
- **Interop:** GitHub App for the wedge deployment; `git fetch` always works against the gateway.

## 5. Why now, and why this can be venture-scale

- **Timing.** 2025–26 is the first period where multi-agent fleets (Claude Code, Codex, Devin,
  internal fleets) are landing real production code. Every fleet operator hits this wall within
  weeks. The pain is new, acute, and unowned.
- **The serialization point is the moat point.** Whoever owns integration owns the control
  plane of agent-era software delivery — the position GitHub occupied for human collaboration.
  Integration control naturally accretes adjacent surface: CI orchestration, deploy, code
  review routing, agent observability.
- **Comps and market shape.** Graphite raised $52M for human stacked-diff workflow; Mergify and
  Aviator built businesses on merge queues alone; GitHub built merge queue as a first-party
  feature — all for *human-velocity* teams. Agent velocity is 10–100× and existing tools cap
  out architecturally, not incrementally. Adjacent evidence that memoized/speculative CI is
  valuable at scale: Bazel remote cache ecosystem (BuildBuddy, EngFlow), Uber SubmitQueue, Meta
  Sapling — all internal-scale solutions never productized for this buyer.
- **Business model (open core).**
  - Apache-2.0: engine, conflict oracle, SDK, MCP server, git gateway, protocol spec,
    single-node deployment. (Permissive on purpose: the neutrality moat and the standards
    race require it. No BUSL/SSPL at day zero — strip-mining is a problem you earn later.)
  - Paid cloud: hosted control plane, test-memoization cache, cross-repo coordination,
    fleet analytics. Pricing per **active agent seat** — a metric that grows with the trend.
  - Enterprise: self-hosted HA, SSO/audit/compliance, priority SLAs.
  - **Exit-protection mechanics (day one, cheap now, unfixable later):**
    1. The open-core line keeps the flywheel closed: the conflict corpus and any oracle
       models trained on it are proprietary — the un-forkable asset.
    2. CLA (or DCO + assignment) from every contributor from commit one → clean IP for
       acquirer diligence and relicensing optionality.
    3. Trademark, domain, and repo stay company-owned; no foundation donation pre-exit
       (one-way door — transfers the asset being sold).
- **Exit paths.** Strategic acquirers on every side: GitHub/Microsoft, Cursor/Anysphere,
  Anthropic/OpenAI (fleet infrastructure), Atlassian, CI vendors (Buildkite, Harness).

## 6. The killer demo (build this before anything else)

**"MergeBench":** N agents (5, 25, 100) making real changes to one mid-size repo.

- Baseline: GitHub merge queue → throughput flatlines at ~1/CI-duration; graph of agents
  sitting in rebase-retry loops; CI-minutes burned grows quadratically.
- Cafecito: near-linear merge throughput until true-conflict density saturates; CI-minutes
  per landed change stays flat.

One chart, two lines. That's the launch blog post, the HN demo, and slide 3 of the deck.

## 7. Roadmap

### Phase 0 — Validate the physics (weeks 1–4)
Two falsification experiments, both cheap, one of which no competitor has published:
- **Experiment A — commutativity rate.** Prototype the conflict oracle standalone; run it over
  historical PR pairs from busy OSS repos (e.g. kubernetes, vscode) and agent-generated PR
  corpora. Go/no-go: % of concurrent change pairs provably commutative at symbol level.
  Hypothesis: >70% for typical repos. If it's 20%, rethink oracle granularity.
- **Experiment B — regenerative-merge success rate.** Harvest real conflicting PR pairs; for
  each, give a fresh agent both diffs' *intents* (PR descriptions / derived summaries) plus both
  test suites, and have it regenerate the overlapping region from the common ancestor. Measure
  % that pass both test suites. This number is the launch blog post and the category-defining
  benchmark — publish it regardless of outcome.
- Spec the changeset format and landed-log semantics (the "protocol" — this doc is what the
  community will argue about, which is good).

### Phase 1 — Wedge MVP (weeks 5–14)
- GitHub App: Cafecito as a drop-in *smart merge queue* — agents (and humans) open normal PRs;
  Cafecito lands commutative ones in parallel, serializes true conflicts, batches CI.
- MCP server + TS/Python SDK: `submit`, `reserve`, `status`, `sync`.
- Dogfood: run our own agent fleet building Cafecito through Cafecito. This recursion is the
  credibility story.
- Ship MergeBench publicly.

### Phase 2 — The moat (months 4–8)
- Test memoization cache (content-addressed verdicts; Bazel-compatible where possible).
- Full speculative DAG execution with kill-and-requeue.
- Deploy trains with automatic bisection.
- Fleet console v1.
- **OSS launch:** benchmark post + repo + `docker compose up` quickstart. Target: HN front
  page, 5k GitHub stars in 90 days, 10 design partners running real fleets.

### Phase 3 — Source of truth (months 9–18)
- Native landed-log mode: git becomes pure export; agents sync from the log directly
  (no working-copy rebases ever).
- Cross-repo / monorepo-scale coordination; symbol-graph service.
- Hosted cloud GA; per-agent-seat billing.
- Raise on: fleet count growth, landed-changesets/week, CI-minutes saved (a dollar number).

## 8. Metrics that matter

- **Merges/hour per repo** at fixed CI latency (the headline).
- **CI-minutes per landed change** (the COGS/ROI number — converts directly to dollars).
- **Wasted-work ratio:** agent-hours discarded due to conflicts, before vs. after leases.
- **Time-to-main:** p50/p95 from changeset submission to landed.
- Adoption: active fleets, weekly landed changesets, stars/forks/contributors.

## 9. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Oracle false-negatives (semantically coupled changes with disjoint symbols) | Speculative CI is always the landing gate; oracle only chooses parallelism. Track false-negative rate as a first-class metric. |
| "Replacing git" adoption allergy | Never lead with that. Wedge is a merge queue for GitHub PRs; git gateway keeps every existing tool working. |
| GitHub/Cursor ships this | They're anchored to human workflows and line-based merge; our speed, OSS community, and agent-native protocol are the counter. Being the *neutral* layer across agent vendors is a position none of them can take. |
| Conflict density too high in real repos (oracle rarely helps) | Phase 0 exists to falsify this cheaply. Leases also *reduce* conflict density by steering agents apart before work starts. |
| Test suites too slow/flaky for speculation to pay off | Memoization pays off regardless; speculation degrades gracefully to a plain queue. Flake detection is a natural (and sellable) byproduct. |
| Rust engine slows iteration early | Phase 1 wedge can be TS on the GitHub API; port hot paths to Rust when the landed-log becomes native (Phase 3). |

## 10. Immediate next steps

1. Scaffold the repo: workspace layout (`engine/`, `oracle/`, `sdk/`, `mcp/`, `gateway/`,
   `bench/`), Apache-2.0, protocol spec draft (`SPEC.md`).
2. Build the Phase-0 conflict oracle prototype + commutativity measurement harness.
3. Run it on 2–3 real repos; produce the first commutativity-rate numbers.
4. Draft MergeBench design.
5. Name decision + domain/org handles.
