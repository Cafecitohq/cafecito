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
  is what moves it.
- **If the cafecito tools are not available in this session, say so and stop** rather than
  committing around the plane. A bypassed commit strands the tip, and the next landing
  builds on history the plane never saw.

Check state any time with `cafecito status`, or `cafecito doctor` for a health check.
<!-- cafecito:end -->
