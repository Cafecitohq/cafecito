import json
import subprocess
import sys
import types

import pytest

from cafecito import ingest
from cafecito.engine import Engine
from cafecito.ingest import IngestState, ingest_once, slug_from_origin


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


def test_unfetchable_head_rejected_once(repo, monkeypatch):
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
    assert ingest_once(eng, "o/r", state) == [(3, "rejected")]
    assert "could not fetch" in notes[0]
    assert ingest_once(eng, "o/r", state) == []   # not retried on same head
