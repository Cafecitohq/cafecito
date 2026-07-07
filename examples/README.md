# examples

## demo.sh — the 60-second fleet demo

Three agents branch from the same commit: two commute and land in parallel, the third
collides and lands via live regeneration. Main ends green. This is the recording that goes
next to the launch post.

```sh
./examples/demo.sh                       # watch it
DEMO_DELAY=0 ./examples/demo.sh          # fast run (testing)
```

Recording checklist:
- `asciinema rec demo.cast -c "DEMO_DELAY=1.4 ./examples/demo.sh"` (or any screen recorder)
- terminal ≈ 100×30, dark theme, font ≥ 14pt for legibility at blog width
- needs the `claude` CLI on PATH (one live reconciler call — the dramatic pause is real)
- `PYTEST_PY` must point at a python with pytest importable (default `python3`)
- do a `DEMO_DELAY=0` dry run first; the reconciler call is the only nondeterministic beat
