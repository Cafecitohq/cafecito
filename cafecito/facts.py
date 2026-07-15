"""Verification facts — content-addressed test verdicts (SPEC §1).

A fact records that a check passed against a specific input closure:
key = sha256(check command, test path, sorted (path, blob sha) of the
closure). Blob shas come from git's own content addressing, so two states
with identical closure content share facts regardless of history.

Only GREEN verdicts are recorded: a red must re-run every time (flakes and
fixes both deserve fresh evidence). The store lives in the engine state dir
and is trimmed FIFO. Since v0.5 gates run concurrently: writes go through an
atomic replace, and a lost update between racers is acceptable — facts are an
optimization, never correctness. The temp file is per-writer (mkstemp) —
a shared temp name lets one racer's replace steal another's file, which
crashes the losing gate: lost updates are acceptable, lost gates are not.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import tempfile
import time

MAX_FACTS = 5000


def fact_key(test_cmd: list[str], test_path: str,
             closure_blobs: list[tuple[str, str]]) -> str:
    payload = json.dumps([test_cmd, test_path, sorted(closure_blobs)])
    return hashlib.sha256(payload.encode()).hexdigest()


class FactsStore:
    def __init__(self, state_dir: pathlib.Path):
        self.path = pathlib.Path(state_dir) / "facts.json"
        try:
            self._facts: dict = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self._facts = {}

    def green(self, key: str) -> bool:
        return self._facts.get(key, {}).get("verdict") == "green"

    def record_green(self, key: str, test_path: str) -> None:
        self._facts[key] = {"verdict": "green", "test": test_path,
                            "at": time.time()}
        if len(self._facts) > MAX_FACTS:
            oldest = sorted(self._facts, key=lambda k: self._facts[k]["at"])
            for k in oldest[: len(self._facts) - MAX_FACTS]:
                del self._facts[k]
        fd, tmp = tempfile.mkstemp(dir=self.path.parent,
                                   prefix="facts.", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(self._facts))
        os.replace(tmp, self.path)
