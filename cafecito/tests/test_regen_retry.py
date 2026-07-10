import subprocess
import sys

import pytest

from cafecito import engine as engine_mod
from cafecito.engine import Engine

MOD_BASE = "def greet(name):\n    return f'hi {name}'\n"
TEST = ("from mod import greet\n\n\n"
        "def test_greet():\n    assert greet('x') == 'HI x'\n")


@pytest.fixture()
def repo(tmp_path):
    def sh(*args):
        subprocess.run(args, cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / "tests").mkdir()
    (tmp_path / "mod.py").write_text(MOD_BASE)
    (tmp_path / "tests" / "test_mod.py").write_text(
        "from mod import greet\n\n\ndef test_greet():\n"
        "    assert greet('x').lower().endswith('x')\n")
    (tmp_path / "conftest.py").write_text(
        "import pathlib, sys\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).parent))\n")
    sh("git", "init", "-q", "-b", "main")
    sh("git", "add", "-A")
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "i")
    return tmp_path


def branch(repo, name, mod_content, msg):
    def sh(*args):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)

    sh("git", "checkout", "-q", "-b", name, "main")
    (repo / "mod.py").write_text(mod_content)
    sh("git", "add", "-A")
    sh("git", "-c", "user.name=a", "-c", "user.email=a@a", "commit", "-q", "-m", msg)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    sh("git", "checkout", "-q", "main")
    return head


GOOD = "def greet(name):\n    return f'HI {name}!'[:-1]\n"
BAD = "def greet(name):\n    return 'broken'\n"


def test_regen_gate_failure_retries_with_feedback(repo, monkeypatch):
    eng = Engine(str(repo))
    eng.config["test_cmd"] = [sys.executable, "-m", "pytest", "-q",
                              "-p", "no:cacheprovider"]
    a = branch(repo, "wa", "def greet(name):\n    return f'hi {name}'.upper()\n",
               "shout")
    b = branch(repo, "wb", "def greet(name):\n    return f'hi {name}'.title()\n",
               "titlecase")
    assert eng.submit(a, agent="a", title="shout")["verdict"] == "landed"

    calls = []

    def fake_regen(repo_, base, tip, head, conflicted, landed, intent,
                   model="sonnet", feedback=""):
        calls.append(feedback)
        content = BAD if len(calls) == 1 else GOOD
        return ({p: content for p in conflicted}, 0.1), None

    monkeypatch.setattr(engine_mod, "live_regen", fake_regen)
    res = eng.submit(b, agent="b", title="titlecase")
    assert res["verdict"] == "landed", res
    assert len(calls) == 2
    assert calls[0] == ""                       # first attempt: no feedback
    assert "failed" in calls[1].lower()         # gate output fed back
    entry = eng._log_entries(1)[0]
    assert entry.get("regen_attempts") == 2


def test_regen_retries_zero_escalates_first_failure(repo, monkeypatch):
    eng = Engine(str(repo))
    eng.config["test_cmd"] = [sys.executable, "-m", "pytest", "-q",
                              "-p", "no:cacheprovider"]
    eng.config["regen_retries"] = 0
    a = branch(repo, "wa", "def greet(name):\n    return f'hi {name}'.upper()\n",
               "shout")
    b = branch(repo, "wb", "def greet(name):\n    return f'hi {name}'.title()\n",
               "titlecase")
    assert eng.submit(a, agent="a", title="shout")["verdict"] == "landed"

    calls = []

    def always_bad(repo_, base, tip, head, conflicted, landed, intent,
                   model="sonnet", feedback=""):
        calls.append(feedback)
        return ({p: BAD for p in conflicted}, 0.1), None

    monkeypatch.setattr(engine_mod, "live_regen", always_bad)
    res = eng.submit(b, agent="b", title="titlecase")
    assert res["verdict"] == "escalated"
    assert res["reason"] == "failed landing gate"
    assert len(calls) == 1
