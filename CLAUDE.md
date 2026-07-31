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

Changes reach `cafecito/main` through the **cafecito** control plane — by *landing*, not by
pushing. Read-only work needs none of this.

Before you edit, `sync` for your base and `reserve` the files or symbols you'll touch, so
contention surfaces before the work is done. Then commit and `submit` the sha: separate
changesets land in parallel, overlaps regenerate from both sides' intent, contradictions
come back to a human, and every landing runs the gate. `cafecito status` shows the state.

If neither the cafecito tools nor the CLI are here, don't commit around the plane anyway —
leave the work uncommitted or on a side branch and say so. A repo-specific exception (a
release ritual, say) counts only if it is written down inside this block.

In *this* repo the one written-down exception is the release ritual under
"Versions & pushing": maintainer commits land on `main` directly and the
post-commit hook advances the tip. Feature work still goes through `submit`.
<!-- cafecito:end -->
