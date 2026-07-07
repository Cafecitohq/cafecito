import subprocess

import pytest

from gate import impact_tests


def _make_repo(tmp_path):
    repo = str(tmp_path)
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text("x = 1\n")

    tests = pkg / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    (tests / "test_mod.py").write_text("def test_x(): pass\n")

    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    )
    return repo, result.stdout.strip()


# ---------------------------------------------------------------------------
# impact_tests
# ---------------------------------------------------------------------------

def test_source_file_maps_to_sibling_test(tmp_path):
    repo, rev = _make_repo(tmp_path)
    result = impact_tests(repo, {"pkg/mod.py"}, rev)
    assert result == {"pkg/tests/test_mod.py"}


def test_test_file_maps_to_itself(tmp_path):
    repo, rev = _make_repo(tmp_path)
    result = impact_tests(repo, {"pkg/tests/test_mod.py"}, rev)
    assert result == {"pkg/tests/test_mod.py"}


def test_non_py_file_produces_nothing(tmp_path):
    repo, rev = _make_repo(tmp_path)
    result = impact_tests(repo, {"README.md"}, rev)
    assert result == set()


def test_source_without_sibling_produces_nothing(tmp_path):
    repo, rev = _make_repo(tmp_path)
    result = impact_tests(repo, {"pkg/__init__.py"}, rev)
    assert result == set()


def test_mixed_paths_combined_correctly(tmp_path):
    repo, rev = _make_repo(tmp_path)
    result = impact_tests(repo, {"pkg/mod.py", "pkg/tests/test_mod.py", "README.md"}, rev)
    assert result == {"pkg/tests/test_mod.py"}
