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

## demo_swarm_split.sh — the split-screen fleet demo

`cafecito swarm` (left pane) and `cafecito watch` (right pane) in one tmux frame — a real
planner, real workers, real gates, and the dashboard streaming it all live.

```sh
asciinema rec swarm-split.cast --window-size 150x38 -c "./examples/demo_swarm_split.sh"
```

- needs `tmux`, the `claude` CLI, `cafecito` on PATH, and PYTEST_PY with pytest importable
- a UTF-8 locale is exported by the script (tmux ASCII-mangles ☕ without one)
- trim the post-`kill-session` tail (the `[exited]` events) before rendering a GIF, then:
  `agg --font-size 14 --idle-time-limit 4 swarm-split.cast swarm-split.gif`
