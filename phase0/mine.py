"""Mine genuinely-concurrent branch pairs from a repo's merge history.

A mainline merge commit M (parents P1=mainline, P2=feature head) reconstructs a
real PR branch: it diverged at merge-base(P1, P2) and landed at M's commit
time. Two branches whose development intervals overlap were in flight
simultaneously — exactly the pairs a merge queue would serialize today.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from gitutil import git, git_rc


@dataclass(frozen=True)
class Branch:
    merge: str    # merge commit on mainline
    head: str     # feature branch tip (P2)
    base: str     # merge-base(P1, P2) — where the branch diverged
    start: int    # earliest author time of a branch-only commit (epoch s)
    end: int      # commit time of the merge (epoch s)
    subject: str


def mine_branches(
    repo: str,
    since: str,
    max_commits: int = 50,
    max_files: int = 100,
) -> list[Branch]:
    """Reconstruct PR branches from mainline merge commits since `since`.

    Branches with more than `max_commits` commits or `max_files` changed files
    are dropped — release trains and vendored bumps, noise for this experiment.
    """
    out = git(
        repo, "log", "--merges", "--first-parent", "HEAD",
        f"--since={since}", "--format=%H %ct %P|%s",
    )
    branches: list[Branch] = []
    for line in out.splitlines():
        meta, _, subject = line.partition("|")
        parts = meta.split()
        if len(parts) != 4:  # sha, committer-time, exactly two parents
            continue
        merge, ct, p1, p2 = parts
        code, base, _ = git_rc(repo, "merge-base", p1, p2)
        if code != 0:  # shallow-clone boundary — history not available
            continue
        base = base.strip()
        if base == p2:  # fast-forward-ish, no distinct branch
            continue
        code, times, _ = git_rc(repo, "log", "--format=%at", f"{base}..{p2}")
        stamps = [int(t) for t in times.split()] if code == 0 else []
        if not stamps or len(stamps) > max_commits:
            continue
        code, names, _ = git_rc(repo, "diff", "--name-only", base, p2)
        nfiles = len(names.splitlines()) if code == 0 else 0
        if nfiles == 0 or nfiles > max_files:
            continue
        branches.append(Branch(merge, p2, base, min(stamps), int(ct), subject.strip()))
    return branches


def concurrent_pairs(
    branches: list[Branch],
    max_pairs: int,
    seed: int = 42,
) -> list[tuple[Branch, Branch]]:
    """All interval-overlapping pairs, sampled down to `max_pairs`."""
    pairs = [
        (a, b)
        for i, a in enumerate(branches)
        for b in branches[i + 1:]
        if a.start < b.end and b.start < a.end
    ]
    rng = random.Random(seed)
    if len(pairs) > max_pairs:
        pairs = rng.sample(pairs, max_pairs)
    return pairs


def is_dependent(repo: str, a: Branch, b: Branch) -> bool:
    """True if one branch contains the other (stacked, not concurrent)."""
    for x, y in ((a, b), (b, a)):
        code, _, _ = git_rc(repo, "merge-base", "--is-ancestor", x.head, y.head)
        if code == 0:
            return True
    return False
