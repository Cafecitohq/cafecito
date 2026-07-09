"""cafecito watch — a live terminal dashboard for the fleet and landed log.

Pure stdlib. `snapshot(repo)` gathers everything renderable from the engine
state dir; `render(snap, ...)` is a PURE function turning that into a frame of
text (no cursor tricks, no I/O); `run_watch(args)` does the loop and the ANSI
screen clears. The reader never writes state — the swarm process owns it.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import sys
import time

from .fleetstate import read_snapshot

# ---------------------------------------------------------------- ansi ---

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
GREEN = "\x1b[32m"
RED = "\x1b[31m"
YELLOW = "\x1b[33m"
CYAN = "\x1b[36m"
BLUE = "\x1b[34m"
MAGENTA = "\x1b[35m"

# A swarm snapshot older than this (and not done) is treated as stale — the
# writer likely died, so we don't paint a misleading "working" fleet.
STALE_S = 30.0

# state -> (glyph, color)
_AGENT_STYLE = {
    "planning": ("*", CYAN),
    "working": ("●", CYAN),      # ●
    "submitting": ("↑", BLUE),   # ↑
    "landed": ("✓", GREEN),      # ✓
    "escalated": ("✗", RED),     # ✗
    "failed": ("!", YELLOW),
}

_VERDICT_COLOR = {
    "landed": GREEN,
    "escalated": RED,
    "advanced": BLUE,
    "rejected": YELLOW,
    "noop": DIM,
}


# ------------------------------------------------------------ snapshot ---

def _read_json(path: pathlib.Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def snapshot(repo: str) -> dict:
    """Gather everything renderable from <repo>/.cafecito/. Every file may be
    missing; the caller sees empty sections rather than an exception. When the
    state dir itself is absent, `present` is False and render shows a hint."""
    state_dir = pathlib.Path(repo) / ".cafecito"
    snap: dict = {
        "present": state_dir.is_dir(),
        "repo": str(repo),
        "branch": "cafecito/main",
        "tip": "",
        "landed": 0,
        "escalated": 0,
        "log": [],
        "leases": [],
        "swarm": None,
        "now": time.time(),
    }
    if not snap["present"]:
        return snap

    cfg = _read_json(state_dir / "config.json")
    if isinstance(cfg, dict) and cfg.get("branch"):
        snap["branch"] = cfg["branch"]

    state = _read_json(state_dir / "state.json")
    if isinstance(state, dict) and state.get("tip"):
        snap["tip"] = state["tip"]

    log = _read_log(state_dir / "log.jsonl")
    snap["landed"] = sum(1 for e in log if e.get("verdict") == "landed")
    snap["escalated"] = sum(1 for e in log if e.get("verdict") == "escalated")
    snap["log"] = log[-12:]

    snap["leases"] = _active_leases(state_dir / "leases.json", snap["now"])

    swarm = read_snapshot(state_dir)
    if isinstance(swarm, dict):
        snap["swarm"] = swarm

    return snap


def _read_log(path: pathlib.Path) -> list[dict]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def _active_leases(path: pathlib.Path, now: float) -> list[dict]:
    data = _read_json(path)
    if not isinstance(data, dict):
        return []
    out = []
    for key, v in data.items():
        if not isinstance(v, dict):
            continue
        expires = v.get("expires", 0)
        if expires <= now:
            continue
        out.append({"key": key, "agent": v.get("agent", "?"),
                    "intent": v.get("intent", ""),
                    "left_s": round(expires - now)})
    out.sort(key=lambda d: d["left_s"])
    return out


# -------------------------------------------------------------- render ---

class _Paint:
    """Colorize-or-not, chosen once per render so every helper stays pure."""

    def __init__(self, color: bool):
        self.color = color

    def __call__(self, text: str, *codes: str) -> str:
        if not self.color or not codes:
            return text
        return "".join(codes) + text + RESET


def _clip(s: str, width: int) -> str:
    """Truncate to width, accounting for nothing but visible chars — render
    only clips strings that carry no escape codes (we colorize after clip)."""
    if len(s) <= width:
        return s
    if width <= 1:
        return s[:width]
    return s[:width - 1] + "…"  # …


def _trunc(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ").strip()
    if len(s) <= n:
        return s
    if n <= 1:
        return s[:n]
    return s[:n - 1] + "…"


def _gate_suffix(entry: dict) -> str:
    """Compact trailing facts for a landed log line: `gate 0.9s regen 12s ...`."""
    bits = []
    gate = entry.get("gate")
    if isinstance(gate, dict):
        sec = gate.get("seconds")
        if sec is not None:
            bits.append(f"gate {sec}s")
        if gate.get("no_signal"):
            bits.append("no-signal")
        memo = gate.get("memo")
        if isinstance(memo, dict):
            bits.append(f"memo {memo.get('hits', 0)}/{memo.get('runs', 0)}")
    if entry.get("regen_s") is not None:
        bits.append(f"regen {entry['regen_s']}s")
    if entry.get("gen_s") is not None:
        bits.append(f"gen {entry['gen_s']}s")
    elif entry.get("generated"):
        bits.append("gen")
    return "  ".join(bits)


def render(snap: dict, width: int = 100, color: bool = True) -> str:
    """PURE: build the full frame as one string. No I/O, no time reads (uses
    snap['now']). Every emitted line is clipped to <= width. With color=False,
    not a single escape code appears in the output."""
    width = max(20, int(width))
    paint = _Paint(color)
    lines: list[str] = []

    if not snap.get("present"):
        lines.append(paint(_clip("☕ cafecito", width), BOLD, MAGENTA))
        lines.append(_clip(f"no control plane at {snap.get('repo', '.')}", width))
        lines.append(_clip("run `cafecito init` to set one up.", width))
        return "\n".join(lines)

    # --- header -----------------------------------------------------------
    tip = snap.get("tip") or ""
    header = (f"☕ cafecito · {snap.get('branch', '')} · "
              f"{tip[:10] or '(no tip)'} · "
              f"landed {snap.get('landed', 0)} · "
              f"escalated {snap.get('escalated', 0)}")
    lines.append(paint(_clip(header, width), BOLD, MAGENTA))

    # --- fleet ------------------------------------------------------------
    swarm = snap.get("swarm")
    if isinstance(swarm, dict) and swarm.get("agents"):
        now = snap.get("now", time.time())
        age = now - swarm.get("updated_at", 0)
        stale = (not swarm.get("done")) and age > STALE_S
        if not stale:
            goal = _trunc(swarm.get("goal", ""), width - 10)
            tag = "done" if swarm.get("done") else "live"
            head = f"FLEET · {tag}"
            if goal:
                head += f" · {goal}"
            lines.append(paint(_clip(head, width), BOLD))
            for aid, a in sorted(swarm["agents"].items()):
                lines.append(_agent_line(aid, a, width, paint))

    # --- leases -----------------------------------------------------------
    leases = snap.get("leases") or []
    if leases:
        lines.append(paint(_clip("LEASES", width), BOLD))
        for lz in leases:
            left = f"{lz['left_s']}s"
            body = f"  {lz['key']}  {lz['agent']}  {left} left"
            intent = lz.get("intent")
            if intent:
                body += f"  — {intent}"
            lines.append(_clip(body, width))

    # --- landed log -------------------------------------------------------
    log = snap.get("log") or []
    if log:
        lines.append(paint(_clip("LANDED LOG", width), BOLD))
        for entry in reversed(log):
            lines.append(_log_line(entry, width, paint))

    return "\n".join(lines)


def _agent_line(aid: str, a: dict, width: int, paint: _Paint) -> str:
    state = a.get("state", "")
    glyph, col = _AGENT_STYLE.get(state, ("·", DIM))
    title = _trunc(a.get("title", ""), 40)
    detail = _trunc(a.get("detail", ""), max(0, width - 60))
    # Build with a plain-text budget, then colorize the glyph in place.
    plain = f"  {glyph} {aid[:14]:<14} {state:<10} {title}"
    if detail:
        plain += f"  {detail}"
    plain = _clip(plain, width)
    if paint.color:
        # Recolor only the leading glyph occurrence (after the two spaces).
        return plain.replace(glyph, paint(glyph, col), 1)
    return plain


def _log_line(entry: dict, width: int, paint: _Paint) -> str:
    verdict = entry.get("verdict", "?")
    col = _VERDICT_COLOR.get(verdict, DIM)
    suffix = _gate_suffix(entry)
    reason = entry.get("reason")
    tag = f"[{verdict}]"
    # Reserve room for the tag, a suffix, and any escalation reason.
    reason_part = f"  ✗ {reason}" if reason else ""
    fixed = len(tag) + 2 + (2 + len(suffix) if suffix else 0) + len(reason_part)
    title = _trunc(entry.get("title", ""), max(4, width - fixed - 2))
    plain = f"{tag} {title}"
    if suffix:
        plain += f"  {suffix}"
    if reason_part:
        plain += reason_part
    plain = _clip(plain, width)
    if paint.color:
        return plain.replace(tag, paint(tag, col), 1)
    return plain


# ------------------------------------------------------------- run loop ---

def run_watch(args) -> int:
    if getattr(args, "once", False):
        color = sys.stdout.isatty()
        width = shutil.get_terminal_size((100, 24)).columns
        print(render(snapshot(args.repo), width=width, color=color))
        return 0

    interval = getattr(args, "interval", 1.0)
    try:
        while True:
            width = shutil.get_terminal_size((100, 24)).columns
            frame = render(snapshot(args.repo), width=width,
                           color=sys.stdout.isatty())
            sys.stdout.write("\x1b[H\x1b[2J" + frame + "\n")
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
