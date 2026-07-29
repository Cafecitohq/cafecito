"""cafecito ingest — the gateway's PR path (SPEC §7).

Humans keep their normal GitHub flow: open a PR. Ingest fetches each head
(fork PRs included — `refs/pull/N/head` covers them), submits it through the
engine like any other changeset — commute → land, collide → regenerate,
contradiction → escalate — and reports the verdict back to the PR as a
comment plus a `cafecito:<verdict>` label. Re-pushed heads are re-ingested;
unchanged heads are skipped.

Two drivers share that work. `cafecito ingest` polls open PRs on a timer;
`cafecito gateway` receives webhook deliveries and calls `ingest_pr` per PR.
The cut between them is exactly the `for` statement in `ingest_once`: which
PRs changed is the only thing polling contributes that events do not.

GitHub access goes through the `gh` CLI (same external-binary class as `git`
and `claude`; no Python dependencies). All `gh`/fetch calls sit behind
module-level seams so the whole loop is unit-testable offline. Identity is
bound per thread with `gh_identity` rather than by mutating os.environ: the
gate's setup command and the generator commands both inherit this process's
environment while running code from the submitted head, and a fork PR's
`package.json` must not be handed a write-scoped credential.

Ingest never closes PRs: the landed commit is a new engine-authored commit
(Changeset-Id trailer), so GitHub won't auto-mark the PR merged. The comment
says exactly what landed; closing stays a human decision.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from typing import Callable, Iterator

from .engine import Engine, _FileLock
from .ghapp import redact
from .gitutil import git_rc

LABELS = {
    "landed": ("2a9d8f", "landed on cafecito/main by the integration plane"),
    "escalated": ("d9694b", "cafecito could not land this automatically"),
    "rejected": ("7c6d61", "cafecito rejected this submission"),
}

_SLUG_RE = re.compile(r"github\.com[:/]([^/]+/[^/.]+?)(?:\.git)?/?$")

# The empty first helper resets the inherited chain: without it a
# maintainer's osxkeychain entry shadows the App on a dev machine.
GIT_CREDENTIAL_ARGS = ("-c", "credential.helper=",
                       "-c", "credential.helper=!gh auth git-credential")

# A `synchronize` delivery can beat GitHub's own update of refs/pull/N/head,
# and giving up on the first miss posts a wrong "could not fetch" rejection.
_FETCH_BACKOFF_S = (1, 3, 8)

# States that are NOT a verdict on the submitted code: infrastructure said no.
# Only a verdict is terminal. A terminal record is permanent — the claim
# answers "already worked" forever, GitHub never redelivers, and the catch-up
# sweep is the only retry that exists — so writing one for a transient
# failure makes that head unlandable for good. These never block a claim;
# they are remembered only so the author is told once instead of once a poll.
#   unfetched  refs/pull/N/head could not be fetched (GitHub unreachable, or
#              the ref had not appeared yet)
#   contended  the engine's admission window closed behind an overlapping
#              changeset — a queueing fact, not a judgement
RETRYABLE = ("unfetched", "contended")
_ADMISSION_TIMEOUT = "admission timeout behind "

_identity: contextvars.ContextVar = contextvars.ContextVar(
    "cafecito_gh_identity", default=None)


def slug_from_origin(repo: str) -> str | None:
    code, out, _ = git_rc(repo, "remote", "get-url", "origin")
    if code != 0:
        return None
    m = _SLUG_RE.search(out.strip())
    return m.group(1) if m else None


# ---------------------------------------------------------- gh identity ----

@contextlib.contextmanager
def gh_identity(provider: Callable[[], str] | None) -> Iterator[None]:
    """Run as a GitHub App installation for the duration of this block.

    A provider, not a token: `gh` and `git` are invoked throughout a landing
    that may run for half an hour, and an installation token lives one hour,
    so the value must be asked for per subprocess. A freshly spawned thread
    starts with an empty context, so two workers can never see each other's
    identity."""
    tok = _identity.set(provider)
    try:
        yield
    finally:
        _identity.reset(tok)


def _gh_env() -> dict | None:
    """Environment for GitHub-touching subprocesses, or None when no identity
    is bound — which is the poller's behaviour, unchanged. Built here so a
    caller cannot clobber PATH on the way past."""
    provider = _identity.get()
    if provider is None:
        return None
    return {**os.environ, "GH_TOKEN": provider(),
            "GH_NO_UPDATE_NOTIFIER": "1"}


# ------------------------------------------------------------- gh seams ----

def _gh(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True,
                          timeout=timeout, env=_gh_env())


def _gh_ok(r: subprocess.CompletedProcess | None, what: str) -> bool:
    """True when the call worked; otherwise say so on stderr.

    Every write-side caller used to discard the return code. A PAT's blanket
    `repo` scope hides a missing permission; an App's per-resource grant does
    not, and the symptom is a verdict that is simply never reported."""
    if r is None or getattr(r, "returncode", 0) == 0:
        return True
    err = redact((getattr(r, "stderr", "") or "").strip())[:150]
    print(f"  ! {what} failed: {err}", file=sys.stderr)
    return False


def _list_prs(slug: str) -> list[dict]:
    # 200, not 50: under events this is the catch-up sweep that backstops
    # deliveries dropped while the gateway was down, and a truncated sweep
    # silently never reaches the oldest open PRs.
    r = _gh("pr", "list", "--repo", slug, "--state", "open", "--limit", "200",
            "--json", "number,title,headRefOid,isCrossRepository")
    if r.returncode != 0:
        raise RuntimeError(f"gh pr list failed: {redact(r.stderr).strip()[:150]}")
    return json.loads(r.stdout or "[]")


def _pr_view(slug: str, number: int) -> dict | None:
    """One PR as it is right now, in `_list_prs`'s exact shape, or None if it
    is closed or gone. This is what makes a delivery a trigger rather than
    data: out-of-order deliveries and force-pushes both name the same current
    head, and the loser collapses into a no-op instead of landing work the
    author already replaced. It collapses them only while they are still
    queued — a landing a worker already started is not cancelled."""
    r = _gh("pr", "view", str(number), "--repo", slug,
            "--json", "number,title,headRefOid,state")
    if r.returncode != 0:
        return None
    try:
        d = json.loads(r.stdout or "{}")
    except ValueError:
        return None
    if not d.get("headRefOid") or d.get("state") not in (None, "OPEN"):
        return None
    return {"number": d.get("number", number), "title": d.get("title", ""),
            "headRefOid": d["headRefOid"]}


def _fetch_pr_head(repo: str, slug: str, number: int, sha: str) -> bool:
    for attempt in range(len(_FETCH_BACKOFF_S)):
        code, _, _ = git_rc(repo, "rev-parse", "--verify", f"{sha}^{{commit}}")
        if code == 0:
            return True
        env = _gh_env()
        # Credential bridge only under App identity: the poller runs on the
        # maintainer's own git credentials and must keep doing so.
        cred = GIT_CREDENTIAL_ARGS if env else ()
        code, _, _ = git_rc(repo, *cred, "fetch", "--quiet",
                            f"https://github.com/{slug}",
                            f"refs/pull/{number}/head", env=env)
        if code == 0:
            code, _, _ = git_rc(repo, "rev-parse", "--verify",
                                f"{sha}^{{commit}}")
            if code == 0:
                return True
        if attempt + 1 < len(_FETCH_BACKOFF_S) and _FETCH_BACKOFF_S[attempt]:
            time.sleep(_FETCH_BACKOFF_S[attempt])
    return False


def _comment(slug: str, number: int, body: str) -> bool:
    return _gh_ok(_gh("pr", "comment", str(number), "--repo", slug,
                      "--body", body), f"comment on #{number}")


def _label(slug: str, number: int, verdict: str) -> bool:
    for name in LABELS:
        _gh("pr", "edit", str(number), "--repo", slug,
            "--remove-label", f"cafecito:{name}")
    return _gh_ok(_gh("pr", "edit", str(number), "--repo", slug,
                      "--add-label", f"cafecito:{verdict}"),
                  f"label #{number} cafecito:{verdict}")


def _ensure_labels(slug: str) -> bool:
    ok = True
    for name, (color, desc) in LABELS.items():
        ok = _gh_ok(_gh("label", "create", f"cafecito:{name}", "--repo", slug,
                        "--color", color, "--description", desc, "--force"),
                    f"create label cafecito:{name}") and ok
    return ok


# ---------------------------------------------------------------- state ----

class IngestState:
    """Which (PR, head) pairs have been worked, which are being worked, and
    which hit something transient and are owed another attempt (RETRYABLE).

    The polling design stored one head per PR and checked it before acting;
    both break under events. It remembers a bounded set of heads instead of
    the last one, and `claim` is an atomic test-and-set under the engine's own
    flock, so the record happens BEFORE the landing rather than after it: a
    crash between the two used to mean a redelivery re-submitted work that had
    already landed. Re-reading inside the lock is what kills the lost update
    when two workers hold two instances."""

    HEADS_KEPT = 20

    def __init__(self, state_dir: pathlib.Path, stale_s: float = 1800.0):
        self.dir = pathlib.Path(state_dir)
        self.path = self.dir / "ingest.json"
        self.stale_s = stale_s
        self.lock_path = self.dir / "lock"

    def _locked(self):
        """A FRESH lock object per acquisition — flock is per open file
        description, and one shared object would have two threads writing the
        same `fd` attribute over each other."""
        return _FileLock(self.lock_path)

    # -- storage ----------------------------------------------------------

    def _read(self) -> dict:
        try:
            d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            d = {}
        return d if isinstance(d, dict) else {}

    def _write(self, d: dict) -> None:
        self.path.write_text(json.dumps(d, indent=1))

    def _entry(self, d: dict, number: int) -> dict:
        """This PR's record, migrating the pre-gateway one-head format."""
        e = d.get(str(number))
        if not isinstance(e, dict):
            return {"heads": {}, "last": None}
        if "heads" not in e:
            head = e.get("head")
            e = {"heads": ({head: {"state": e.get("verdict", "landed"),
                                   "at": e.get("at", 0)}} if head else {}),
                 "last": head}
        return e

    def _state_of(self, entry: dict, head: str) -> str | None:
        rec = (entry.get("heads") or {}).get(head)
        if not isinstance(rec, dict):
            return None
        state = rec.get("state")
        # A worker that was SIGKILLed mid-landing leaves an in_progress entry
        # nobody will ever finalize; the engine's own horizon for a dead
        # in-flight submission is the same number.
        if state == "in_progress" \
                and time.time() - rec.get("at", 0) > self.stale_s:
            return None
        # A transient failure is not work that was done, so it never stands
        # between this head and the next attempt.
        if state in RETRYABLE:
            return None
        return state

    def _put(self, number: int, head: str, state: str,
             told: bool | None = None) -> None:
        d = self._read()
        e = self._entry(d, number)
        heads = dict(e.get("heads") or {})
        prev = (e.get("heads") or {}).get(head)
        rec: dict = {"state": state, "at": time.time()}
        # `told` outlives the state it was set on: a retryable record is
        # overwritten by the very next claim, and losing the flag there would
        # mean re-commenting on every retry.
        keep = (prev or {}).get("told") if told is None else told
        if keep:
            rec["told"] = True
        heads[head] = rec
        if len(heads) > self.HEADS_KEPT:
            for k in sorted(heads, key=lambda h: heads[h].get("at", 0)
                            )[:len(heads) - self.HEADS_KEPT]:
                heads.pop(k)
        d[str(number)] = {"heads": heads, "last": head, "at": time.time()}
        self._write(d)

    # -- api --------------------------------------------------------------

    def claim(self, number: int, head: str) -> bool:
        """True exactly once per (PR, head), across threads and processes."""
        with self._locked():
            if self._state_of(self._entry(self._read(), number), head):
                return False
            self._put(number, head, "in_progress")
            return True

    def finalize(self, number: int, head: str, verdict: str) -> None:
        with self._locked():
            self._put(number, head, verdict)

    def release(self, number: int, head: str) -> None:
        """Un-claim work that died, so it is retryable now instead of after
        the stale horizon."""
        with self._locked():
            d = self._read()
            e = self._entry(d, number)
            heads = dict(e.get("heads") or {})
            if heads.pop(head, None) is not None:
                d[str(number)] = {"heads": heads,
                                  "last": e.get("last"), "at": time.time()}
                self._write(d)

    def seen(self, number: int, head: str) -> bool:
        with self._locked():
            return self._state_of(self._entry(self._read(), number),
                                  head) is not None

    def record(self, number: int, head: str, verdict: str) -> None:
        self.finalize(number, head, verdict)

    def record_transient(self, number: int, head: str, state: str) -> bool:
        """Record a RETRYABLE non-verdict. True the first time this (PR, head)
        hits one, so the author is told once and the retries stay quiet."""
        with self._locked():
            entry = self._entry(self._read(), number)
            rec = (entry.get("heads") or {}).get(head) or {}
            told = bool(rec.get("told"))
            self._put(number, head, state, told=True)
            return not told


# ----------------------------------------------------------------- loop ----

def _verdict_message(pr: dict, res: dict) -> str:
    v = res.get("verdict")
    if v == "landed":
        gate = res.get("gate") or {}
        regen = " (colliding regions were regenerated)" if res.get(
            "regenerated") else ""
        # The gate summary is the tail of a test command that RAN the
        # submitted head's code. It is the one string in this package that
        # reaches a public PR comment, so it is redacted like every other
        # emitted line — a token in the environment of a fork's test run
        # would otherwise be published by us.
        return (f"☕ **landed** on `cafecito/main` as `{res['tip'][:12]}`"
                f"{regen} — gate: "
                f"{redact(str(gate.get('summary', 'green')))}.\n\n"
                f"This PR's changes are integrated (engine-authored commit "
                f"with a `Changeset-Id` trailer); close when convenient.")
    if v == "escalated":
        return (f"☕ **escalated** — cafecito could not land this "
                f"automatically: {res.get('reason')}.\n\nFix and push; "
                f"the new head will be re-ingested.")
    return f"☕ **rejected**: {res.get('reason')}."


def ingest_pr(engine: Engine, slug: str, state: IngestState, pr: dict,
              report: bool = True,
              claimed: bool = False) -> tuple[int, str] | None:
    """Land one PR. Returns (number, verdict), or None if nothing to do.

    `pr` is `{number, title, headRefOid}` — the shape `_list_prs` projects,
    the shape `_pr_view` returns, and a subset of the webhook payload, so all
    three producers feed this one consumer with no adapter. `claimed` says
    the caller already took the (PR, head) claim and will finalize it."""
    number, head = pr["number"], pr["headRefOid"]
    if not claimed and state.seen(number, head):
        return None
    if not _fetch_pr_head(engine.repo, slug, number, head):
        # GitHub was unreachable or the ref had not appeared — nothing about
        # the submitted code is wrong, so this must not be recorded as a
        # verdict. A terminal record here would make the head unlandable
        # forever: the claim answers "already worked" and the sweep, which is
        # the only retry that exists, would never get past it.
        first = state.record_transient(number, head, "unfetched")
        if report and first:
            _comment(slug, number, "☕ **could not fetch** this PR's head — "
                                   "nothing was submitted. cafecito retries "
                                   "on its own; no action needed unless this "
                                   "PR stays silent.")
        return (number, "unfetched")
    res = engine.submit(head, agent=f"pr/{number}",
                        title=pr.get("title", f"PR #{number}"))
    verdict = res.get("verdict", "rejected")
    reason = str(res.get("reason") or "")
    # The engine recognising redelivered work is not a rejection worth
    # alarming a human about: it means this head is already landed.
    if verdict == "rejected" and reason == "changeset already in tip":
        state.record(number, head, "landed")
        return (number, "landed")
    # Admission timeout is contention, not a judgement: an overlapping
    # changeset held the in-flight registration past the window. Same
    # reasoning as an unfetchable head — retryable, and never a comment.
    if verdict == "rejected" and reason.startswith(_ADMISSION_TIMEOUT):
        state.record_transient(number, head, "contended")
        return (number, "contended")
    state.record(number, head, verdict)
    if report:
        _comment(slug, number, _verdict_message(pr, res))
        _label(slug, number, verdict if verdict in LABELS else "rejected")
    return (number, verdict)


def ingest_once(engine: Engine, slug: str, state: IngestState,
                report: bool = True) -> list[tuple[int, str]]:
    """One poll cycle. Returns [(pr_number, verdict), ...] for acted PRs."""
    return [r for r in (ingest_pr(engine, slug, state, pr, report)
                        for pr in _list_prs(slug)) if r]


def run_ingest(args) -> int:
    engine = Engine(args.repo)
    slug = getattr(args, "github", None) or slug_from_origin(engine.repo)
    if not slug:
        print("no GitHub repo: pass --github owner/repo or set an origin remote")
        return 2
    state = IngestState(engine.state_dir,
                        stale_s=engine.config["gate_timeout_s"] * 2)
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
