# Phase 0 — validating the physics

Two falsification experiments behind cafecito's core bets. Both run against real repositories,
stdlib-only Python, no API keys needed for A (B shells out to the `claude` CLI).

## Method: reconstructing genuinely-concurrent changes

Every mainline merge commit `M` (parents: mainline `P1`, feature head `P2`) reconstructs a real
PR branch: it diverged at `merge-base(P1, P2)` and landed when `M` was committed. Two branches
whose development intervals overlap in time were **in flight simultaneously** — exactly the
pairs today's merge queues force through serial rebase-retest cycles. This gives us true
parallel development (not linear-history simulation) straight from git history, no GitHub API.

Noise filters: branches with >50 commits or >100 changed files are dropped (release trains,
vendored bumps); stacked branches (one contains the other) are excluded.

## Experiment A — commutativity rate

For each concurrent pair, three measurements:

| Measure | Question | Mechanism |
|---|---|---|
| textual | does a *pair-attributed* 3-way merge conflict? | rebase simulation, `attrib.py` (below) |
| file | do the branches touch a common file? | diff name intersection |
| symbol | do symbol-level write sets intersect? | `oracle.py` — changed lines mapped to innermost enclosing def/class via `ast`; non-Python and unparseable files degrade to whole-file granularity |

**Conflict attribution matters.** Naively merging two branch heads uses their mutual
merge-base, so each "side" of the 3-way includes every third-party mainline commit that landed
between the two branch points — conflicts get blamed on the pair that didn't cause them. In our
first (naive) scan, 22 of 25 detected conflicts were this drift artifact. `attrib.py` replays
the older branch onto the newer branch's base (merge-tree + commit-tree plumbing, no worktree)
and merges from there, so a reported conflict is strictly between the two changes.

Headline metrics:

- **symbol-disjoint %** — pairs cafecito lands in parallel: no rebase, and under
  verification-fact memoization, no re-test. Baseline: a merge queue serializes 100%.
- **oracle win %** — pairs touching the *same file* whose symbols are disjoint: the advantage
  over file-level locking (Perforce-style).
- **silent risk %** — pairs git merges *cleanly* despite symbol-level collision: changes to the
  same function auto-merged without any signal. The safety argument for symbol-aware gating.

```sh
python3 experiment_a.py --repo workdir/repos/numpy --since 2024-06-01 --max-pairs 400
```

## Experiment B — regenerative-merge success rate

For textually-conflicting pairs from A: no one resolves the conflict. A fresh reconciler agent
receives the BASE version, both sides' complete file versions, and both sides' intents (commit
messages as proxy), and regenerates the merged file(s) from scratch.

```sh
python3 experiment_b.py --repo workdir/repos/numpy --max-pairs 4 --model sonnet
```

**Two validation tiers:**

- *v0 heuristic* (`experiment_b.py`): output parses + ≥60% of each side's added lines
  incorporated. Cheap, but structurally blind to legitimate rewrites (a regeneration that
  correctly renames the other side's code scores low).
- *Semantic* (`validate_b.py`): dual test-suite execution. Three states are materialized as
  detached worktrees — OURS (one branch replayed onto the other's base), THEIRS (the other
  head), and MERGED (the attributed merge tree with regenerated regions spliced in). Each
  side's tests (test files that side changed + the sibling `tests/test_<stem>.py` of each
  conflicted source) first run in their **home** state; only home-passing tests count as
  signal, which filters flaky/broken/env-incompatible tests per side. Verdict `pass` requires
  every surviving test green in MERGED **and** ≥1 surviving test on each side (otherwise
  `partial` — never silently upgraded).

Sandboxing v1 is process-level: dedicated venv, detached worktrees, temp HOME, CPU rlimit,
per-file wall-clock timeout. Same trust level as running an OSS project's tests locally;
container isolation is future hardening for untrusted corpora.

Other known limitations: commit messages are a weak stand-in for true intents (production
changesets carry explicit intent + acceptance criteria); pairs with >2 conflicted files or
non-Python conflicts are skipped in v0; single-ecosystem bias (scientific Python) until more
repos are measured.

## Agent-generated corpus — `agent_corpus.py`

The human corpus above under-produces conflicts by construction: maintainers coordinate
socially before conflicts form, and painful branches never merge. Agent fleets have neither
property. `agent_corpus.py` measures that directly: a model drafts a realistic sprint backlog
scoped to hotspot files, each task goes to a fresh headless agent (`claude -p`, edit-only
tools) in its own worktree at the **same base commit**, blind to the other agents; non-empty
diffs are committed with the task brief as the message — so downstream **intents are real
intents**, not commit-message proxies. Pairs get the experiment A treatment (attribution is
trivial: shared base), and conflicts flow into `experiment_b.py` / `validate_b.py` unchanged
via `--results workdir/results/agent`.

Deliberate bias, stated up front: tasks concentrate on hotspot files to measure conflict
*behavior under contention*, not fleet-wide conflict *rates*.

### Scaled fleet (sympy @ 2026-07-06 · 33 agents across 2 runs · 5 target files · model: sonnet)

Run 2 (25 agents, 5 hotspots) folded together with run 1 (8 agents) via `--include` — all at
the same base commit. Two runs assigning near-identical tasks to different agents is kept
deliberately: redundant assignment is a real fleet pattern, and it produced the most
instructive conflicts.

| metric | agent corpus (528 pairs) | human corpus (1,465 pairs) | read |
|---|---|---|---|
| file-disjoint | **80.7%** | 91–100% | uncoordinated agents pile onto shared files (57.1% in the 2-hotspot run) |
| symbol-disjoint | **97.0%** | 97.4% | …but still mostly touch different symbols; the oracle's parallelism survives contention |
| textual conflict | **2.7%** (14 pairs) | ~0.1% | ~27× human conflict density, per the selection-bias prediction |
| silent risk | 1.3% | 2.5% | clean textual merge, same symbol touched |

**Regenerative merge on all 14 conflicts — heuristic vs. semantic (dual test-suite) gate:**

| | semantic PASS | semantic FAIL |
|---|---|---|
| heuristic PASS | 8 | **2** ← false-accepts caught by tests |
| heuristic FAIL | **4** ← legitimate rewrites vindicated by tests | 0 |

**Semantic verdict: 12/14 (86%).** Combined with the human corpus (2/2 sympy), regeneration
dissolves **14 of 16** validatable real conflicts. The line-level heuristic was wrong in both
directions on 6/14 pairs — the empirical case for the product's safety model: the oracle and
heuristics only *optimize*; content-addressed verification (tests) is always the landing gate.

The two semantic FAILs are the corpus earning its keep:

1. **A true conflict.** Two agents independently assigned "add `__repr__` to `SympifyError`"
   chose different repr formats, each with a test asserting its exact output. The acceptance
   criteria *contradict* — no merge can satisfy both. Regeneration correctly cannot dissolve
   this; the plane must serialize or supersede (SPEC §5 fallback). The heuristic passed it
   at 0.75; the semantic gate caught it.
2. **The shadowing guard fired on its maiden run.** Both agents fixing the same cache bug
   added a test named `test_sympy_cache_size_bad_value`; the reconciler's regeneration did
   not preserve that def name, so the def-union check failed the pair even though the merged
   file's tests are green. Deliberately strict: acceptance tests must survive *by name* at a
   landing gate. (A rename-aware matcher could soften this; strict-and-explainable wins v0.)

## Multi-language oracle validation — 2026-07-07 (`experiment_a_multilang.py`)

Same methodology, write sets from the shipped product oracle (`cafecito.writeset`:
Python `ast` + stdlib span scanners for js/ts/go, landed as cs_3c36a4be38).

| repo | lang | pairs | coverage | symbol-disjoint | textual conflicts |
|---|---|---|---|---|---|
| prometheus | Go | 193 | 99.7% | **98.4%** | 0.0% |
| nest | TS | 341 | 100.0% | 71.0% → **79.8% with JSON-key oracle** (v0.2) | 17.6% → **0.0% code** |

v0.2 follow-up: with JSON key-level write sets, nest's oracle win rose 0.3% → **9.1%**
(different-dependency bumps in package.json commute) and silent risk fell 11.4% → 2.6%.
The remaining 17.6% textual conflicts are lockfiles — handled at landing time by
deterministic regeneration (`generated` config), not by merging.

**Live end-to-end on TypeScript (2026-07-10):** beyond history mining, the full engine loop
ran on a TS fixture — a 3-agent swarm writing TypeScript, gated by **vitest** (0.35s gates),
3/3 landed, tip verified green. The loop is language-agnostic in practice, not just in the
oracle. (Operational note: JS gate worktrees need the test runner reachable — solved first-class in
v0.10 by the `setup_cmd` gate step: the same repo now gates with `npm ci` + plain
`npx vitest run`, 1.9s per landing.)

The nest anomaly is a finding, not a failure: **58 of 60 conflicting pairs conflict only in
`package-lock.json`**, and every remaining file overlap is a `package.json` — dependency-bump
churn. TypeScript *code* had zero conflicts in 341 concurrent pairs. Consequences on the
roadmap: (a) deterministic regeneration for declared generated files (lockfiles regenerate
from manifests by running the generator — "regenerate, don't merge", literally); (b)
JSON-key-level write sets for manifests, so two PRs bumping different dependencies commute.

## Results — 2026-07-06 (raw JSON in `workdir/results/`)

### Experiment A (branches merged since 2024-06-01; up to 400 sampled concurrent pairs)

Corpus: 10 repos (run via `run_corpus.py`). Candidates with squash-merge workflows reconstruct
no branches and are auto-skipped (qutebrowser, salt, xarray fell out this way).

| repo | branches | pairs | symbol-disjoint | file-disjoint | textual conflicts | oracle win | silent risk |
|---|---|---|---|---|---|---|---|
| astropy | 1208 | 84 | **100.0%** | 100.0% | 0.0% | 0.0% | 0.0% |
| matplotlib | 994 | 90 | **98.9%** | 95.6% | 0.0% | 3.3% | 1.1% |
| nova | 60 | 178 | **97.8%** | 95.5% | 0.0% | 2.2% | 2.2% |
| numpy | 397 | 202 | **99.0%** | 98.0% | 0.0% | 1.0% | 1.0% |
| pillow | 782 | 253 | **97.6%** | 94.1% | 0.0% | 3.6% | 2.4% |
| pip | 257 | 81 | **93.8%** | 93.8% | 0.0% | 0.0% | 6.2% |
| pytest | 40 | 33 | **93.9%** | 84.8% | 0.0% | 9.1% | 6.1% |
| scipy | 793 | 125 | **100.0%** | 98.4% | 0.0% | 1.6% | 0.0% |
| statsmodels | 192 | 207 | **97.6%** | 97.6% | 0.0% | 0.0% | 2.4% |
| sympy | 696 | 212 | **93.9%** | 91.0% | 0.0% | 2.8% | 6.1% |
| **all** | 5419 | **1465** | **97.4%** | 95.4% | 0.0% | 1.8% | 2.5% |

Smaller/hotter repos show the pattern the thesis predicts: pytest (small, concentrated
hotspots) has the highest oracle win (9.1% of pairs share a file yet commute at symbol level)
and, with pip, the highest silent risk (~6%).

Exhaustive conflict scan (every file-sharing concurrent pair, pair-attributed):
**5 genuine conflicts in 4,752 pairs scanned** — numpy 0/105, sympy 2/1121, matplotlib 1/1393,
scipy 0/371, astropy 1/926, nova 0/59, pillow 1/567, pip 0/155, pytest 0/28,
statsmodels 0/27.

### Experiment B (all usable attributed conflicts; model: sonnet)

| repo | pairs tried | heuristic PASS (v0) | semantic PASS (tests) | notes |
|---|---|---|---|---|
| sympy | 2 | 1 | **2/2** | the heuristic FAIL (rename-PR × logic-fix, 0.5 incorporation) is adjudicated **correct** by tests — the regeneration properly rewrote the fix under the renamed API. In pair 2, THEIRS added 4 new tests; the merged state runs all 173 green, i.e. the union of both sides' acceptance criteria holds |
| pillow | 1 | 1 | needs build | conflict is itself a test file; semantic validation requires building Pillow's C core per state (a wheel wouldn't contain the branch's C fix) — compiled-repo support is a runner milestone |
| matplotlib | 0 | — | — | its only conflict is a YAML workflow file (v0 is Python-only) |
| astropy | 0 | — | — | its only conflict is `tox.ini` (v0 is Python-only) |

### Findings so far

1. **The commutativity hypothesis survives, with room to spare.** We hypothesized >70%
   symbol-disjoint; measured 93.8–100% across 10 repos and 1,465 pairs (aggregate 97.4%).
   A merge queue serializes 100% of these pairs.
2. **Pair-attributable conflicts are vanishingly rare** (5 in 4,752 file-sharing concurrent
   pairs, ~0.1%). Most "merge conflicts" developers hit in queues are accumulated mainline
   drift across many changes — which serialized rebase-retest loops *amplify*, since every
   landing moves everyone else's base. Parallel landing of commuting changes attacks the cause.
3. **Silent risk is real but small** (0–6.1%): pairs git auto-merges cleanly despite touching
   the same symbol. This is the case for symbol-aware gating over raw textual merges.
4. **Selection-bias caveat, stated plainly:** these are *human* repos, where maintainers
   coordinate socially before conflicts form, and abandoned/painful branches never merge (so
   never enter the corpus). Agent fleets lack that implicit coordination — true conflict
   density will be higher. Measuring on an agent-generated corpus is the next data milestone;
   leases exist precisely to replace the social coordination humans do for free.
5. **Regenerative merge: 2/2 semantic PASS on the attributed corpus.** Every genuinely
   conflicting Python pair we found regenerates into a state where all of both sides'
   home-passing tests stay green — including one the line-level heuristic wrongly failed
   (line heuristics can't credit correct rewrites; tests can). n=2 is far too small to claim a
   rate; it is exactly enough to claim the *mechanism works end-to-end*. Growing n needs more
   repos and an agent-generated corpus, not a better harness — the harness is now the easy part.
