import subprocess
import sys

import pytest

from cafecito.engine import Engine


@pytest.fixture()
def repo(tmp_path):
    def sh(*args):
        subprocess.run(args, cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / "a.py").write_text("x = 1\n")
    sh("git", "init", "-q", "-b", "main")
    sh("git", "add", "-A")
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "i")
    return tmp_path


def commit(repo, msg):
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "--allow-empty", "-m", msg],
                   cwd=repo, check=True, capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()


def test_advance_to_descendant(repo):
    eng = Engine(str(repo))
    new = commit(repo, "out-of-band docs")
    r = eng.advance(new)
    assert r == {"verdict": "advanced", "tip": new}
    assert eng.status()["tip"] == new
    assert eng._log_entries(1)[0]["verdict"] == "advanced"


def test_advance_noop_and_rejections(repo):
    eng = Engine(str(repo))
    tip = eng.status()["tip"]
    assert eng.advance(tip)["verdict"] == "noop"
    assert eng.advance("nonsense")["verdict"] == "rejected"
    # a sibling commit that does NOT contain the tip is refused
    subprocess.run(["git", "checkout", "-q", "--orphan", "stray"], cwd=repo,
                   check=True, capture_output=True)
    stray = commit(repo, "unrelated")
    r = eng.advance(stray)
    assert r["verdict"] == "rejected" and "does not contain" in r["reason"]
