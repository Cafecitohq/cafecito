"""cafecito ingest — the gateway's PR path (SPEC §7).

Humans keep their normal GitHub flow: open a PR. Ingest polls open PRs,
fetches each head (fork PRs included — `refs/pull/N/head` covers them),
submits it through the engine like any other changeset — commute → land,
collide → regenerate, contradiction → escalate — and reports the verdict
back to the PR as a comment plus a `cafecito:<verdict>` label. Re-pushed
heads are re-ingested; unchanged heads are skipped.

GitHub access goes through the `gh` CLI (same external-binary class as `git`
and `claude`; no Python dependencies). All `gh`/fetch calls sit behind
module-level seams so the whole loop is unit-testable offline.

Ingest never closes PRs: the landed commit is a new engine-authored commit
(Changeset-Id trailer), so GitHub won't auto-mark the PR merged. The comment
says exactly what landed; closing stays a human decision.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import time

from .engine import Engine
from .gitutil import git_rc

LABELS = {
    "landed": ("2a9d8f", "landed on cafecito/main by the integration plane"),
    "escalated": ("d9694b", "cafecito could not land this automatically"),
    "rejected": ("7c6d61", "cafecito rejected this submission"),
}

_SLUG_RE = re.compile(r"github\.com[:/]([^/]+/[^/.]+?)(?:\.git)?/?$")


def slug_from_origin(repo: str) -> str | None:
    code, out, _ = git_rc(repo, "remote", "get-url", "origin")
    if code != 0:
        return None
    m = _SLUG_RE.search(out.strip())
    return m.group(1) if m else None


# ------------------------------------------------------------- gh seams ----

def _gh(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True,
                          timeout=timeout)


def _list_prs(slug: str) -> list[dict]:
    r = _gh("pr", "list", "--repo", slug, "--state", "open", "--limit", "50",
            "--json", "number,title,headRefOid")
    if r.returncode != 0:
        raise RuntimeError(f"gh pr list failed: {r.stderr.strip()[:150]}")
    return json.loads(r.stdout or "[]")


def _fetch_pr_head(repo: str, slug: str, number: int, sha: str) -> bool:
    code, _, _ = git_rc(repo, "rev-parse", "--verify", f"{sha}^{{commit}}")
    if code == 0:
        return True
    code, _, err = git_rc(repo, "fetch", "--quiet",
                          f"https://github.com/{slug}",
                          f"refs/pull/{number}/head")
    if code != 0:
        return False
    code, _, _ = git_rc(repo, "rev-parse", "--verify", f"{sha}^{{commit}}")
    return code == 0


def _comment(slug: str, number: int, body: str) -> None:
    _gh("pr", "comment", str(number), "--repo", slug, "--body", body)


def _label(slug: str, number: int, verdict: str) -> None:
    for name in LABELS:
        _gh("pr", "edit", str(number), "--repo", slug,
            "--remove-label", f"cafecito:{name}")
    _gh("pr", "edit", str(number), "--repo", slug,
        "--add-label", f"cafecito:{verdict}")


def _ensure_labels(slug: str) -> None:
    for name, (color, desc) in LABELS.items():
        _gh("label", "create", f"cafecito:{name}", "--repo", slug,
            "--color", color, "--description", desc, "--force")


# ---------------------------------------------------------------- state ----

class IngestState:
    def __init__(self, state_dir: pathlib.Path):
        self.path = pathlib.Path(state_dir) / "ingest.json"
        try:
            self._d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self._d = {}

    def seen(self, number: int, head: str) -> bool:
        return self._d.get(str(number), {}).get("head") == head

    def record(self, number: int, head: str, verdict: str) -> None:
        self._d[str(number)] = {"head": head, "verdict": verdict,
                                "at": time.time()}
        self.path.write_text(json.dumps(self._d, indent=1))


# ----------------------------------------------------------------- loop ----

def _verdict_message(pr: dict, res: dict) -> str:
    v = res.get("verdict")
    if v == "landed":
        gate = res.get("gate") or {}
        regen = " (colliding regions were regenerated)" if res.get(
            "regenerated") else ""
        return (f"☕ **landed** on `cafecito/main` as `{res['tip'][:12]}`"
                f"{regen} — gate: {gate.get('summary', 'green')}.\n\n"
                f"This PR's changes are integrated (engine-authored commit "
                f"with a `Changeset-Id` trailer); close when convenient.")
    if v == "escalated":
        return (f"☕ **escalated** — cafecito could not land this "
                f"automatically: {res.get('reason')}.\n\nFix and push; "
                f"the new head will be re-ingested.")
    return f"☕ **rejected**: {res.get('reason')}."


def ingest_once(engine: Engine, slug: str, state: IngestState,
                report: bool = True) -> list[tuple[int, str]]:
    """One poll cycle. Returns [(pr_number, verdict), ...] for acted PRs."""
    acted: list[tuple[int, str]] = []
    for pr in _list_prs(slug):
        number, head = pr["number"], pr["headRefOid"]
        if state.seen(number, head):
            continue
        if not _fetch_pr_head(engine.repo, slug, number, head):
            state.record(number, head, "rejected")
            if report:
                _comment(slug, number, "☕ **rejected**: could not fetch the "
                                       "PR head.")
            acted.append((number, "rejected"))
            continue
        res = engine.submit(head, agent=f"pr/{number}",
                            title=pr.get("title", f"PR #{number}"))
        verdict = res.get("verdict", "rejected")
        state.record(number, head, verdict)
        if report:
            _comment(slug, number, _verdict_message(pr, res))
            _label(slug, number,
                   verdict if verdict in LABELS else "rejected")
        acted.append((number, verdict))
    return acted


def run_ingest(args) -> int:
    engine = Engine(args.repo)
    slug = getattr(args, "github", None) or slug_from_origin(engine.repo)
    if not slug:
        print("no GitHub repo: pass --github owner/repo or set an origin remote")
        return 2
    state = IngestState(engine.state_dir)
    report = not getattr(args, "no_report", False)
    if report:
        _ensure_labels(slug)
    print(f"ingest: watching {slug} → {engine.config['branch']}")
    while True:
        acted = ingest_once(engine, slug, state, report=report)
        for number, verdict in acted:
            print(f"  PR #{number}: {verdict}")
        if getattr(args, "once", False):
            return 0
        time.sleep(getattr(args, "poll", 60))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("use: cafecito ingest")
