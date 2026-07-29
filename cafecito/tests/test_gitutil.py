"""git_rc's environment passthrough — the one seam that lets an installation
token reach `git` without touching argv or the reflog."""

import types

from cafecito import gitutil


def test_git_rc_passes_an_environment_through(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gitutil.subprocess, "run", fake_run)
    gitutil.git_rc(".", "status", env={"GH_TOKEN": "x"})
    assert seen["env"] == {"GH_TOKEN": "x"}
    gitutil.git_rc(".", "status")
    assert seen["env"] is None
