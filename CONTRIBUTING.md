# Contributing to cafecito

Thanks for your interest. The project is in **Phase 0** (validating the core physics — see
[PLAN.md](PLAN.md)), so the most valuable contributions right now are:

- Running the Phase 0 experiments ([phase0/README.md](phase0/README.md)) against repos we
  haven't measured and sharing the numbers.
- Arguing with [SPEC.md](SPEC.md). The changeset format and lease semantics are drafts; issues
  that poke holes in them are gifts.
- **Symbol scanners for more languages.** Python, TypeScript/JavaScript, and Go ship today
  (plus JSON key paths); everything else lands at file granularity. Rust, Ruby, Java, and C#
  are all open. The bar is unusual, so read it before starting: **stdlib only, no new runtime
  dependencies** — zero dependencies is a deliberate product property, not an accident, so
  please don't send a tree-sitter extractor. Scanners must also be *conservative*: returning
  "I don't know" has to be safe, because the landing gate, not the scanner, is what keeps
  main green. See [`cafecito/spans.py`](cafecito/spans.py) for the shape.

## Developer Certificate of Origin

All commits must be signed off (`git commit -s`), certifying the
[Developer Certificate of Origin](https://developercertificate.org/).

For non-trivial contributions we additionally require a Contributor License Agreement; the CLA
bot will prompt you on your first pull request. This keeps the project's IP unambiguous — a
deliberate, documented decision (see PLAN.md §5).

## Ground rules

- The engine and oracle stay dependency-light. Phase 0 code is Python stdlib only.
- Every claim about integration behavior needs a reproducible experiment behind it.
- Vocabulary discipline (SPEC §1.1): changesets *land*; collisions *commute*, *regenerate*,
  or *escalate*; "merge" is reserved for git's textual mechanism and the market category.
- Benchmarks are published even when the numbers are unflattering.
