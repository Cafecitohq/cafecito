#!/usr/bin/env python3
"""The swarm demo, cinematic edition — one recordable feed.

Builds a fresh coffeeshop repo, then runs `cafecito swarm` (a REAL fleet:
planner + parallel workers + gated landings) while rendering live
`cafecito watch` frames to the terminal, so a single recording shows the
whole story: tasks planned, agents working, gates running concurrently
(GATING NOW), landings streaming in, main ending green.

Record with:
  asciinema rec swarm-demo.cast -c "python3 examples/demo_swarm.py"

Env:
  PYTEST_PY   python with pytest importable (default: python3)
  AGENTS      fleet size (default 3)
  MODEL       worker/planner model (default sonnet)

Runtime ≈ 2–4 minutes — real reconciler-grade agents, nothing simulated.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cafecito.watch import render, snapshot  # noqa: E402

GOAL = ("Add three independent features to this coffee shop app: "
        "(1) in menu.py, a loyalty_points(item) function awarding 1 point per "
        "whole dollar of the item's price, tested in tests/test_menu.py; "
        "(2) in orders.py, cup-size support — brew(order, size='tall') "
        "returning 'brewing {size} {order}', tested in tests/test_orders.py; "
        "(3) a new receipts.py with format_receipt(item) returning "
        "'{item}: ${price:.2f}' using menu.price, tested in a new "
        "tests/test_receipts.py")

FILES = {
    "menu.py": 'PRICES = {"espresso": 3.0, "latte": 4.5, "cortado": 4.0}\n\n\n'
               "def price(item):\n    return PRICES[item]\n",
    "orders.py": "def brew(order):\n    return f\"brewing {order}\"\n",
    "tests/test_menu.py": "from menu import price\n\n\n"
                          "def test_price():\n    assert price('latte') == 4.5\n",
    "tests/test_orders.py": "from orders import brew\n\n\n"
                            "def test_brew():\n"
                            "    assert brew('espresso') == 'brewing espresso'\n",
}


def sh(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def setup_repo(repo: pathlib.Path, pytest_py: str) -> None:
    """Build the coffeeshop fixture and init the plane (shared with the
    split-screen driver, examples/demo_swarm_split.sh)."""
    for path, content in FILES.items():
        f = repo / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    sh(repo, "git", "init", "-q", "-b", "main")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "-c", "user.name=demo", "-c", "user.email=demo@cafeci.to",
       "commit", "-q", "-m", "coffeeshop: initial")
    subprocess.run(
        [sys.executable, "-m", "cafecito.cli", "init", "--repo", str(repo),
         "--test-cmd", f"{pytest_py} -m pytest -q -p no:cacheprovider"],
        cwd=ROOT, check=True, capture_output=True)


def main() -> int:
    pytest_py = os.environ.get("PYTEST_PY", "python3")
    agents = os.environ.get("AGENTS", "3")
    model = os.environ.get("MODEL", "sonnet")
    work = pathlib.Path(tempfile.mkdtemp(prefix="cafecito-swarm-demo-"))
    repo = work / "coffeeshop"

    try:
        setup_repo(repo, pytest_py)

        print("\x1b[2J\x1b[H☕ cafecito swarm — one sentence in, a fleet out\n")
        print(f"goal: {GOAL[:90]}…\n")
        time.sleep(2)

        proc = subprocess.Popen(
            [sys.executable, "-m", "cafecito.cli", "swarm", GOAL,
             "--repo", str(repo), "--agents", agents, "--model", model],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        width = shutil.get_terminal_size((100, 30)).columns
        while proc.poll() is None:
            frame = render(snapshot(str(repo)), width=width,
                           color=sys.stdout.isatty())
            sys.stdout.write("\x1b[H\x1b[2J" + frame + "\n")
            sys.stdout.flush()
            time.sleep(1.2)

        # final frame + the receipts
        frame = render(snapshot(str(repo)), width=width,
                       color=sys.stdout.isatty())
        sys.stdout.write("\x1b[H\x1b[2J" + frame + "\n\n")
        out = (proc.stdout.read() or "").strip().splitlines()
        for line in out[-8:]:
            print(line)
        print("\nthe landed branch (normal git, trailers and all):")
        log = subprocess.run(
            ["git", "-C", str(repo), "log", "cafecito/main", "--format=%h %s", "-4"],
            capture_output=True, text=True).stdout
        print(log)
        wt = work / "tipcheck"
        sh(repo, "git", "worktree", "add", "--detach", "--quiet", str(wt),
           "cafecito/main")
        r = subprocess.run([pytest_py, "-m", "pytest", "-q",
                            "-p", "no:cacheprovider", "tests/"],
                           cwd=wt, capture_output=True, text=True)
        print("tests at tip:", r.stdout.strip().splitlines()[-1])
        print("\nzero rebases. zero conflict markers. that's cafecito. ☕")
        return 0 if proc.returncode == 0 and r.returncode == 0 else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--setup":
        target = pathlib.Path(sys.argv[2])
        setup_repo(target, os.environ.get("PYTEST_PY", "python3"))
        print(target)
        sys.exit(0)
    if len(sys.argv) == 2 and sys.argv[1] == "--goal":
        print(GOAL)
        sys.exit(0)
    sys.exit(main())
