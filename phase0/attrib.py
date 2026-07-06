"""Pair-attributable conflict detection via rebase simulation.

Naively merging two branch heads contaminates the result: the 3-way base is
their *mutual* merge-base, so each "side" includes every third-party mainline
commit that landed between the two branch points. Conflicts then can't be
attributed to the pair.

Fix: replay the older-based branch onto the newer branch's base first
(merge-tree with an explicit base + a synthetic commit via commit-tree — pure
plumbing, no worktree), then merge the newer branch against that. Both sides
of the final 3-way now contain exactly one branch's changes.

  older.base ─── newer.base ────────────── (mainline)
        \             \        \
         older.head    \        newer.head
                        \
                         C = newer.base + older's changes   (rebase sim)
  conflict(pair) := merge-tree(C, newer.head, base=newer.base) conflicts

Statuses:
  clean    — pair merges cleanly; any naive conflict was mainline drift
  conflict — the two branches genuinely collide
  drift    — older branch conflicts with intervening mainline itself;
             pair attribution impossible (excluded from rates)
  error    — missing history (shallow boundary) etc.
"""

from __future__ import annotations

from dataclasses import dataclass

from gitutil import git_rc
from mine import Branch


@dataclass
class Attributed:
    status: str            # clean | conflict | drift | error
    ours: str = ""         # commit holding the replayed branch's changes
    theirs: str = ""       # the other branch's head
    base: str = ""         # true common base of ours/theirs
    replayed: str = ""     # head of the branch that was replayed (maps ours→a|b)
    conflicted: list[str] | None = None  # paths, when status == "conflict"


def attributed_merge(repo: str, a: Branch, b: Branch) -> Attributed:
    # Order so `old` is the branch whose base is the mainline ancestor.
    if a.base == b.base:
        old, new, ours = a, b, a.head
    else:
        code, _, _ = git_rc(repo, "merge-base", "--is-ancestor", a.base, b.base)
        if code == 0:
            old, new = a, b
        else:
            code, _, _ = git_rc(repo, "merge-base", "--is-ancestor", b.base, a.base)
            if code != 0:
                return Attributed("error")  # bases unrelated (shallow boundary)
            old, new = b, a
        # Replay old's changes onto new.base.
        code, out, _ = git_rc(repo, "merge-tree", "--write-tree", "--no-messages",
                              f"--merge-base={old.base}", new.base, old.head)
        if code == 1:
            return Attributed("drift")
        if code != 0:
            return Attributed("error")
        tree = out.splitlines()[0].strip()
        code, out, _ = git_rc(repo, "commit-tree", tree, "-p", new.base,
                              "-m", "cafecito rebase-sim")
        if code != 0:
            return Attributed("error")
        ours = out.strip()

    code, out, _ = git_rc(repo, "merge-tree", "--write-tree", "--name-only",
                          "--no-messages", f"--merge-base={new.base}", ours, new.head)
    if code == 0:
        return Attributed("clean", ours, new.head, new.base, old.head)
    if code != 1:
        return Attributed("error")
    paths = sorted({p for p in out.splitlines()[1:] if p})
    return Attributed("conflict", ours, new.head, new.base, old.head, paths)
