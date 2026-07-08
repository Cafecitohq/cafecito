import subprocess
import sys

import pytest

from cafecito.closure import python_closure
from cafecito.engine import Engine
from cafecito.gate import blob_map

PKG_A = "def area(w, h):\n    return w * h\n"
TEST_A = "from pkg_a.mod import area\n\n\ndef test_area():\n    assert area(2, 3) == 6\n"
PKG_B = "def greet(n):\n    return f'hi {n}'\n"
TEST_B = "from pkg_b.mod import greet\n\n\ndef test_greet():\n    assert greet('x') == 'hi x'\n"


@pytest.fixture()
def repo(tmp_path):
    def sh(*args):
        subprocess.run(args, cwd=tmp_path, check=True, capture_output=True)

    for pkg, mod, test in (("pkg_a", PKG_A, TEST_A), ("pkg_b", PKG_B, TEST_B)):
        d = tmp_path / pkg
        (d / "tests").mkdir(parents=True)
        (d / "__init__.py").write_text("")
        (d / "mod.py").write_text(mod)
        (d / "tests" / "__init__.py").write_text("")
        (d / "tests" / f"test_{pkg}.py").write_text(test)
    (tmp_path / "conftest.py").write_text(
        "import pathlib, sys\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n")
    sh("git", "init", "-q", "-b", "main")
    sh("git", "add", "-A")
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "i")
    return tmp_path


def branch_edit(repo, name, path, content, msg):
    def sh(*args):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)

    sh("git", "checkout", "-q", "-b", name, "main")
    (repo / path).write_text(content)
    sh("git", "add", "-A")
    sh("git", "-c", "user.name=a", "-c", "user.email=a@a", "commit", "-q", "-m", msg)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    sh("git", "checkout", "-q", "main")
    return head


def test_closure_resolves_imports_and_conftest(repo):
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    listing = set(blob_map(str(repo), head))
    c = python_closure(str(repo), head, "pkg_a/tests/test_pkg_a.py", listing)
    assert c is not None
    assert "pkg_a/mod.py" in c            # the import
    assert "conftest.py" in c             # pytest injection
    assert "pkg_b/mod.py" not in c        # unrelated package excluded


def test_closure_confusion_returns_none(repo, tmp_path):
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    assert python_closure(str(repo), head, "missing.py",
                          set(blob_map(str(repo), head))) is None


def test_full_gate_memoizes_across_commuting_landings(repo):
    eng = Engine(str(repo))
    eng.config["test_cmd"] = [sys.executable, "-m", "pytest", "-q",
                              "-p", "no:cacheprovider"]
    eng.config["gate_mode"] = "full"

    a = branch_edit(repo, "wa", "pkg_a/mod.py",
                    "def area(w, h):\n    \"\"\"Area.\"\"\"\n    return w * h\n",
                    "docstring in pkg_a")
    ra = eng.submit(a, agent="a", title="pkg_a docstring")
    assert ra["verdict"] == "landed", ra
    assert ra["gate"]["memo"]["runs"] == 2          # cold: both suites execute

    b = branch_edit(repo, "wb", "pkg_b/mod.py",
                    "def greet(n):\n    \"\"\"Greet.\"\"\"\n    return f'hi {n}'\n",
                    "docstring in pkg_b")
    rb = eng.submit(b, agent="b", title="pkg_b docstring")
    assert rb["verdict"] == "landed", rb
    memo = rb["gate"]["memo"]
    # pkg_a's suite is untouched by this landing — its fact is inherited;
    # pkg_b's closure changed, so it executes. Quadratic → linear, live.
    assert memo["hits"] == 1 and memo["runs"] == 1, memo


def test_red_is_never_memoized(repo):
    eng = Engine(str(repo))
    eng.config["test_cmd"] = [sys.executable, "-m", "pytest", "-q",
                              "-p", "no:cacheprovider"]
    eng.config["gate_mode"] = "full"
    bad = branch_edit(repo, "wbad", "pkg_a/mod.py",
                      "def area(w, h):\n    return w + h\n", "break area")
    r1 = eng.submit(bad, agent="x", title="break area")
    assert r1["verdict"] == "escalated"
    r2 = eng.submit(bad, agent="x", title="break area again")
    assert r2["verdict"] == "escalated"
    assert r2["gate"]["memo"]["runs"] >= 1          # the red ran again
