# cafecito protocol — draft v0

Status: **draft, pre-implementation.** This document exists to be argued with. Open questions
are marked `[Q]`. Semantics here are constrained by the Phase 0 experiment results as they come
in ([phase0/README.md](phase0/README.md)).

## 1. Concepts

- **Changeset** — the unit of contribution. A diff *plus* machine-derived metadata: symbol-level
  write set, declared intent, acceptance criteria. Identified by content hash.
- **Write set** — the set of symbols (functions, methods, classes, config keys, schema objects;
  fallback: whole file) a changeset modifies. Derived by the oracle, not declared by the agent.
- **Lease** — a short-lived advisory reservation on symbols, taken at intent time. Leases don't
  block landing (they're not locks); they steer planning so agents avoid doomed work.
- **Landed log** — append-only, content-addressed sequence of accepted changesets. The source
  of truth. Git branches are a *materialization* of a log position, produced by the gateway.
- **Verification fact** — a content-addressed record `(input-closure hash, check id) → verdict`.
  Tests are facts about states, not rituals; a state whose input closure is unchanged inherits
  its facts.

## 2. Changeset

```jsonc
{
  "id": "cs_<blake3-of-canonical-form>",
  "basis": "log@<seq>",             // log position the diff was authored against
  "diff": "<binary patch, content-addressed blob ref>",
  "intent": {
    "summary": "Add retry with backoff to session refresh",
    "prompt_ref": "blob:<hash>",    // optional: originating prompt/task, opaque to the plane
    "acceptance": ["test:auth/test_refresh.py::test_backoff"]
  },
  "write_set": [                    // derived by oracle at intake; agent-supplied hints ignored
    "py:auth/session.py::SessionManager.refresh",
    "file:auth/config.yaml"
  ],
  "agent": { "fleet": "...", "id": "...", "runner": "claude-code/2.x" },
  "signatures": ["..."]
}
```

`[Q]` Should `basis` pin a log position (strict) or a state hash (allows landing against any
equivalent state)? Leaning state hash — it's what makes commutation meaningful.

## 3. Commutation rule (v0)

Changesets `a`, `b` commute iff `write_set(a) ∩ write_set(b) = ∅` **and** neither touches a
symbol the other's acceptance checks depend on (dependency closure via the symbol graph;
file-level fallback where analysis is uncertain — uncertainty always widens the set).

Commuting changesets may land in any order or simultaneously; their verification facts remain
valid across each other's landings. Non-commuting changesets are ordered by the planner, and
the later one is either (a) trivially rebased if textually clean and re-verified, or
(b) sent to **regenerative merge** (§5).

The oracle is an optimization, never the safety gate: every landed state is verified (possibly
via inherited facts + the delta's own checks) before the gateway advances any materialized branch.

## 4. Leases

- `reserve(symbols[], ttl, intent_summary)` → lease id, or contention info (holder, ETA).
- Advisory. Landing never requires a lease; planning quality degrades without them.
- TTL is short (minutes). Renewable. Expiry is silent — no cleanup obligations.
- `[Q]` Do leases nest (module → symbol)? Probably yes via the symbol tree.

## 5. Regenerative merge

When `a` and `b` truly collide, neither is rebased. The plane constructs a regeneration task:

```
inputs:  state at common basis, intent(a) + acceptance(a), intent(b) + acceptance(b),
         the colliding region (dependency-closed)
output:  changeset c with basis = current state, satisfying acceptance(a) ∪ acceptance(b)
```

`c` is produced by a fresh agent (the "reconciler"), enters intake like any changeset, and must
pass the union of acceptance checks plus the standard gate. The original changesets are marked
`superseded_by: c`. Provenance is preserved — `c` records both parents' intents.

`[Q]` Reconciler failure policy: retry with more context, escalate to submitting agents, or
serialize a+b classically? v0: bounded retries, then classic serialization as fallback.

## 6. Landing pipeline

```
intake → oracle (write set, commute graph) → planner (parallel lanes + speculation)
       → verification (facts cache, speculative combined states)
       → landed log append → gateway materializes git branch(es) → deploy trains
```

States: `submitted → analyzed → queued|parallel-lane → verifying → landed | superseded | rejected`.

## 7. MCP surface (v0)

| Tool | Purpose |
|---|---|
| `sync` | Get current landed state (and materialize a working copy without git rebase) |
| `reserve` | Take/renew leases on symbols; returns contention info |
| `submit` | Submit a changeset; returns changeset id + initial analysis |
| `status` | Changeset lifecycle, position, verification facts, supersession |

Humans interact through the git gateway (normal branches and PRs); their PRs enter intake as
ordinary changesets with oracle-derived write sets.

## 8. Non-goals (v0)

- Replacing git for humans. The gateway keeps every existing tool working.
- Cross-repo transactions (Phase 3).
- Read-set precision — v0 over-approximates via dependency closure; refinement comes from the
  conflict corpus.
