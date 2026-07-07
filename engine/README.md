# engine — the landing pipeline (v0.1)

State in `<repo>/.cafecito/` (landed log, leases, config, lock); any number of
MCP server processes share it via file locking.

Per submit: merge-base → oracle write set → `merge-tree` vs landed tip →
clean candidate **or** live regenerative merge ([regen.py](regen.py)) →
**landing gate always** ([gate.py](gate.py) — changeset tests + impact tests,
clean merges included) → landed log append → materialized branch
(`cafecito/main` by default) advances. Escalations return a reason; agents
rework and resubmit. Agents never rebase.

Config: `.cafecito/config.json` — `branch`, `test_cmd`, `reconciler_model`,
`lease_ttl_s`, `gate_timeout_s`.

Prove it end to end (three scripted agents, one live regeneration — needs the
`claude` CLI, or pass `--no-regen`):

```sh
python3 smoke_test.py [--pytest-python /path/with/pytest]
```

Provenance: extracted from `phase0/` and `bench/mergebench.py` where every
mechanism was validated (12/14 semantic PASS regenerations; MergeBench landed
a real 33-changeset burst with green main). The experiments stay frozen there.
