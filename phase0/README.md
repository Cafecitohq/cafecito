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

**v0 validation, stated honestly:**

- `parse` — output must be valid Python.
- `incorporation` — fraction of each side's added lines present in the merged output;
  PASS needs ≥ 0.6 on *both* sides plus a clean parse.

This is a proxy for "both intents survived," **not** semantic correctness. The real gate —
running both branches' test suites against the merged state in a sandbox — is the next
milestone here. Numbers below are labeled v0 accordingly.

Other known limitations: commit messages are a weak stand-in for true intents (production
changesets carry explicit intent + acceptance criteria); pairs with >2 conflicted files or
non-Python conflicts are skipped in v0; single-ecosystem bias (scientific Python) until more
repos are measured.

## Results — 2026-07-06 (raw JSON in `workdir/results/`)

### Experiment A (branches merged since 2024-06-01; up to 400 sampled concurrent pairs)

| repo | branches | pairs | symbol-disjoint | file-disjoint | textual conflicts | oracle win | silent risk |
|---|---|---|---|---|---|---|---|
| numpy | 397 | 202 | **99.0%** | 98.0% | 0.0% | 1.0% | 1.0% |
| sympy | 696 | 212 | **93.9%** | 91.0% | 0.0% | 2.8% | 6.1% |
| matplotlib | 994 | 90 | **98.9%** | 95.6% | 0.0% | 3.3% | 1.1% |
| scipy | 793 | 125 | **100.0%** | 98.4% | 0.0% | 1.6% | 0.0% |

Exhaustive conflict scan (every file-sharing concurrent pair, pair-attributed):
**3 genuine conflicts in 2,990 pairs scanned** (numpy 0/105, sympy 2/1121, matplotlib 1/1393,
scipy 0/371).

### Experiment B (all usable attributed conflicts; model: sonnet)

| repo | pairs tried | regen PASS (v0) | notes |
|---|---|---|---|
| sympy | 2 | 1 | FAIL is a rename-PR × logic-fix pair scoring 0.5 incorporation — verbatim-line matching can't credit a merge that correctly *rewrites* the fix under new names; test-execution validation needed to adjudicate |
| matplotlib | 0 | — | its only conflict is a YAML workflow file (v0 is Python-only) |

### Findings so far

1. **The commutativity hypothesis survives, with room to spare.** We hypothesized >70%
   symbol-disjoint; measured 93.9–100%. A merge queue serializes 100% of these pairs.
2. **Pair-attributable conflicts are vanishingly rare** (~0.1% of even file-sharing concurrent
   pairs). Most "merge conflicts" developers hit in queues are accumulated mainline drift
   across many changes — which serialized rebase-retest loops *amplify*, since every landing
   moves everyone else's base. Parallel landing of commuting changes attacks the cause.
3. **Silent risk is real but small** (0–6.1%): pairs git auto-merges cleanly despite touching
   the same symbol. This is the case for symbol-aware gating over raw textual merges.
4. **Selection-bias caveat, stated plainly:** these are *human* repos, where maintainers
   coordinate socially before conflicts form, and abandoned/painful branches never merge (so
   never enter the corpus). Agent fleets lack that implicit coordination — true conflict
   density will be higher. Measuring on an agent-generated corpus is the next data milestone;
   leases exist precisely to replace the social coordination humans do for free.
5. **Regenerative merge needs semantic validation before the number is claimable.** The v0
   incorporation heuristic under-credits legitimate rewrites (see the rename FAIL). Milestone:
   sandboxed dual test-suite execution per merged state.
