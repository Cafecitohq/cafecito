"""Deterministic tests for the pure parts of `cafecito watch` — no terminal,
no sleeping, no ANSI cursor tricks. We build a fake engine state dir on disk
and assert what snapshot() reads and what render() paints."""

import json
import time

from cafecito.watch import render, snapshot


def _write(state_dir, name, obj):
    (state_dir / name).write_text(json.dumps(obj))


def _fake_repo(tmp_path):
    """A repo whose .cafecito/ has state, config, a mixed log, live+expired
    leases, and a two-agent swarm snapshot."""
    now = time.time()
    state = tmp_path / ".cafecito"
    state.mkdir()

    _write(state, "config.json", {"branch": "cafecito/trunk"})
    _write(state, "state.json", {"tip": "abcdef1234567890deadbeef"})

    landed = {
        "id": "cs_land01", "verdict": "landed", "title": "add retry backoff",
        "agent": "worker-1", "regen_s": 12,
        "gate": {"green": True, "no_signal": False, "seconds": 0.9,
                 "memo": {"hits": 3, "runs": 1}},
    }
    escalated = {
        "id": "cs_esc01", "verdict": "escalated", "title": "rework parser core",
        "agent": "worker-2", "reason": "failed landing gate",
        "gate": {"green": False, "no_signal": False, "seconds": 2.1},
    }
    plain = {"id": "cs_land02", "verdict": "landed", "title": "tidy imports",
             "agent": "worker-1", "regen_s": None,
             "gate": {"green": True, "no_signal": True, "seconds": 0.0}}
    with (state / "log.jsonl").open("w") as f:
        for e in (plain, landed, escalated):
            f.write(json.dumps(e) + "\n")

    _write(state, "leases.json", {
        "src/api.py": {"agent": "worker-1", "intent": "wire retries",
                       "expires": now + 300},
        "src/old.py": {"agent": "worker-9", "intent": "stale",
                       "expires": now - 60},  # expired -> excluded
    })

    _write(state, "swarm.json", {
        "goal": "harden the client", "started_at": now, "updated_at": now,
        "done": False,
        "agents": {
            "worker-1": {"state": "working", "title": "retry backoff",
                         "detail": "editing api.py", "updated_at": now},
            "worker-2": {"state": "escalated", "title": "parser rework",
                         "detail": "gate red", "updated_at": now},
        },
    })
    return tmp_path


def test_snapshot_reads_tip_counts_and_leases(tmp_path):
    snap = snapshot(str(_fake_repo(tmp_path)))
    assert snap["present"] is True
    assert snap["tip"] == "abcdef1234567890deadbeef"
    assert snap["branch"] == "cafecito/trunk"
    assert snap["landed"] == 2
    assert snap["escalated"] == 1
    # Expired lease excluded; only the live one survives.
    keys = [lz["key"] for lz in snap["leases"]]
    assert keys == ["src/api.py"]
    assert snap["leases"][0]["agent"] == "worker-1"
    # Two agents present in the swarm section.
    assert set(snap["swarm"]["agents"]) == {"worker-1", "worker-2"}


def test_render_plain_has_content_and_no_ansi(tmp_path):
    snap = snapshot(str(_fake_repo(tmp_path)))
    out = render(snap, width=100, color=False)
    assert "\x1b" not in out                  # no escape codes at all
    assert "cafecito/trunk" in out            # branch in header
    assert "add retry backoff" in out         # landed title
    assert "failed landing gate" in out       # escalated reason marker text
    assert "retry backoff" in out             # swarm agent title
    assert "parser rework" in out             # other swarm agent title
    assert "src/api.py" in out                # live lease
    assert "src/old.py" not in out            # expired lease absent
    assert "memo 3/1" in out                  # compact gate suffix
    assert "regen 12s" in out


def test_render_color_emits_escapes(tmp_path):
    snap = snapshot(str(_fake_repo(tmp_path)))
    out = render(snap, width=100, color=True)
    assert "\x1b[" in out


def test_missing_cafecito_shows_init_hint(tmp_path):
    snap = snapshot(str(tmp_path))              # no .cafecito dir
    assert snap["present"] is False
    out = render(snap, width=80, color=False)
    assert "cafecito init" in out
    assert "\x1b" not in out


def test_every_line_respects_width(tmp_path):
    snap = snapshot(str(_fake_repo(tmp_path)))
    for width in (30, 60, 80, 100, 120):
        out = render(snap, width=width, color=False)
        for line in out.splitlines():
            assert len(line) <= width, (width, repr(line))


def test_color_line_length_ignores_escape_bytes(tmp_path):
    """With color on, visible content still fits width — the extra bytes are
    escape sequences, not printable columns."""
    snap = snapshot(str(_fake_repo(tmp_path)))
    out = render(snap, width=80, color=True)
    for line in out.splitlines():
        visible = line
        for code in ("\x1b[0m", "\x1b[1m", "\x1b[2m", "\x1b[31m", "\x1b[32m",
                     "\x1b[33m", "\x1b[34m", "\x1b[35m", "\x1b[36m"):
            visible = visible.replace(code, "")
        assert len(visible) <= 80, repr(line)
