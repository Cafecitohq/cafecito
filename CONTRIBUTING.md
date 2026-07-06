# Contributing to cafecito

Thanks for your interest. The project is in **Phase 0** (validating the core physics — see
[PLAN.md](PLAN.md)), so the most valuable contributions right now are:

- Running the Phase 0 experiments ([phase0/README.md](phase0/README.md)) against repos we
  haven't measured and sharing the numbers.
- Arguing with [SPEC.md](SPEC.md). The changeset format and lease semantics are drafts; issues
  that poke holes in them are gifts.
- tree-sitter symbol extractors for languages beyond Python.

## Developer Certificate of Origin

All commits must be signed off (`git commit -s`), certifying the
[Developer Certificate of Origin](https://developercertificate.org/).

For non-trivial contributions we additionally require a Contributor License Agreement; the CLA
bot will prompt you on your first pull request. This keeps the project's IP unambiguous — a
deliberate, documented decision (see PLAN.md §5).

## Ground rules

- The engine and oracle stay dependency-light. Phase 0 code is Python stdlib only.
- Every claim about merge behavior needs a reproducible experiment behind it.
- Benchmarks are published even when the numbers are unflattering.
