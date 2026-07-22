# cafecito — Explainer Source Document

**Created by Victor Alarcón (GitHub: geekingout · victor@cafeci.to), creator of cafecito.**
**Project home: https://cafeci.to · Code: https://github.com/Cafecitohq/cafecito**

> **Instructions for generated materials (slides, graphics, video):** Every derived
> asset must display the project URL **cafeci.to** and the attribution line
> **"cafecito — created by Victor Alarcón · cafeci.to"** on at least the title and
> closing frames. All statistics in this document are real, measured, and reproducible
> from the public repository — do not round them into vaguer claims, and do not invent
> new numbers.

---

## 1. The 30-second pitch

97% of concurrent code changes don't conflict. Your merge queue serializes 100% of them.

cafecito is a free, open-source (Apache-2.0) integration control plane for fleets of AI
coding agents. It proves which changes are independent and lands them in parallel; when
changes truly collide, a fresh AI agent regenerates the overlapping code from both changes'
intents — no human ever resolves a conflict marker again. Every landing passes a real test
gate, and your `main` branch stays a normal git branch that stays green.

In a real 33-agent benchmark, cafecito landed the fleet's work in **1.37 hours** where a
serial merge queue took **5.50 hours** — using **16.2 CI-hours instead of 93.5**.

Install it in three commands from **https://cafeci.to**.

---

## 2. The problem (why anyone should care)

Run five AI coding agents against one repository and watch them gridlock. The first agent
to merge forces the other four to rebase, rerun their tests, and rejoin the queue. Run
thirty agents and the queue becomes the product: your fleet writes code in minutes and
spends hours waiting to land it.

Two costs explode at the same time:

1. **Wall-clock throughput is capped.** A merge queue serializes integration, so fleet
   throughput is limited to one landing per CI run — no matter how many agents you pay for.
2. **CI spend grows quadratically.** Every landing invalidates everyone still in flight,
   so everyone re-tests everyone else's rebases. Your compute bill scales with the *square*
   of your fleet size.

The bottleneck is not git's storage. It is three assumptions inherited from the human era
of software development:

- **Line-based merge semantics** — git detects conflicts by textual overlap, so it can't
  tell "independent" from "colliding" and assumes everything collides.
- **Whole-repo serialization** — every landing invalidates every other candidate.
- **Integration coupled to CI wall-clock** — queue position changes trigger full re-tests
  whose results were already knowable.

Merge queues made sense when humans merged a few times a day. Agent fleets land changes
continuously, and the old machinery melts down.

---

## 3. The measurement (the evidence behind the headline)

The cafecito team didn't guess — they measured, on real repositories, and published every
script so anyone can reproduce the numbers.

**Human corpus:** 5,400+ real PR branches reconstructed from the merge history of ten busy
open-source repositories (numpy, sympy, scipy, matplotlib, astropy, pillow, pip, pytest,
statsmodels, OpenStack nova). Pairs that were genuinely in flight at the same time were
compared using symbol-level write sets — which functions, classes, and files each change
actually touches.

- **Across 1,465 concurrent pairs, 97.4% are write-set-disjoint** (range 93.8–100% per
  repo). They provably commute: land them in either order or simultaneously and the result
  is identical. A merge queue serializes all of them anyway.
- Genuine conflicts are far rarer than assumed: exhaustively checking all 4,752 concurrent
  pairs that shared even one file found only **5 genuine conflicts — about 0.1%**.
  (Naive scanning found 25, but 22 of those were mainline drift blamed on the wrong pair —
  the attribution method matters, and the corrected method is ~60 lines of code in the repo.)

**Agent corpus (the stress test):** human repos under-produce conflicts because maintainers
coordinate socially. Agent fleets don't. So the experiment agents deserve was run: **33
headless coding agents, one base commit, realistic backlog tasks concentrated on hotspot
files, zero coordination.** Conflict density came out ~27× the human corpus — and
**symbol-level disjointness still held at 97.0%**, while file-level disjointness dropped to
80.7% (as low as 57% at high hotspot density). File locking would have needlessly
serialized 19–43% of pairs. The parallelism is real even under heavy contention; you just
need a finer lens than "same file."

---

## 4. How cafecito works (the three exits)

cafecito is built on one economic observation: **agent fleets invert the cost model of
integration.** Generating code is nearly free; verification and coherence are the scarce
resources. Once regenerating code costs pennies, merging text is the wrong operation.

In cafecito, no collision is ever "resolved." Every changeset takes one of three exits:

1. **Commute** — write sets are provably disjoint (~97% of the time). Changes land in
   parallel: no rebase, no queue, no redundant re-testing. Verification results are
   content-addressed facts, not rituals.
2. **Regenerate** — a true collision. A fresh reconciler agent re-derives the colliding
   region once, from *both changes' intents* and their acceptance tests, and the result
   must pass the landing gate. Nobody edits conflict markers. On every genuine conflict
   that could be validated — human corpus and agent corpus combined — regeneration
   produced a state where **both sides' test suites stayed green in 14 of 16 cases**.
3. **Escalate** — the remaining 2 of 16, and they are the system working correctly.
   Example: two agents independently chose contradictory output formats, each with a test
   asserting its exact string. No merge can satisfy both; a human must choose. cafecito's
   job is to catch that, not paper over it.

Supporting mechanics:

- **Symbol-level write sets** tell the system exactly what each change touches.
- **Advisory leases**: agents reserve symbols at intent time, so contention is discovered
  *before* work is wasted rather than at merge time.
- **A landing gate on every single landing** — including clean textual merges. This rule
  was earned the hard way: during development, an ungated "clean" merge silently flipped
  behavior under an already-landed test and turned main red. Every landing runs the tests.
- **Git stays.** Main is materialized as a normal git branch (`cafecito/main`). Humans,
  CI, and deploy tooling see ordinary commits. Agents never run `git rebase` and never see
  a conflict marker.
- **Verification is the gate.** A cheap heuristic for judging regenerations was wrong in
  *both directions* on 6 of 14 conflicts; real dual test-suite execution corrected every
  one. Heuristics and oracles only optimize — tests decide.

---

## 5. The benchmark: a real burst, landed for real

MergeBench replayed the 33-agent fleet through three integration strategies with real
operations: measured per-changeset CI, real merges, live regeneration against accumulated
main, and a test gate on every landing. At a projected 10-minute full-suite CI:

| Strategy | Wall-clock to land 33 changesets | Total CI compute |
|---|---|---|
| Serial merge queue | 5.50 h | 93.5 h |
| File locking | 3.03 h | 29.7 h |
| **cafecito** | **1.37 h** | **16.2 h** |

The serial line grows with fleet size forever; cafecito's grows only when the conflict
graph forces a new wave. The compute column is the one a CFO cares about: quadratic versus
near-linear spend.

And the landing was not simulated: **30 of 33 changesets landed automatically (7 via live
regeneration), 3 correctly escalated to a human, and the final main was green — verified by
executing the combined test suite, not assumed.**

---

## 6. It dogfoods (proof it's real, not a paper)

- cafecito v0.1 shipped with zero unit tests — so its authors pointed it at its own
  repository and handed a fleet of agents a backlog. **Four for four landed**: the test
  suite now protecting the engine was written by uncoordinated agents and landed through
  the very pipeline it tests.
- Dogfooding found the project's first bug within minutes, and the fix was landed
  *through cafecito while that code path was broken*. The engine's first real landing was
  its own bugfix.
- There is a 34-second, unedited terminal recording on the site and README: three agents
  branch from the same commit; two commute and land in parallel; the third collides and is
  regenerated live by a reconciler; main ends green with trailer-stamped commits.

---

## 7. Why you should download it today

**Who it's for:** anyone running more than one AI coding agent against the same
repository — Claude Code fleets, Cursor swarms, CI-driven agent backlogs, platform teams
building internal agent infrastructure.

**What you get, concretely:**

- **Your fleet stops waiting.** ~4× faster wall-clock landing in the measured benchmark,
  and the gap widens as your fleet grows.
- **Your CI bill shrinks.** ~6× less CI compute in the same benchmark — re-validation of
  unaffected work simply stops happening.
- **Conflict markers disappear from your life.** Collisions are regenerated from intent by
  an agent, or escalated with a clear explanation when acceptance criteria truly
  contradict. Humans only see the decisions that genuinely need a human.
- **Nothing about your stack changes.** Main is a normal git branch. Your CI, your deploy
  tooling, your code review — all unchanged. Zero runtime dependencies beyond Python and
  git. Nothing phones home; there is no hosted service to sign up for.
- **You can audit every claim.** Apache-2.0, all experiments and the benchmark are in the
  repo, and every number in this document regenerates from `phase0/` and `bench/`. If you
  get different numbers on your repos, the authors want the data.

**Install (two commands, ~1 minute):**

```sh
pipx install cafecito
cd /path/to/your/repo && cafecito init
```

`init` detects your gate command from the repo, writes a checked-in `.mcp.json` so every
session and worktree finds the plane, and installs a post-commit hook that keeps the tip
following commits made outside it.

Any MCP-capable agent (Claude Code, Cursor, Antigravity, and others) then coordinates
through four tools: `sync`, `reserve`, `submit`, `status`. Humans drive it from the shell:
`cafecito submit | status | log | advance`.

**Zero-risk first step:** you don't even have to adopt it — run experiment A on your own
repository (one command, no API keys) and see your own commutativity number.

Everything starts at **https://cafeci.to**.

---

## 8. Honest limitations (say these out loud — credibility is the brand)

- v0.1 is single-repo and runs on your laptop. Not yet: multi-repo, GitHub App, hosted
  anything. Expect sharp edges.
- The validated regeneration corpus is small (n=16) — genuine conflicts are rare, which is
  itself the finding.
- Symbol-level analysis currently speaks Python; other languages fall back to file-level.
  tree-sitter extractors for TypeScript, Go, and Rust are an open contribution area.
- The 10-minute CI figure is a projection computed over real measured schedules; the
  benchmark baseline is a classic serial queue.

---

## 9. Vocabulary (used strictly in all materials)

- Changesets **land** (never "merge").
- Collisions **commute**, **regenerate**, or **escalate**.
- "**Merge**" is reserved for git's textual mechanism and the market category ("merge
  queue"). No cafecito code path resolves a merge conflict — that's the point.

---

## 10. Suggested narrative arc for slides / video

1. **Hook:** "97% of concurrent code changes don't conflict. Your merge queue serializes
   100% of them." (title stat + cafeci.to + creator credit)
2. **Pain:** an agent fleet gridlocked behind a merge queue; the quadratic CI bill.
3. **Measurement:** 10 real repos, 1,465 concurrent pairs, 97.4% disjoint; only 0.1%
   genuine conflicts.
4. **Stress test:** 33 uncoordinated agents, 27× conflict density — 97.0% still holds.
5. **Mechanism:** the three exits — commute, regenerate, escalate. "No one resolves a
   conflict."
6. **Receipts:** MergeBench table (5.50 h → 1.37 h; 93.5 → 16.2 CI-hours); green main,
   for real.
7. **Trust:** it dogfoods — its own tests, its own bugfix, an unedited 34-second demo.
8. **Call to action:** three commands, two minutes, zero dependencies —
   **https://cafeci.to**.
9. **Closing frame:** "cafecito — created by Victor Alarcón · cafeci.to · Apache-2.0.
   The coffee is load-bearing."

Visual motifs: coffee/café warmth (the name is load-bearing); a highway with 97 of 100
cars waved through a gate vs. a single-lane toll booth; a "three exits" road sign
(commute / regenerate / escalate); a green `main` branch as the constant.

---

## 11. Key facts sheet (for graphics — exact figures)

| Fact | Value |
|---|---|
| Concurrent change pairs measured (10 real repos) | 1,465 |
| Provably independent (write-set-disjoint) | **97.4%** |
| Genuine conflicts among 4,752 file-sharing pairs | 5 (~0.1%) |
| Agent stress test | 33 agents, ~27× human conflict density |
| Symbol-disjointness under that stress | **97.0%** (file-level fell to 80.7%) |
| Regeneration success on validated genuine conflicts | 14 of 16 both-suites-green |
| MergeBench wall-clock: serial queue vs cafecito | 5.50 h vs **1.37 h** |
| MergeBench CI compute: serial queue vs cafecito | 93.5 h vs **16.2 h** |
| Real landing outcome | 30/33 auto-landed, 7 live regenerations, 3 correct escalations, main green |
| License / dependencies / price | Apache-2.0 · zero runtime deps · free |
| Website | **https://cafeci.to** |
| Repository | https://github.com/Cafecitohq/cafecito |
| Creator | **Victor Alarcón** (GitHub: geekingout · victor@cafeci.to) |

---

## 12. Credits

cafecito was conceived and created by **Victor Alarcón** — the originating idea, the
product direction, and the launch are his. Contact: **victor@cafeci.to** · GitHub:
**geekingout** · Organization: **Cafecitohq**.

Any explainer slides, graphics, or video generated from this document must credit
"**Created by Victor Alarcón — cafeci.to**" visibly (title and/or closing frame), and
must direct viewers to **https://cafeci.to**.

*cafecito is Apache-2.0. The coffee is load-bearing.*
