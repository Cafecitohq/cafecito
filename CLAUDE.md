# cafecito — conventions for Claude Code sessions

## Vocabulary (SPEC.md §1.1 — enforced in code, docs, and commit messages)
Changesets **land**; collisions **commute**, **regenerate**, or **escalate**. "Merge" is
reserved for git's textual mechanism (`merge-tree`, 3-way) and the market category
("merge queue"). No cafecito code path resolves a merge conflict.

## Hard rules
- `cafecito/` package is **stdlib-only** — zero runtime dependencies is a feature. pytest is
  the *user's* gate command, never our import.
- The landing gate gates **every** landing, clean textual merges included. Never weaken this;
  two red mains during development are why (see bench/README.md).
- `phase0/` is the frozen evidence base — add new experiments, never refactor old ones.
- Commits: DCO sign-off (`-s`) required; append-only decision history in docs.

## Versions & pushing (the four-heads invariant)
- A session that touched `main` ends with all four heads identical — `git rev-parse main
  cafecito/main origin/main origin/cafecito/main` prints one sha four times — and the
  suite green, or states out loud why not. Local commits mid-session are fine; ending
  half-synced is the failure mode.
- After committing directly to `main`: `python3 -m cafecito.cli advance --repo .`, then
  push both refs in one command — `git push origin main cafecito/main`. CI's plane-sync
  job goes red when a pushed `main` leaves `cafecito/main` trailing.
- **Tags are releases, not bookmarks.** Pushing `v*` publishes to PyPI via trusted
  publishing, and PyPI never reuses a version — so never create a local-only `v*` tag.
  Release ritual: suite green → bump `pyproject.toml` AND `cafecito/__init__.py`
  (test_version.py pins them together) → commit, advance, four-heads check → push
  branches → tag `vX.Y.Z` → push the tag in the same breath → watch publish.yml, verify
  on pypi.org. Versioning is `0.MINOR.PATCH`: minor for features, patch for fixes.

## Commands
- Tests: `phase0/workdir/venv-test/bin/python -m pytest -q cafecito/tests`
- Engine smoke (live regen, needs `claude` CLI):
  `python3 -m cafecito.tests.smoke --pytest-python phase0/workdir/venv-test/bin/python`
- Demo dry run: `DEMO_DELAY=0 PYTEST_PY=$PWD/phase0/workdir/venv-test/bin/python ./examples/demo.sh`

## Maintainer sessions
If a `CLAUDE.local.md` is present, follow its documentation-maintenance instructions at the
end of every session that changes strategy, architecture, decisions, or ships a milestone.

<!-- cafecito:begin: edit freely; delete this block to regenerate -->
## Landing changes (cafecito)

This repo coordinates parallel agent work through **cafecito**. Changes reach the deploy
branch by *landing* through the control plane — not by pushing.

- **Start from the plane's tip.** Call the `sync` tool (or `cafecito sync`) and work from
  the commit it returns, not from wherever the checkout happens to sit.
- **Reserve before editing.** Call `reserve` with the symbols or files you are about to
  change, so contention surfaces before the work is done instead of after it.
- **Land with `submit`.** Commit your work, then submit the sha. Changesets that touch
  different code land in parallel; real overlaps are regenerated automatically from both
  sides' intent; contradictions come back to a human. Every landing runs the test gate.
- **Don't push the deploy branch yourself.** The landed branch is `cafecito/main`, and `submit`
  is what moves it. (In *this* repo the release ritual under "Versions & pushing" above is the
  one documented exception — maintainer commits land on `main` directly and the post-commit
  hook advances the tip. Feature work still goes through `submit`.)
- **If the cafecito tools are not available in this session, say so and stop** rather than
  committing around the plane. A bypassed commit strands the tip, and the next landing
  builds on history the plane never saw.

Check state any time with `cafecito status`, or `cafecito doctor` for a health check.
<!-- cafecito:end -->
