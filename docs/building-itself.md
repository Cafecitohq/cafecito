# We asked our merge plane to build its own features. In parallel.

*Published 2026-07-10 · every claim below is verifiable in
[the repo](https://github.com/cafecitohq/cafecito) — the landed log, the tags, the closed PR.*

---

Three days ago we launched [cafecito](https://cafeci.to), an integration control plane for AI
agent fleets, with a benchmark and a thesis: agent fleets don't need merge queues — commuting
changes should land in parallel, colliding changes should be regenerated from both intents,
and only contradictions should reach a human.

Then we did the only honest thing you can do with a thesis like that: **we stopped merging
our own code.** Since v0.1, every feature of cafecito — the multi-language oracle, lockfile
regeneration, verification-fact memoization, wave-parallel gates, the swarm, the PR gateway —
has been landed *through cafecito*. The landed log stands at **23 landings, 0 escalations**,
each an engine-authored commit with a `Changeset-Id` trailer. `git log cafecito/main` is the
changelog. The suite grew from 36 tests to 100, all of them landed by the machinery they test.

Here is what we learned, including the part where we broke it.

## Two agents, two changesets, one commutation

For v0.4.0 we needed two features at once — `cafecito swarm` (one goal in, a parallel fleet
out) and `cafecito watch` (a live fleet dashboard). So we ran the experiment on ourselves:
one scaffold changeset defining the shared contract, then **two coding agents in parallel
worktrees off the same tip**, each building its module, forbidden from touching shared files.

The first agent's changeset landed. The second — authored against the *pre-first* tip —
landed on the *post-first* tip with `regenerated: false`. No rebase, no reconciler, no human.
Two agents' work, provably disjoint, commuting through the product they were building. That
is the whole thesis in one log entry.

Then we pointed the result at a fresh repo and recorded it, unedited:

![cafecito swarm — 31 seconds, real](https://raw.githubusercontent.com/cafecitohq/cafecito/main/examples/swarm-demo.gif)

One sentence → a planner decomposes it → three agents build in parallel under advisory
leases → three gated landings → main ends green with more tests than it started with. 31
seconds, nothing simulated.

## The part where we broke it

Honesty is the house style, so: **v0.5.0 and v0.5.1 are broken releases**, and the story of
why is the most useful thing in this post.

Building wave-parallel gates, a refactor spliced a region of the engine — and silently
swallowed the neighboring `advance()` method. The landing gate let it through, because **no
test had ever pinned `advance`**. Our own doctrine — *the gate only catches what tests pin* —
held perfectly. Our coverage had a hole, and the doctrine walked right through it.

It compounded beautifully: `advance` was the tool our release process used to keep the landed
branch synced with out-of-band commits, so the release batch failed *silently at exactly that
step*, the branches diverged, and v0.5.1 was tagged **without containing its own fix**.

Recovery: one reconciling merge (no history rewrites on a public repo), regression tests for
everything the splice could have touched, both bad tags left immutable, v0.5.2 as the
superseding release, and two new rules in the book — grep a splice for every `def` it spans,
and a release isn't done until the branch heads are verified identical.

If you're evaluating tools like ours, ask every vendor for this story. A landing gate is a
property of your *test suite*, not of anyone's engine — including ours.

## The gateway ate its first PR

v0.7.0 added `cafecito ingest`: keep your normal GitHub flow, open PRs, and a poller lands
them through the plane — commute, regenerate, or escalate — then reports the verdict back as
a comment and label. Fork PRs included; re-pushed heads re-ingested; never auto-closed.

Its first real run is public:
[PR #1](https://github.com/Cafecitohq/cafecito/pull/1) on our own repo — opened, ingested,
landed as an engine-authored commit, labeled `cafecito:landed`, commented with the receipt,
closed by a human. The feature's first production input was the pull request that documents
it.

## What four days of self-hosting proved

- **The three exits are sufficient for real development.** Twenty-three landings: most
  commuted; collisions were regenerated (live, against the accumulated tip — v0.8.0 even
  feeds gate failures back into a retry regeneration); nothing needed a human to stare at
  conflict markers. Zero escalations — though our benchmark corpus shows escalations firing
  exactly when they should (contradictory acceptance criteria).
- **Memoized verification is what makes parallel landing cheap.** When two concurrent
  submissions race and one re-lands on the moved tip, its re-gate inherits verification facts
  for every test whose input closure didn't change — the log shows `memo` hits and `raced`
  counters on real landings.
- **The gate is the gate.** Every landing, including textually clean merges, runs real tests.
  We enforced this after our benchmark landed two red mains without it, and the v0.5.0 saga
  re-taught it from the other direction: the gate is exactly as strong as your tests.

## Try it on your repo

```sh
pipx install git+https://github.com/cafecitohq/cafecito
cafecito init --repo . --test-cmd "python3 -m pytest -q"
cafecito swarm "your goal here" --agents 3     # or: cafecito ingest, for your open PRs
cafecito watch                                  # and watch it land
```

Numbers, methodology, and the honesty boxes live in the repo:
[the benchmark](https://github.com/cafecitohq/cafecito/tree/main/bench) (now including a
generously-modeled speculative-queue baseline) and
[the experiments](https://github.com/cafecitohq/cafecito/tree/main/phase0).

*— the cafecito authors · hello@cafeci.to*

*cafecito is Apache-2.0. The coffee is load-bearing. ☕*
