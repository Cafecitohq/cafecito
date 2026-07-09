"""Shared fleet-state contract between `cafecito swarm` (writer) and
`cafecito watch` (reader). Single JSON file in the engine state dir; the
swarm process is the only writer, so a thread lock suffices.

Snapshot shape:
{
  "goal": str, "started_at": float, "updated_at": float, "done": bool,
  "agents": {
    "<id>": {"state": "planning|working|submitting|landed|escalated|failed",
              "title": str, "detail": str, "updated_at": float}
  }
}
"""

from __future__ import annotations

import json
import pathlib
import threading
import time

STATES = ("planning", "working", "submitting", "landed", "escalated", "failed")


class SwarmState:
    def __init__(self, state_dir: pathlib.Path | str):
        self.path = pathlib.Path(state_dir) / "swarm.json"
        self._lock = threading.Lock()
        self._data = {"goal": "", "started_at": time.time(),
                      "updated_at": time.time(), "done": False, "agents": {}}

    def start(self, goal: str) -> None:
        with self._lock:
            self._data["goal"] = goal
            self._data["started_at"] = time.time()
            self._flush()

    def update(self, agent_id: str, state: str, title: str = "",
               detail: str = "") -> None:
        assert state in STATES, state
        with self._lock:
            a = self._data["agents"].setdefault(agent_id, {})
            a["state"] = state
            if title:
                a["title"] = title
            a["detail"] = detail
            a["updated_at"] = time.time()
            self._flush()

    def finish(self) -> None:
        with self._lock:
            self._data["done"] = True
            self._flush()

    def _flush(self) -> None:
        self._data["updated_at"] = time.time()
        self.path.write_text(json.dumps(self._data, indent=1))


def read_snapshot(state_dir: pathlib.Path | str) -> dict | None:
    """Reader side (watch). None if no swarm has run."""
    p = pathlib.Path(state_dir) / "swarm.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None
