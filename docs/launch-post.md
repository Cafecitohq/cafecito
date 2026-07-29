# Your AI agents write code faster than they can merge it

*Published 2026-07-07 · revised 2026-07-28 (framing rewritten in plainer language; facts
brought up to v0.16 — see [What's shipped since launch](#whats-shipped-since-launch)) ·
every number in this post is reproducible from
[the repo](https://github.com/cafecitohq/cafecito).*

---

Point five coding agents at one repository and they stop behaving like five agents. The
first one to land forces the other four to rebuild on top of it: pull the new code, re-run
their tests, get back in line. Point thirty at it and the line *is* the product — your
fleet writes code in minutes, then spends hours waiting for permission to ship it, while
the CI bill grows quadratically, because every landing re-validates everyone still waiting.

All that waiting looks like the price of safety. We measured how much of it actually buys
you anything. The answer is: almost none of it.

The tool that came out of that measurement is called **cafecito**. It's open source,
Apache-2.0, and it landed its own test suite — written by uncoordinated agents — through
its own pipeline before we wrote this post.

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

## What happens when two changes collide

cafecito is built on one economic observation: agent fleets invert the cost model of
integration. Generating code is nearly free; verification and coherence are the scarce
resources. Once regenerating code costs pennies, *merging text is the wrong operation* —
and resolving conflicts by hand is a human-era ritual.

So in cafecito, nothing is ever "resolved." Every changeset takes one of three exits:

- **They touch different code → both land at once.** The write sets are provably disjoint,
  which is the case ~97% of the time. Both land in parallel, neither waits, and under
  memoized verification, tests that couldn't have been affected don't re-run. The term for
  it: the changes *commute*.
- **They touch the same code → an agent rewrites it.** A real collision. Instead of handing
  a person a tangle to sort out, a fresh reconciler agent re-derives the overlapping region
  from *both changes' intents*, and the result only ships if it passes the landing gate.
  Nobody edits a conflict marker. On every genuine conflict we could validate — human corpus
  and agent corpus — this produced a state where **both sides' test suites stayed green in 14
  of 16 cases.** cafecito *regenerates* the overlap rather than resolving it.
- **They want opposite things → a human decides.** The remaining 2 of 16, and they're the
  system working. Example: two agents independently assigned "add `__repr__`" chose different
  formats, each with a test asserting its exact output. The acceptance criteria contradict;
  no version of the code satisfies both; a person must choose. cafecito's job is to catch
  that, not paper over it. It *escalates*.

One result mattered more than we expected: we scored regenerations two ways, with a cheap
line-incorporation heuristic and with real dual test-suite execution. The heuristic was wrong
in *both directions* on 6 of 14 conflicts — false accepts the tests caught, false rejects the
tests overturned. **Heuristics and oracles can only optimize. Verification is the gate.**
That sentence is load-bearing in the architecture.

## MergeBench: a real burst, landed for real

We replayed the 33-agent fleet through four integration strategies. The operations are real —
measured per-changeset CI, real merges, live regeneration against accumulated main, a test
gate on every landing — and the schedules are computed from those measured durations.

At a projected 10-minute full-suite CI:

| strategy | wall-clock to land 33 changesets | CI compute incl. re-validation |
|---|---|---|
| serial merge queue | 5.50 h | 93.5 h |
| file locking | 3.03 h | 29.7 h |
| speculative queue, unlimited window | 1.83 h | 43.3 h |
| **cafecito** | **1.37 h** | **16.2 h** |

The serial line grows with fleet size forever; cafecito's steps only when the conflict graph
forces a new wave. The compute line is the one your CFO cares about: quadratic versus
near-linear. We modeled the speculative queue *generously* — free conflict discovery, offline
resolution — and it still loses on both axes at fleet conflict density, because speculation
buys wall-clock by spending compute on branches it will throw away.

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

That has held ever since: **every feature in every release since v0.1 has landed through
cafecito itself** — 49 self-hosted landings and counting. Worth stating precisely, because
it cuts against us: every one of those landings either commuted or merged cleanly. Our own
changesets have been disjoint enough that **the reconciler has never once fired on this
repo**. That's the commutativity thesis holding on real work, not evidence that regeneration
works — the evidence for that is the corpora above, where 14 of 16 genuine conflicts
regenerated with both test suites green. The only two escalations were the landing gate
correctly refusing the sandbox feature it now runs inside, until that same changeset fixed a
latent bug in our own tests. The story, including a release we broke and what it taught us, is in
[building-itself.md](building-itself.md).

## What's shipped since launch

This post was written at v0.1. The physics above hasn't changed; the tool around it has:

- **Symbol-level write sets for Python, TypeScript/JavaScript, and Go** (plus JSON key
  paths), from stdlib-only scanners. Anything unanalyzable widens safely to whole-file
  granularity — the oracle only chooses parallelism, so uncertainty is always safe.
- **Wave-parallel landing.** Gates run with the lock released; a changeset whose base moved
  under it rebases and re-gates, which is nearly free because verification facts are inherited.
- **Memoized verification.** Every landing can gate on the whole suite, but verdicts are
  content-addressed by input closure, so only tests the landing actually touched execute.
  Closures resolve Python, TS/JS, and Go inputs; anything the analysis can't see through
  statically simply runs the test instead of trusting a fact.
- **A fleet in one command.** `cafecito swarm "…"` plans a goal into independent tasks, runs
  the agents in parallel, and lands the results; `cafecito watch` is the live dashboard.
- **PRs land through the plane.** `cafecito ingest` takes ordinary GitHub PRs through the
  same gate. Its first production input was the pull request documenting it.
- **Gates run isolated.** The gate executes candidate code, so `isolation: sandbox` denies
  the network and confines writes to the gate's own worktree (macOS today; a container
  backend ships experimental). An unavailable backend reddens the gate — never a silent
  fallback to an unisolated run.
- **One-command setup.** `cafecito init` detects your gate, registers the plane so every
  clone and agent session finds it, and installs a hook so commits made *outside* the plane
  still move its tip. `--ci` scaffolds a GitHub Actions workflow: your test gate plus a
  guard that goes red the moment a commit bypasses the plane.

## Try it

Zero dependencies beyond git and Python:

```sh
pipx install cafecito
cd your-repo && cafecito init --ci
```

That's it — `init` detects how your project tests itself, writes a checked-in `.mcp.json` so
every clone and worktree finds the plane, installs the tip-following hook, and (with `--ci`)
generates the workflow. Any MCP-capable agent — Claude Code, Cursor, Antigravity — then gets
the tools: `sync`, `reserve` (advisory leases, so contention is discovered *before* work is
wasted), `submit`, `status`, `swarm`. Main is materialized as a normal git branch; humans and
CI see ordinary commits; agents never rebase and never see a conflict marker.

## Honesty box

The regeneration corpus is small (n=16 validated conflicts — genuine ones are rare, which is
itself the finding). The fleet experiment is one repo, hotspot-biased by design. The
10-minute CI is a projection over measured schedules, and the speculative-queue baseline is a
model, generously specified, not a live system we ran. Symbol granularity covers Python,
TS/JS, and Go; everything else lands at file granularity today. Sandboxed gates are macOS
today — the container backend is built but not yet proven against a live runtime. cafecito is
still a **single-repo** control plane: webhooks and a hosted multi-repo app are not built.
Every number in this post regenerates from `phase0/` and `bench/` in the repo — if you get
different numbers on your repos, we want the data.

## What we're asking for

- **Run experiment A on your repo.** One command, no API keys. We especially want
  merge-commit workflows outside scientific Python.
- **Symbol scanners for more languages** — Rust, Ruby, Java, C#. The bar is unusual: stdlib
  only, no new runtime dependencies, and conservative by construction — returning "I don't
  know" must always be safe, because the landing gate, not the scanner, is what keeps main
  green.
- **Argue with [SPEC.md](../SPEC.md).** The changeset format, lease semantics, and landed-log
  design are drafts; holes poked now are cheap.

The endgame is bigger than a faster queue: an integration layer where the unit of
contribution is intent plus acceptance tests, code is a derived artifact, and "merge
conflict" is a term you explain to junior engineers along with punch cards. The physics
says it works. The repo shows it working.

*— the cafecito authors*

*cafecito is Apache-2.0. The coffee is load-bearing.*
