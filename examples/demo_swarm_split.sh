#!/usr/bin/env bash
# cafecito split-screen demo — a REAL fleet on the left, `cafecito watch`
# live on the right, in one tmux frame. Nothing simulated.
#
# Record with:
#   asciinema rec swarm-split.cast --window-size 150x38 \
#     -c "./examples/demo_swarm_split.sh"
#
# Needs: tmux; `claude` CLI on PATH (real planner + workers); `cafecito`
# on PATH; PYTEST_PY pointing at a python with pytest (default python3).
# Runtime ≈ 2–4 min (real agents). AGENTS/MODEL env override the fleet.
set -euo pipefail
export LC_ALL="${LC_ALL:-en_US.UTF-8}"   # tmux ASCII-mangles ☕ and — without a UTF-8 locale

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS="${AGENTS:-3}"
MODEL="${MODEL:-sonnet}"
PYTEST_PY="${PYTEST_PY:-python3}"
SESSION="cafecito-split-$$"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/cafecito-split-XXXXXX")
REPO="$WORK/coffeeshop"
trap 'tmux kill-session -t "$SESSION" 2>/dev/null || true; rm -rf "$WORK"' EXIT

PYTEST_PY="$PYTEST_PY" python3 "$ROOT/examples/demo_swarm.py" --setup "$REPO" >/dev/null
GOAL="$(python3 "$ROOT/examples/demo_swarm.py" --goal)"

cat > "$WORK/left.sh" <<LEFT
#!/usr/bin/env bash
printf '☕ cafecito swarm — one sentence in, a REAL fleet out\n\n'
printf '\$ cafecito swarm "loyalty points, cup sizes, receipts" --agents $AGENTS\n\n'
cafecito swarm "$GOAL" --repo "$REPO" --agents $AGENTS --model $MODEL || true
printf '\nthe landed branch — normal git, trailers and all:\n'
git -C "$REPO" log cafecito/main --format='%h %s' -4
git -C "$REPO" worktree add --detach --quiet "$WORK/tip" cafecito/main
(cd "$WORK/tip" && $PYTEST_PY -m pytest -q -p no:cacheprovider tests/ 2>/dev/null | tail -1)
printf '\nzero rebases. zero conflict markers. ☕\n'
sleep 8
tmux kill-session -t "$SESSION"
LEFT
chmod +x "$WORK/left.sh"

tmux new-session -d -s "$SESSION" -x "$(tput cols)" -y "$(tput lines)" "$WORK/left.sh"
tmux set -t "$SESSION" status off
tmux set -t "$SESSION" pane-border-style "fg=colour94"
tmux split-window -h -l '55%' -t "$SESSION" \
  "cafecito watch --repo '$REPO' --interval 0.8"
tmux select-pane -t "$SESSION:0.0"
exec tmux attach -t "$SESSION"
