# Dogfood log — cafecito builds cafecito

Session 1 · 2026-07-07 · [dogfood.py](dogfood.py) drives a real agent fleet through the MCP
server **on this repository**. Every landing below is in `git log` — synthetic commits by the
engine, deployed to `main` via `git merge --ff-only cafecito/main`.

## What landed (through cafecito, by agents)

| changeset | outcome | gate |
|---|---|---|
| engine: sanitize agent id in sync worktree prefix | landed (hotfix, see finding 1) | no signal — see finding 2 |
| oracle: unit tests for symbol extraction | landed | green, 0.23s |
| engine: unit tests for diff3 segmentation | landed | green, 0.44s |
| engine: unit tests for impact-test mapping | landed | green, 0.57s |
| engine: landed commits carry Changeset-Id + Signed-off-by trailers | landed | green, 0.23s |

Result: cafecito went from zero unit tests to 36 (0.48s suite), written by four headless
agents and landed through the pipeline those tests now protect. 4/4 fleet tasks landed, no
escalations, no collisions (tasks were write-set-disjoint by design; leases all granted).

## Findings

1. **First bug surfaced within minutes.** Agent ids containing `/` (e.g.
   `dogfood/tests-writeset`) crashed `sync(create_worktree)` — the id leaked into a
   `mkdtemp` prefix. Fixed, and *landed through cafecito while cafecito was broken* — the
   submit path didn't depend on the broken code. The engine's first-ever real landing was its
   own bugfix.
2. **The bootstrap gap is real.** A repo without tests gives the landing gate no signal; the
   hotfix above landed with `no_signal: true`, honestly flagged in the log. Consequence: the
   very first thing a fleet should land in any repo is test coverage — which is exactly what
   this session's backlog did. A `require_signal` config option (refuse no-signal landings)
   is the follow-up.
3. **MCP clients must respect `isError`.** The driver initially JSON-parsed error text and
   crashed with a useless message. Tool errors are results, not protocol errors — handle them.
4. **The engine tip must follow out-of-band commits.** Docs committed directly to `main`
   leave `cafecito/main` (and `state.json`) behind. v0.1 fix: operator advances the tip
   manually. Real fix: an `advance`/ingest operation so external commits flow into the landed
   log — this is the seed of the git gateway's PR-ingestion path.
5. **Trailers now land with every changeset** (`Changeset-Id`, `Signed-off-by`) — an agent
   extracted the `_land_message` helper and tested it. The trailer commit is the repo's last
   landed commit without trailers.
