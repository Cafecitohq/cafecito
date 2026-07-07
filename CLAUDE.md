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
- After committing directly to `main`, run `python3 -m cafecito.cli advance --repo .` and
  push both `main` and `cafecito/main`.

## Commands
- Tests: `phase0/workdir/venv-test/bin/python -m pytest -q cafecito/tests`
- Engine smoke (live regen, needs `claude` CLI):
  `python3 -m cafecito.tests.smoke --pytest-python phase0/workdir/venv-test/bin/python`
- Demo dry run: `DEMO_DELAY=0 PYTEST_PY=$PWD/phase0/workdir/venv-test/bin/python ./examples/demo.sh`

## Maintainer sessions
If a `CLAUDE.local.md` is present, follow its documentation-maintenance instructions at the
end of every session that changes strategy, architecture, decisions, or ships a milestone.
