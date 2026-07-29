"""Thin subprocess wrapper around git. Stdlib only."""

from __future__ import annotations

import subprocess


class GitError(RuntimeError):
    pass


def git(repo: str, *args: str) -> str:
    """Run git in `repo`, return stdout. Raises GitError on nonzero exit."""
    code, out, err = git_rc(repo, *args)
    if code != 0:
        raise GitError(f"git {' '.join(args)} failed ({code}): {err.strip()[:200]}")
    return out


def git_rc(repo: str, *args: str, env: dict | None = None) -> tuple[int, str, str]:
    """Run git in `repo`, return (exit_code, stdout, stderr). Never raises.

    `env` exists so a caller can hand git an installation token through the
    environment — `gh auth git-credential` reads GH_TOKEN from there, which
    keeps the credential out of argv and out of the reflog. None means
    inherit, which is what every other caller wants."""
    r = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        errors="replace",
        env=env,
    )
    return r.returncode, r.stdout, r.stderr


def show(repo: str, rev: str, path: str) -> str | None:
    """Content of `path` at `rev`, or None if it doesn't exist there."""
    code, out, _ = git_rc(repo, "show", f"{rev}:{path}")
    return out if code == 0 else None
