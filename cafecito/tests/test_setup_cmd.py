import json
import subprocess
import sys

import pytest

from cafecito.engine import Engine


@pytest.fixture()
def repo(tmp_path):
    def sh(*args):
        subprocess.run(args, cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / "tests").mkdir()
    (tmp_path / "mod.py").write_text("x = 1\n")
    (tmp_path / "tests" / "test_mod.py").write_text(
        "import pathlib\n\n\ndef test_setup_ran():\n"
        "    assert pathlib.Path('SETUP_MARKER').exists()\n"
        "    from mod import x\n    assert x == 1\n")
    (tmp_path / "conftest.py").write_text(
        "import pathlib, sys\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).parent))\n")
    sh("git", "init", "-q", "-b", "main")
    sh("git", "add", "-A")
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "i")
    return tmp_path


def branch_touch(repo, name):
    def sh(*args):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)

    sh("git", "checkout", "-q", "-b", name, "main")
    (repo / "mod.py").write_text(f"x = 1  # {name}\n")
    sh("git", "add", "-A")
    sh("git", "-c", "user.name=a", "-c", "user.email=a@a", "commit", "-q", "-m", name)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    sh("git", "checkout", "-q", "main")
    return head


def make_engine(repo, setup):
    eng = Engine(str(repo))
    eng.config["test_cmd"] = [sys.executable, "-m", "pytest", "-q",
                              "-p", "no:cacheprovider"]
    eng.config["setup_cmd"] = setup
    return eng


def test_setup_runs_in_worktree_before_tests(repo):
    # the fixture's test REQUIRES the marker the setup step creates
    eng = make_engine(repo, [sys.executable, "-c",
                             "open('SETUP_MARKER','w').write('ok')"])
    head = branch_touch(repo, "wa")
    res = eng.submit(head, agent="a", title="touch")
    assert res["verdict"] == "landed", res


def test_setup_failure_reddens_gate(repo):
    eng = make_engine(repo, [sys.executable, "-c", "raise SystemExit(3)"])
    head = branch_touch(repo, "wa")
    res = eng.submit(head, agent="a", title="touch")
    assert res["verdict"] == "escalated"
    assert "setup failed" in res["gate"]["summary"]


def test_no_setup_means_old_behavior(repo):
    eng = make_engine(repo, [])
    (repo / "tests" / "test_mod.py").write_text(
        "from mod import x\n\n\ndef test_x():\n    assert x == 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "-m", "simplify"], cwd=repo, check=True,
                   capture_output=True)
    eng.advance("HEAD")
    head = branch_touch(repo, "wb")
    assert eng.submit(head, agent="b", title="touch")["verdict"] == "landed"
