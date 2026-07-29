import json
import subprocess
import sys
import time
import types

import pytest

from cafecito import ingest
from cafecito.engine import Engine
from cafecito.ingest import IngestState, ingest_once, slug_from_origin

SLUG = "acme/widget"


@pytest.fixture()
def repo(tmp_path):
    def sh(*args):
        subprocess.run(args, cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / "mod.py").write_text("x = 1\n")
    sh("git", "init", "-q", "-b", "main")
    sh("git", "add", "-A")
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "i")
    return tmp_path


def pr_branch(repo, name, content):
    def sh(*args):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)

    sh("git", "checkout", "-q", "-b", name, "main")
    (repo / "mod.py").write_text(content)
    sh("git", "add", "-A")
    sh("git", "-c", "user.name=pr", "-c", "user.email=p@p", "commit", "-q", "-m", name)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    sh("git", "checkout", "-q", "main")
    return head


def make_engine(repo):
    eng = Engine(str(repo))
    eng.config["test_cmd"] = [sys.executable, "-c", "pass"]
    return eng


def test_slug_from_origin(repo):
    subprocess.run(["git", "remote", "add", "origin",
                    "https://github.com/cafecitohq/cafecito.git"],
                   cwd=repo, check=True, capture_output=True)
    assert slug_from_origin(str(repo)) == "cafecitohq/cafecito"


def test_slug_from_ssh_origin(repo):
    subprocess.run(["git", "remote", "add", "origin",
                    "git@github.com:owner/thing.git"],
                   cwd=repo, check=True, capture_output=True)
    assert slug_from_origin(str(repo)) == "owner/thing"


def test_ingest_lands_comments_and_skips_seen(repo, monkeypatch):
    eng = make_engine(repo)
    head = pr_branch(repo, "feature", "x = 2\n")
    comments, labels = [], []
    monkeypatch.setattr(ingest, "_list_prs", lambda slug: [
        {"number": 7, "title": "bump x", "headRefOid": head}])
    monkeypatch.setattr(ingest, "_comment",
                        lambda s, n, b: comments.append((n, b)))
    monkeypatch.setattr(ingest, "_label",
                        lambda s, n, v: labels.append((n, v)))
    state = IngestState(eng.state_dir)

    acted = ingest_once(eng, "o/r", state)
    assert acted == [(7, "landed")]
    assert labels == [(7, "landed")]
    assert "landed" in comments[0][1] and "Changeset-Id" in comments[0][1]
    assert eng.status()["landed"] == 1

    # same head again → skipped entirely
    assert ingest_once(eng, "o/r", state) == []
    assert eng.status()["landed"] == 1


def test_repushed_head_is_reingested(repo, monkeypatch):
    eng = make_engine(repo)
    h1 = pr_branch(repo, "f1", "x = 2\n")
    # the re-push must NOT collide with h1's landing: an offline suite may
    # never reach the real reconciler (CI proved the original fixture did)
    (repo / "other.py").write_text("y = 0\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "-m", "add other"], cwd=repo, check=True,
                   capture_output=True)
    def sh(*args):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    sh("git", "checkout", "-q", "-b", "f2", "main")
    (repo / "other.py").write_text("y = 1\n")
    sh("git", "add", "-A")
    sh("git", "-c", "user.name=pr", "-c", "user.email=p@p", "commit", "-q",
       "-m", "f2")
    h2 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                        capture_output=True, text=True).stdout.strip()
    sh("git", "checkout", "-q", "main")
    heads = {"h": h1}
    monkeypatch.setattr(ingest, "_list_prs", lambda slug: [
        {"number": 9, "title": "evolving pr", "headRefOid": heads["h"]}])
    monkeypatch.setattr(ingest, "_comment", lambda s, n, b: None)
    monkeypatch.setattr(ingest, "_label", lambda s, n, v: None)
    state = IngestState(eng.state_dir)

    assert ingest_once(eng, "o/r", state) == [(9, "landed")]
    heads["h"] = h2
    acted = ingest_once(eng, "o/r", state)
    assert len(acted) == 1 and acted[0][0] == 9   # re-ingested on new head


def test_unfetchable_head_is_retried_and_commented_once(repo, monkeypatch):
    """An unfetchable head used to be recorded as `rejected`, which is a
    verdict — and a verdict is permanent: `claim` answers "already worked"
    forever, GitHub never redelivers, and the catch-up sweep is the only
    retry that exists. GitHub being briefly unreachable must not make a PR
    unlandable, so the record stays retryable and the comment happens once."""
    eng = make_engine(repo)
    ghost = "0" * 40
    monkeypatch.setattr(ingest, "_list_prs", lambda slug: [
        {"number": 3, "title": "ghost", "headRefOid": ghost}])
    monkeypatch.setattr(ingest, "_fetch_pr_head",
                        lambda repo, slug, n, sha: False)
    notes = []
    monkeypatch.setattr(ingest, "_comment", lambda s, n, b: notes.append(b))
    monkeypatch.setattr(ingest, "_label", lambda s, n, v: None)
    state = IngestState(eng.state_dir)
    assert ingest_once(eng, "o/r", state) == [(3, "unfetched")]
    assert "could not fetch" in notes[0]
    assert ingest_once(eng, "o/r", state) == [(3, "unfetched")]
    assert len(notes) == 1              # told once, not once per poll
    assert state.seen(3, ghost) is False


# ----------------------------------------------------------- ingest seams ---
# `ingest_pr` is the seam the gateway drives; these pin it, and the claim
# store underneath it, without a poll cycle in sight.

def test_ingest_pr_lands_a_hand_built_pr(repo, monkeypatch):
    """The seam takes the dict directly: no _list_prs involved at all."""
    eng = make_engine(repo)
    head = pr_branch(repo, "feature", "x = 2\n")
    monkeypatch.setattr(ingest, "_comment", lambda s, n, b: None)
    monkeypatch.setattr(ingest, "_label", lambda s, n, v: None)
    state = IngestState(eng.state_dir)
    pr = {"number": 5, "title": "bump x", "headRefOid": head}
    assert ingest.ingest_pr(eng, SLUG, state, pr) == (5, "landed")
    assert ingest.ingest_pr(eng, SLUG, state, pr) is None


def test_a_head_that_arrives_late_is_not_a_rejection(repo, monkeypatch):
    """A synchronize delivery can beat GitHub's update of refs/pull/N/head."""
    eng = make_engine(repo)
    head = pr_branch(repo, "feature", "x = 2\n")
    monkeypatch.setattr(ingest, "_FETCH_BACKOFF_S", (0, 0, 0))
    tries = {"rev-parse": 0, "fetch": 0}
    real = ingest.git_rc

    def flaky(repo_path, *args, env=None):
        if "fetch" in args:
            tries["fetch"] += 1
            return 0, "", ""            # offline: pretend the ref arrived
        if args[:1] == ("rev-parse",):
            tries["rev-parse"] += 1
            if tries["rev-parse"] == 1:
                return 1, "", "unknown revision"
        return real(repo_path, *args, env=env)

    monkeypatch.setattr(ingest, "git_rc", flaky)
    notes = []
    monkeypatch.setattr(ingest, "_comment", lambda s, n, b: notes.append(b))
    monkeypatch.setattr(ingest, "_label", lambda s, n, v: None)
    state = IngestState(eng.state_dir)
    acted = ingest.ingest_pr(eng, SLUG, state,
                             {"number": 6, "title": "t", "headRefOid": head})
    assert acted == (6, "landed"), acted
    assert tries["fetch"] == 1
    assert not any("could not fetch" in n for n in notes)


def test_the_landed_comment_is_redacted(repo, monkeypatch):
    """The gate summary is the tail of a command that RAN the submitted head's
    code, and this comment is the only thing in the package that reaches a
    public PR. It goes through redact() like every other emitted line."""
    eng = make_engine(repo)
    monkeypatch.setattr(ingest, "_fetch_pr_head",
                        lambda repo, slug, n, sha: True)
    tok = "ghs_" + "H" * 30
    body = ingest._verdict_message(
        {"number": 2}, {"verdict": "landed", "tip": "a" * 40,
                        "gate": {"summary": f"1 passed (token {tok})"}})
    assert tok not in body and "***" in body


def test_a_claim_is_taken_once_and_can_be_released(repo):
    eng = Engine(str(repo))
    state = IngestState(eng.state_dir, stale_s=60)
    assert state.claim(7, "a" * 40) is True
    assert state.claim(7, "a" * 40) is False
    assert state.seen(7, "a" * 40) is True
    state.release(7, "a" * 40)
    assert state.claim(7, "a" * 40) is True
    # a worker SIGKILLed mid-landing must not strand the head forever
    d = json.loads((eng.state_dir / "ingest.json").read_text())
    d["7"]["heads"]["a" * 40]["at"] = time.time() - 61
    (eng.state_dir / "ingest.json").write_text(json.dumps(d))
    assert state.claim(7, "a" * 40) is True
    state.finalize(7, "a" * 40, "landed")
    assert state.claim(7, "a" * 40) is False


def test_a_transient_record_never_blocks_the_next_attempt(repo):
    """Only a verdict is terminal. A RETRYABLE record survives just to
    remember that the author was already told."""
    eng = Engine(str(repo))
    state = IngestState(eng.state_dir, stale_s=1800)
    assert state.record_transient(3, "c" * 40, "unfetched") is True
    assert state.record_transient(3, "c" * 40, "unfetched") is False
    assert state.seen(3, "c" * 40) is False
    assert state.claim(3, "c" * 40) is True          # claimable immediately
    state.finalize(3, "c" * 40, "unfetched")
    assert state.record_transient(3, "c" * 40, "unfetched") is False
    state.finalize(3, "c" * 40, "landed")
    assert state.seen(3, "c" * 40) is True


def test_the_pre_gateway_state_format_is_migrated_in_place(repo):
    eng = Engine(str(repo))
    (eng.state_dir / "ingest.json").write_text(json.dumps(
        {"9": {"head": "c" * 40, "verdict": "escalated", "at": time.time()}}))
    state = IngestState(eng.state_dir)
    assert state.seen(9, "c" * 40) is True
    assert state.seen(9, "d" * 40) is False
    state.record(9, "d" * 40, "landed")
    d = json.loads((eng.state_dir / "ingest.json").read_text())
    assert d["9"]["heads"]["c" * 40]["state"] == "escalated"
    assert d["9"]["last"] == "d" * 40


def test_remembered_heads_are_bounded(repo):
    eng = Engine(str(repo))
    state = IngestState(eng.state_dir)
    for i in range(30):
        state.record(3, f"{i:040d}", "landed")
    d = json.loads((eng.state_dir / "ingest.json").read_text())
    assert len(d["3"]["heads"]) == IngestState.HEADS_KEPT
    assert d["3"]["last"] == f"{29:040d}"


def test_a_failed_report_is_said_out_loud(capsys):
    """A PAT's blanket `repo` scope hides a missing permission; an App's
    per-resource grant does not, and the symptom was a silent no-comment."""
    r = types.SimpleNamespace(returncode=1,
                              stderr="HTTP 403: Resource not accessible "
                                     "by integration (ghs_" + "G" * 30 + ")")
    assert ingest._gh_ok(r, "comment on #7") is False
    err = capsys.readouterr().err
    assert "comment on #7 failed" in err and "403" in err
    assert "ghs_G" not in err
    assert ingest._gh_ok(types.SimpleNamespace(returncode=0, stderr=""),
                         "comment on #7") is True
