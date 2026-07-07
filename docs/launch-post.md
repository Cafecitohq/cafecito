# 97% of concurrent code changes don't conflict. Your merge queue serializes 100% of them.

*DRAFT — for cafecito.sh and the OSS launch. Numbers as of 2026-07-07; every one of them is
reproducible from the repo.*

---

Run five coding agents against one repository and watch them gridlock. The first one to merge
forces the other four to rebase, rerun their tests, and rejoin the queue. Run thirty and the
queue becomes the product: your fleet writes code in minutes and spends hours waiting to land
it, while your CI bill grows quadratically — every landing re-validates everyone still in
flight.

We measured how much of that waiting is necessary. The answer is almost none of it, and the
tool that comes out of that measurement is called **cafecito**. It's open source, Apache-2.0,
and it landed its own test suite — written by uncoordinated agents — through its own pipeline
before we wrote this post.

## The measurement

Merge queues exist because git can't tell "independent" from "colliding" — conflicts are
detected by textual overlap, so the system assumes everything collides and serializes
everything. We wanted the actual number.

We reconstructed 5,400+ real PR branches from the merge history of ten busy repositories
(numpy, sympy, scipy, matplotlib, astropy, pillow, pip, pytest, statsmodels, OpenStack nova),
paired the ones that were genuinely in flight at the same time, and derived symbol-level write
sets for each — which functions, classes, and files each change actually touches.

**Across 1,465 concurrent pairs, 97.4% are write-set-disjoint** (range: 93.8–100% per repo).
They provably commute: land them in either order or simultaneously and you get the same
result. A merge queue serializes all of them anyway.

The conflicts that do exist are rarer than anyone assumes. We exhaustively checked every
concurrent pair that shared even one file — 4,752 of them — with a
pair-attributed 3-way merge: **5 genuine conflicts**. About 0.1%.

Two things we had to get right to trust these numbers:

- **Attribution.** Naively merging two branch heads blames the pair for every third-party
  commit that landed between their branch points. In our first scan, 22 of 25 "conflicts"
  were this artifact — mainline drift, not the pair. Serialized queues amplify exactly this
  drift, because every landing moves everyone else's base. (The fix is a rebase simulation
  with git plumbing; it's ~60 lines and it's in the repo.)
- **Selection bias, stated plainly.** Human repos under-produce conflicts: maintainers
  coordinate socially before conflicts form. Agent fleets don't. So we ran the experiment
  agents deserve: 33 headless coding agents, one base commit, realistic backlog tasks
  concentrated on hotspot files, zero coordination. Conflict density came out ~27× the human
  corpus — **and symbol-disjointness still held at 97.0%**. File-level locking would have
  serialized 19–43% of those pairs needlessly. The parallelism is real even under contention;
  you just need a finer lens than "same file".

## The three exits

cafecito is built on one economic observation: agent fleets invert the cost model of
integration. Generating code is nearly free; verification and coherence are the scarce
resources. Once regenerating code costs pennies, *merging text is the wrong operation* —
and resolving conflicts by hand is a human-era ritual.

So in cafecito, no collision is ever "resolved." Every changeset takes one of three exits:

- **Commute** — write sets provably disjoint (97% of the time): land in parallel, no rebase,
  and under memoized verification, no re-testing of things that didn't change.
- **Regenerate** — a true collision: a fresh reconciler agent re-derives the colliding
  regions from *both changes' intents*, and the result must pass the landing gate. Nobody
  edits conflict markers. On every genuine conflict we could validate — human corpus and
  agent corpus — regeneration produced a state where **both sides' test suites stayed green
  in 14 of 16 cases.**
- **Escalate** — the remaining 2 of 16, and they're the system working. Example: two agents
  independently assigned "add `__repr__`" chose different formats, each with a test asserting
  its exact output. The acceptance criteria contradict; no merge can satisfy both; a human
  must choose. cafecito's job is to catch that, not paper over it.

One result mattered more than we expected: we scored regenerations two ways, with a cheap
line-incorporation heuristic and with real dual test-suite execution. The heuristic was wrong
in *both directions* on 6 of 14 conflicts — false accepts the tests caught, false rejects the
tests overturned. **Heuristics and oracles can only optimize. Verification is the gate.**
That sentence is load-bearing in the architecture.

## MergeBench: a real burst, landed for real

We replayed the 33-agent fleet through three integration strategies. The operations are real —
measured per-changeset CI, real merges, live regeneration against accumulated main, a test
gate on every landing — and the schedules are computed from those measured durations.

At a projected 10-minute full-suite CI:

| strategy | wall-clock to land 33 changesets | CI compute incl. re-validation |
|---|---|---|
| serial merge queue | 5.50 h | 93.5 h |
| file locking | 3.03 h | 29.7 h |
| **cafecito** | **1.37 h** | **16.2 h** |

The serial line grows with fleet size forever; cafecito's steps only when the conflict graph
forces a new wave. The compute line is the one your CFO cares about: quadratic versus
near-linear.

And the landing wasn't simulated: 30 of 33 changesets landed automatically (7 via live
regeneration), the 3 escalations were exactly the contradictory/duplicate cases a human
should see, and the final main was **green — checked by executing the combined test union,
not assumed.**

We should tell you what it took to get that green: two red mains during development. Once
from conflict markers sneaking through an uncovered file; once from an *ungated clean merge*
flipping behavior underneath an already-landed test — the "silent risk" category from our
measurements, observed live. Both failures are why cafecito now gates **every** landing,
clean textual merges included. The benchmark taught us our own safety model was not optional.

## It dogfoods

cafecito v0.1 shipped with zero unit tests — so the first thing we did was point it at its
own repository and hand a fleet of agents a backlog. Four for four landed: the tests now
protecting the oracle, the diff3 segmentation, and the gate — written by uncoordinated
agents, landed through the pipeline they test.

Dogfooding also found our first bug within minutes (agent ids containing `/` crashed worktree
creation). The fix was landed *through cafecito while that code path was broken* — the submit
path didn't depend on it. The engine's first real landing was its own bugfix. The log is in
[DOGFOOD.md](../DOGFOOD.md), findings and all.

## Try it

v0.1 is a single-repo control plane, zero dependencies beyond git and Python:

```sh
pipx install git+https://github.com/cafecitohq/cafecito
cafecito init --repo /path/to/your/repo --test-cmd "python3 -m pytest -q"
claude mcp add cafecito -- cafecito serve --repo /path/to/your/repo
```

Any MCP-capable agent — Claude Code, Cursor, Antigravity — gets four tools: `sync`,
`reserve` (advisory leases, so contention is discovered *before* work is wasted), `submit`,
`status`. Main is materialized as a normal git branch; humans and CI see ordinary commits;
agents never rebase and never see a conflict marker.

## Honesty box

The regeneration corpus is small (n=16 validated conflicts — genuine ones are rare, which is
itself the finding). The fleet experiment is one repo, hotspot-biased by design. The
benchmark baseline is a classic serial queue; speculative queues improve utilization but
still serialize semantics. The 10-minute CI is a projection over measured schedules. v0.1
serializes landing bookkeeping (correctness identical to the parallel design; throughput work
pending), speaks symbol-level Python (file-level for everything else), and has process-level
sandboxing. Every number in this post regenerates from `phase0/` and `bench/` in the repo —
if you get different numbers on your repos, we want the data.

## What we're asking for

- **Run experiment A on your repo.** One command, no API keys. We especially want
  merge-commit workflows outside scientific Python.
- **tree-sitter write-set extractors** for TypeScript, Go, Rust.
- **Argue with [SPEC.md](../SPEC.md).** The changeset format, lease semantics, and landed-log
  design are drafts; holes poked now are cheap.

The endgame is bigger than a faster queue: an integration layer where the unit of
contribution is intent plus acceptance tests, code is a derived artifact, and "merge
conflict" is a term you explain to junior engineers along with punch cards. The physics
says it works. The repo shows it working.

*— the cafecito authors*

*cafecito is Apache-2.0. The coffee is load-bearing.*
