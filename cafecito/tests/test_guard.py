"""The guard. Nothing here opens a socket; the git-backed tests run entirely
against local repositories, so these stay green inside our sandboxed gate."""

import io
import json
import subprocess
import sys
import time

import pytest

from cafecito import guard


def write_plane(root, *, leases=None, cfg=None, tip="abc123def456"):
    d = root / ".cafecito"
    d.mkdir(exist_ok=True)
    (d / "config.json").write_text(json.dumps(cfg or {}))
    (d / "leases.json").write_text(json.dumps(leases or {}))
    (d / "state.json").write_text(json.dumps({"tip": tip}))
    return root


@pytest.fixture
def plane(tmp_path):
    return write_plane((tmp_path / "repo").resolve().absolute()
                       if (tmp_path / "repo").exists() else
                       _mk(tmp_path / "repo"))


def _mk(p):
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()


def lease(agent="a1", ttl=900, intent="work"):
    return {"agent": agent, "intent": intent, "expires": time.time() + ttl}


def edit(path, cwd, **kw):
    return {"hook_event_name": "PreToolUse", "cwd": str(cwd),
            "tool_input": {"file_path": path}, **kw}


def shell(command, cwd, **kw):
    return {"hook_event_name": "PreToolUse", "cwd": str(cwd),
            "tool_input": {"command": command}, **kw}


def decide(payload, repo):
    return guard.decide(payload, str(repo))


# ------------------------------------------------------------- edit gate ---

def test_unreserved_edit_is_denied_with_the_reserve_call(plane):
    code, out, err = decide(edit("cafecito/engine.py", plane), plane)
    assert code == 0 and err is None
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    reason = hso["permissionDecisionReason"]
    assert 'reserve(keys=["file:cafecito/engine.py"]' in reason
    assert reason.strip()          # Codex hard-errors on an empty reason


def test_a_file_lease_allows_the_edit(plane):
    write_plane(plane, leases={"file:a/b.py": lease()})
    assert decide(edit("a/b.py", plane), plane)[1] is None


def test_a_symbol_lease_allows_edits_to_its_file(plane):
    """The gate asks 'was reserve called', not 'does this symbol match' —
    adjudicating inside a file is the plane's job, not the hot path's."""
    write_plane(plane, leases={"py:a/b.py::C.m": lease()})
    assert decide(edit("a/b.py", plane), plane)[1] is None


def test_a_lease_on_a_sibling_file_does_not_allow_the_edit(plane):
    write_plane(plane, leases={"file:a/other.py": lease()})
    assert decide(edit("a/b.py", plane), plane)[1] is not None


def test_an_expired_lease_does_not_allow_the_edit(plane):
    write_plane(plane, leases={"file:a/b.py": lease(ttl=-1)})
    assert decide(edit("a/b.py", plane), plane)[1] is not None


@pytest.mark.parametrize("path", [".git/config", ".cafecito/leases.json"])
def test_plane_and_git_internals_are_not_gated(plane, path):
    assert decide(edit(path, plane), plane)[1] is None


def test_paths_outside_the_repo_are_not_gated(plane, tmp_path):
    outside = tmp_path / "elsewhere.txt"
    assert decide(edit(str(outside), plane), plane)[1] is None


def test_absolute_in_repo_paths_are_gated(plane):
    assert decide(edit(str(plane / "a" / "b.py"), plane), plane)[1] is not None


# ---------------------------------------------------------------- renewal ---

def test_a_lease_near_expiry_is_renewed_on_an_allowed_edit(plane):
    """The 900s TTL predates anything watching the agent; without renewal the
    gate would deny an agent its own files 15 minutes into a task."""
    write_plane(plane, leases={"file:a/b.py": lease(ttl=60)})
    assert decide(edit("a/b.py", plane), plane)[1] is None
    after = json.loads((plane / ".cafecito" / "leases.json").read_text())
    assert after["file:a/b.py"]["expires"] > time.time() + 800


def test_a_fresh_lease_is_left_alone(plane):
    write_plane(plane, leases={"file:a/b.py": lease(ttl=900)})
    before = json.loads((plane / ".cafecito" / "leases.json").read_text())
    decide(edit("a/b.py", plane), plane)
    after = json.loads((plane / ".cafecito" / "leases.json").read_text())
    assert after == before


# ------------------------------------------------------------ portability ---

FORBIDDEN = ("\"permissionDecision\": \"allow\"", "\"permissionDecision\": \"ask\"",
             "updatedInput", "\"continue\"")


def test_allow_is_always_silence(plane):
    """Codex rejects an explicit allow. Every allowed path must emit nothing."""
    write_plane(plane, leases={"file:a/b.py": lease()})
    for payload in (edit("a/b.py", plane), shell("ls -la", plane),
                    {"hook_event_name": "Notification", "cwd": str(plane)}):
        assert decide(payload, plane)[1] is None


def test_no_decision_shape_codex_rejects_is_ever_emitted(plane):
    write_plane(plane, leases={"file:x.py": lease()})
    payloads = [edit("a/b.py", plane), edit("x.py", plane),
                shell("git commit -m x", plane), shell("git push origin main", plane),
                {"hook_event_name": "SessionStart", "cwd": str(plane)}]
    for p in payloads:
        out = decide(p, plane)[1]
        blob = json.dumps(out or {})
        for bad in FORBIDDEN:
            assert bad not in blob, f"{bad} in {blob}"


# --------------------------------------------------------------- shell gate ---

def test_commit_without_a_lease_is_nudged_not_denied(plane):
    """Landing is commit-then-submit, so denying commit breaks the happy path.
    The push is the drift event."""
    out = decide(shell("git commit -m 'wip'", plane), plane)[1]
    assert "hookSpecificOutput" not in out
    assert "submit" in out["additionalContext"]


def test_commit_with_a_lease_held_is_silent(plane):
    write_plane(plane, leases={"file:a/b.py": lease()})
    assert decide(shell("git commit -m x", plane), plane)[1] is None


def test_git_ops_splits_chains_and_respects_quoting():
    ops = guard.git_ops("git add -A && git commit -m 'a && b' && git push origin main")
    assert [guard.subcommand(o) for o in ops] == ["add", "commit", "push"]
    assert "main" in ops[2]


def test_git_ops_survives_unbalanced_quotes():
    assert guard.git_ops("git push 'oops") is not None


# -------------------------------------------------- shell gate, against git ---

def git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, check=True)


@pytest.fixture
def repo(tmp_path):
    r = _mk(tmp_path / "gitrepo")
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@t.t")
    git(r, "config", "user.name", "t")
    (r / "f.txt").write_text("one")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "one")
    git(r, "branch", "cafecito/main")
    return write_plane(r)


def test_pushing_a_landed_main_is_allowed(repo):
    """`git push origin main cafecito/main` right after a ff-merge is the
    documented deploy step. Denying it would break the release ritual."""
    out = decide(shell("git push origin main cafecito/main", repo), repo)[1]
    assert out is None


def test_pushing_a_drifted_main_is_denied(repo):
    (repo / "f.txt").write_text("two")
    git(repo, "commit", "-aqm", "around the plane")
    out = decide(shell("git push origin main", repo), repo)[1]
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "never went through the plane" in reason


def test_pushing_an_unprotected_branch_is_allowed(repo):
    git(repo, "checkout", "-qb", "feature")
    (repo / "f.txt").write_text("two")
    git(repo, "commit", "-aqm", "wip")
    assert decide(shell("git push origin feature", repo), repo)[1] is None


# ---------------------------------------------------------------- stop gate ---

def test_stop_is_blocked_when_leases_are_held_and_work_is_dirty(repo):
    write_plane(repo, leases={"file:f.txt": lease()})
    (repo / "f.txt").write_text("uncommitted")
    code, out, err = decide({"hook_event_name": "Stop", "cwd": str(repo)}, repo)
    assert code == 2 and "uncommitted changes" in err


def test_stop_hook_active_always_allows(repo):
    """The loop breaker. Without it a blocked stop can re-block forever."""
    write_plane(repo, leases={"file:f.txt": lease()})
    (repo / "f.txt").write_text("uncommitted")
    code, _, err = decide({"hook_event_name": "Stop", "cwd": str(repo),
                           "stop_hook_active": True}, repo)
    assert code == 0 and err is None


def test_stop_with_a_clean_tree_allows(repo):
    """Plane state lives in an untracked `.cafecito/` unless the project
    gitignores it, and that must not read as unlanded work."""
    write_plane(repo, leases={"file:f.txt": lease()})
    assert decide({"hook_event_name": "Stop", "cwd": str(repo)}, repo)[0] == 0


def test_stop_without_leases_allows(repo):
    (repo / "f.txt").write_text("uncommitted")
    assert decide({"hook_event_name": "Stop", "cwd": str(repo)}, repo)[0] == 0


# ------------------------------------------------------------ session start ---

def test_session_start_injects_tip_and_leases(plane):
    write_plane(plane, leases={"file:a/b.py": lease(agent="builder")},
                cfg={"test_cmd": ["/long/venv/path/bin/python", "-m", "pytest",
                                  "-q"]},
                tip="2fe72ca8ce49aaaa")
    out = decide({"hook_event_name": "SessionStart", "cwd": str(plane)}, plane)[1]
    ctx = out["additionalContext"]
    assert "2fe72ca8ce49" in ctx and "builder" in ctx
    # the runner must survive, the venv path must not eat the line
    assert "python -m pytest" in ctx and "/long/venv" not in ctx


def test_session_start_says_so_when_nothing_is_held(plane):
    out = decide({"hook_event_name": "SessionStart", "cwd": str(plane)}, plane)[1]
    assert "No active leases" in out["additionalContext"]


# ------------------------------------------------------------- escape paths ---

def test_no_plane_means_no_opinion(tmp_path):
    bare = _mk(tmp_path / "bare")
    assert decide(edit("a.py", bare), bare)[1] is None


def test_disabled_in_config_is_silent(plane):
    write_plane(plane, cfg={"guard": {"enabled": False}})
    assert decide(edit("a/b.py", plane), plane)[1] is None


def test_env_kill_switch(plane, monkeypatch):
    monkeypatch.setenv("CAFECITO_GUARD", "off")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(edit("a/b.py", plane))))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert guard.main([]) == 0
    assert out.getvalue() == ""


def test_protected_refs_are_configurable(repo):
    write_plane(repo, cfg={"guard": {"protected_refs": ["release"]}})
    (repo / "f.txt").write_text("two")
    git(repo, "commit", "-aqm", "drift")
    git(repo, "branch", "release")          # release now carries unlanded work
    assert decide(shell("git push origin release", repo), repo)[1] is not None
    # main drifted too, but it is not protected in this config
    assert decide(shell("git push origin main", repo), repo)[1] is None


# --------------------------------------------------------------- fail open ---

def test_corrupt_leases_do_not_wedge_the_session(plane):
    """Unreadable state must not become 'deny every edit forever'."""
    (plane / ".cafecito" / "leases.json").write_text("{not json")
    assert guard.live_leases(plane) == {}


def test_a_bug_in_decide_fails_open_and_says_so(plane, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(guard, "decide", boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(edit("a/b.py", plane))))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert guard.main([]) == 0
    assert "failed open" in out.getvalue() and "synthetic" in out.getvalue()


def test_garbage_on_stdin_fails_open(plane, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("<<<not json"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert guard.main([]) == 0


# ------------------------------------------------------------ latency shape ---

def test_guard_does_not_import_the_engine():
    """The budget is 120ms for the whole call and `cafecito.cli` costs ~100ms
    to import. Asserting the import graph is the non-flaky way to hold that
    line — wall-clock would just measure the CI box."""
    r = subprocess.run(
        [sys.executable, "-c",
         "import cafecito.guard, sys; "
         "print(any(m.startswith('cafecito.engine') for m in sys.modules))"],
        capture_output=True, text=True, timeout=60)
    assert r.stdout.strip() == "False", r.stdout + r.stderr


def test_the_allowed_path_reads_one_file_and_no_git(plane, monkeypatch):
    """The hot path fires on every edit; it must not shell out."""
    write_plane(plane, leases={"file:a/b.py": lease()})
    monkeypatch.setattr(guard.subprocess if hasattr(guard, "subprocess") else
                        subprocess, "run", _forbidden)
    assert decide(edit("a/b.py", plane), plane)[1] is None


def _forbidden(*a, **kw):
    raise AssertionError("the allowed edit path must not run a subprocess")
