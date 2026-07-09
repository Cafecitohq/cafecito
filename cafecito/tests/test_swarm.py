"""Deterministic unit tests for the swarm planner seam and fleet state.

No `claude` CLI, no network: `plan_tasks` takes an injected `call(prompt)`
that returns canned planner text, and `SwarmState` is driven directly against
a tmp state dir. The full run_swarm path needs live agents and is covered
elsewhere (dogfood / e2e).
"""

import json

from cafecito.fleetstate import SwarmState, read_snapshot
from cafecito.swarm import plan_tasks


def _fake_call(text):
    """A `call` seam that ignores the prompt and returns fixed text."""
    return lambda _prompt: text


# ---------------------------------------------------------------------------
# plan_tasks
# ---------------------------------------------------------------------------

def test_valid_json_wrapped_in_prose_is_parsed():
    text = (
        "Sure! Here is the plan:\n"
        '[{"id": "a", "title": "Task A", "brief": "Do a thing here.",'
        ' "paths": ["src/a.py"]},'
        ' {"id": "b", "title": "Task B", "brief": "Do another thing.",'
        ' "paths": ["src/b.py", "tests/test_b.py"]}]\n'
        "Hope that helps!"
    )
    tasks = plan_tasks("goal", "listing", 3, _fake_call(text))
    assert [t["id"] for t in tasks] == ["a", "b"]
    assert tasks[0]["title"] == "Task A"
    assert tasks[1]["paths"] == ["src/b.py", "tests/test_b.py"]


def test_overlapping_paths_offender_dropped():
    text = (
        '[{"id": "a", "title": "A", "brief": "first task.",'
        ' "paths": ["src/shared.py"]},'
        ' {"id": "b", "title": "B", "brief": "collides with a.",'
        ' "paths": ["src/shared.py"]},'
        ' {"id": "c", "title": "C", "brief": "disjoint task.",'
        ' "paths": ["src/c.py"]}]'
    )
    tasks = plan_tasks("goal", "listing", 5, _fake_call(text))
    # a and c survive; b overlaps a's path and is dropped.
    assert [t["id"] for t in tasks] == ["a", "c"]
    claimed = [p for t in tasks for p in t["paths"]]
    assert len(claimed) == len(set(claimed))  # pairwise disjoint


def test_more_tasks_than_cap_is_truncated():
    items = [
        f'{{"id": "t{i}", "title": "T{i}", "brief": "task {i}.",'
        f' "paths": ["src/f{i}.py"]}}'
        for i in range(6)
    ]
    text = "[" + ",".join(items) + "]"
    tasks = plan_tasks("goal", "listing", 2, _fake_call(text))
    assert len(tasks) == 2
    assert [t["id"] for t in tasks] == ["t0", "t1"]


def test_invalid_json_returns_empty():
    tasks = plan_tasks("goal", "listing", 3, _fake_call("no json here at all"))
    assert tasks == []


def test_malformed_array_returns_empty():
    # An opening bracket but broken JSON inside.
    tasks = plan_tasks("goal", "listing", 3,
                       _fake_call('[{"id": "a", broken}]'))
    assert tasks == []


def test_bad_shape_tasks_are_skipped():
    text = (
        '[{"id": "a", "title": "A", "brief": "ok task.", "paths": ["src/a.py"]},'
        ' {"id": "", "title": "empty id", "brief": "b.", "paths": ["x.py"]},'
        ' {"id": "c", "title": "C", "brief": "no paths.", "paths": []},'
        ' {"id": "d", "title": "D", "brief": "too many paths.",'
        '  "paths": ["1.py", "2.py", "3.py", "4.py"]},'
        ' "not a dict",'
        ' {"id": "e", "title": "E", "brief": "good tail task.",'
        '  "paths": ["src/e.py"]}]'
    )
    tasks = plan_tasks("goal", "listing", 10, _fake_call(text))
    assert [t["id"] for t in tasks] == ["a", "e"]


# ---------------------------------------------------------------------------
# SwarmState transitions
# ---------------------------------------------------------------------------

def test_state_lifecycle_written_to_json(tmp_path):
    state = SwarmState(tmp_path)
    state.start("ship the feature")

    snap = read_snapshot(tmp_path)
    assert snap["goal"] == "ship the feature"
    assert snap["done"] is False
    assert snap["agents"] == {}

    state.update("task-1", "planning", title="Task One", detail="reserving")
    state.update("task-1", "working", detail="building")
    state.update("task-1", "submitting", detail="head abc123")
    state.update("task-1", "landed", detail="gate 2.0s")

    snap = read_snapshot(tmp_path)
    agent = snap["agents"]["task-1"]
    assert agent["state"] == "landed"
    assert agent["title"] == "Task One"  # sticky across updates without title
    assert agent["detail"] == "gate 2.0s"

    state.finish()
    snap = read_snapshot(tmp_path)
    assert snap["done"] is True


def test_state_multiple_agents_independent(tmp_path):
    state = SwarmState(tmp_path)
    state.start("goal")
    state.update("a", "landed", title="A", detail="ok")
    state.update("b", "escalated", title="B", detail="failed landing gate")
    state.update("c", "failed", title="C", detail="no changes")

    snap = read_snapshot(tmp_path)
    assert snap["agents"]["a"]["state"] == "landed"
    assert snap["agents"]["b"]["state"] == "escalated"
    assert snap["agents"]["c"]["state"] == "failed"


def test_read_snapshot_missing_is_none(tmp_path):
    assert read_snapshot(tmp_path) is None


def test_state_json_is_valid_on_disk(tmp_path):
    state = SwarmState(tmp_path)
    state.start("goal")
    state.update("x", "working", title="X")
    raw = (tmp_path / "swarm.json").read_text()
    json.loads(raw)  # must be valid JSON
