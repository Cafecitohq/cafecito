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
<!-- cafecito:end -->
