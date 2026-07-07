#!/usr/bin/env bash
# cafecito demo — a three-agent fleet lands in parallel:
# commute, commute, regenerate. Nobody rebases. Main stays green.
#
# Record with:
#   asciinema rec demo.cast -c "DEMO_DELAY=1.4 ./examples/demo.sh"
# Terminal ~100x30. Runtime ≈ 60-90s; the pause in the middle is a real
# reconciler call (needs the `claude` CLI on PATH).
#
# Env:
#   DEMO_DELAY   seconds between beats (default 1.2; 0 for CI/testing)
#   PYTEST_PY    python with pytest importable (default: python3)
set -euo pipefail

DELAY="${DEMO_DELAY:-1.2}"
PYTEST_PY="${PYTEST_PY:-python3}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if command -v cafecito >/dev/null 2>&1; then
  CAFECITO=(cafecito)
else
  CAFECITO=(env "PYTHONPATH=$ROOT" python3 -m cafecito.cli)
fi

if [ -t 1 ] && [ -n "${TERM:-}" ]; then
  BOLD=$(tput bold) DIM=$(tput dim) RESET=$(tput sgr0)
else
  BOLD="" DIM="" RESET=""
fi
say() { printf "\n%s# %s%s\n" "$DIM" "$*" "$RESET"; sleep "$DELAY"; }
run() { printf "%s\$ %s%s\n" "$BOLD" "$*" "$RESET"; "$@"; sleep "$DELAY"; }

WORK=$(mktemp -d "${TMPDIR:-/tmp}/cafecito-demo-XXXXXX")
REPO="$WORK/coffeeshop"
trap 'rm -rf "$WORK"' EXIT

# ---- silent setup: a tiny repo with tests -----------------------------------
mkdir -p "$REPO/tests"
cat > "$REPO/orders.py" <<'PY'
def brew(order):
    return f"brewing {order}"
PY
cat > "$REPO/menu.py" <<'PY'
PRICES = {"espresso": 3.0, "latte": 4.5}


def price(item):
    return PRICES[item]
PY
cat > "$REPO/tests/test_orders.py" <<'PY'
from orders import brew


def test_brew():
    assert brew("espresso") == "brewing espresso"
PY
cat > "$REPO/tests/test_menu.py" <<'PY'
from menu import price


def test_price():
    assert price("latte") == 4.5
PY
git -C "$REPO" init -q -b main
git -C "$REPO" add -A
git -C "$REPO" -c user.name=demo -c user.email=demo@cafecito.local commit -q -m "coffeeshop: initial menu"

agent_commit() { # <worktree> <agent> <message>
  git -C "$1" add -A
  git -C "$1" -c "user.name=$2" -c "user.email=$2@cafecito.local" commit -q -m "$3"
  git -C "$1" rev-parse HEAD
}

{ [ -t 1 ] && [ -n "${TERM:-}" ] && clear; } || true
say "coffeeshop: a tiny repo. four tests, all green. one landed branch, managed by cafecito."
run "${CAFECITO[@]}" init --repo "$REPO" --test-cmd "$PYTEST_PY -m pytest -q -p no:cacheprovider"

say "three agents start from the SAME commit. no coordination. nobody will ever rebase."

WT_A=$("${CAFECITO[@]}" sync --repo "$REPO" --worktree --agent espresso | python3 -c "import json,sys;print(json.load(sys.stdin)['worktree'])")
cat > "$WT_A/orders.py" <<'PY'
def brew(order):
    if not order:
        raise ValueError("empty order")
    return f"brewing {order}"
PY
cat >> "$WT_A/tests/test_orders.py" <<'PY'


def test_brew_rejects_empty():
    import pytest
    with pytest.raises(ValueError):
        brew("")
PY
SHA_A=$(agent_commit "$WT_A" agent-espresso "brew(): reject empty orders")

WT_B=$("${CAFECITO[@]}" sync --repo "$REPO" --worktree --agent croissant | python3 -c "import json,sys;print(json.load(sys.stdin)['worktree'])")
cat > "$WT_B/menu.py" <<'PY'
PRICES = {"espresso": 3.0, "latte": 4.5}
OAT_MILK_SURCHARGE = 0.5


def price(item, oat_milk=False):
    base = PRICES[item]
    return base + OAT_MILK_SURCHARGE if oat_milk else base
PY
cat >> "$WT_B/tests/test_menu.py" <<'PY'


def test_price_oat_milk():
    assert price("latte", oat_milk=True) == 5.0
PY
SHA_B=$(agent_commit "$WT_B" agent-croissant "price(): oat milk surcharge")

WT_C=$("${CAFECITO[@]}" sync --repo "$REPO" --worktree --agent oatmilk | python3 -c "import json,sys;print(json.load(sys.stdin)['worktree'])")
cat > "$WT_C/orders.py" <<'PY'
def brew(order, size="tall"):
    return f"brewing {size} {order}"
PY
cat > "$WT_C/tests/test_orders.py" <<'PY'
from orders import brew


def test_brew():
    assert brew("espresso") == "brewing tall espresso"


def test_brew_size():
    assert brew("latte", size="grande") == "brewing grande latte"
PY
SHA_C=$(agent_commit "$WT_C" agent-oatmilk "brew(): cup sizes")

say "agent-espresso hardened brew(). agent-croissant priced oat milk. agent-oatmilk added cup sizes."
say "espresso submits — gate runs the tests, then it lands:"
run "${CAFECITO[@]}" submit "$SHA_A" --repo "$REPO" --agent agent-espresso --title "brew(): reject empty orders"

say "croissant touched different symbols — it COMMUTES. no rebase, no waiting, no re-testing espresso:"
run "${CAFECITO[@]}" submit "$SHA_B" --repo "$REPO" --agent agent-croissant --title "price(): oat milk surcharge"

say "oatmilk rewrote the SAME function espresso just changed. a merge queue would bounce it back."
say "here, nobody resolves anything: a reconciler regenerates brew() from BOTH intents, then the gate decides…"
run "${CAFECITO[@]}" submit "$SHA_C" --repo "$REPO" --agent agent-oatmilk --title "brew(): cup sizes"

say "the fleet is in:"
run "${CAFECITO[@]}" status --repo "$REPO"

say "the landed branch is a normal git branch — trailers and all:"
run git -C "$REPO" log cafecito/main --format="%h %s%n%(trailers:only=true)" -n 3

say "and main is green — checked, not assumed:"
git -C "$REPO" worktree add -q --detach "$WORK/tip" cafecito/main
(cd "$WORK/tip" && run $PYTEST_PY -m pytest -q -p no:cacheprovider tests/)

say "three agents. two commutes. one regeneration. zero rebases. zero conflict markers."
say "cafecito — land in parallel, regenerate collisions, never resolve a conflict.  ☕"
