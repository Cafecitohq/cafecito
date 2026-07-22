"""Applying the plane to a project: detection, MCP registration, tip hook.

These are the paths a new operator hits in their first thirty seconds, and
every failure mode here is silent — a gate that collects nothing, a plane no
agent session can see, a tip that stops following main."""

import json
import os
import subprocess

import pytest

from cafecito.onboard import (detect_project, hook_installed,
                              install_post_commit_hook, mcp_registered,
                              write_mcp_registration)


@pytest.fixture()
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    return tmp_path


def write(root, rel, text=""):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_detects_python_project(repo):
    write(repo, "pyproject.toml", "[project]\nname='x'\n")
    write(repo, "tests/test_a.py", "def test_a(): pass\n")
    write(repo, "tests/test_b.py", "def test_b(): pass\n")
    d = detect_project(str(repo))
    assert d["language"] == "py"
    assert d["test_cmd"][:3] == ["python3", "-m", "pytest"]
    assert d["gate_families"] == ["py"]
    assert d["test_files"] == 2


def test_detects_js_project_with_lockfile(repo):
    write(repo, "package.json",
          json.dumps({"scripts": {"test": "vitest run"}}))
    write(repo, "package-lock.json", "{}")
    write(repo, "src/app.test.ts", "")
    d = detect_project(str(repo))
    assert d["language"] == "js"
    assert d["test_cmd"] == ["npm", "test", "--silent"]
    assert d["setup_cmd"] == ["npm", "ci"]
    assert d["generated"] == {
        "package-lock.json": ["npm", "install", "--package-lock-only"]}


def test_detects_js_app_in_subdirectory(repo):
    """The app-in-a-subdir layout: commands must carry --prefix or the gate
    runs npm in a directory with no package.json."""
    write(repo, "app/package.json",
          json.dumps({"scripts": {"test": "vitest run"}}))
    write(repo, "app/package-lock.json", "{}")
    write(repo, "app/src/x.spec.tsx", "")
    d = detect_project(str(repo))
    assert d["language"] == "js"
    assert d["test_cmd"] == ["npm", "test", "--silent", "--prefix", "app"]
    assert d["setup_cmd"] == ["npm", "ci", "--prefix", "app"]
    assert "app/package-lock.json" in d["generated"]


def test_placeholder_test_script_is_not_evidence(repo):
    """npm init writes a test script that only prints an error — treating it
    as a gate would land everything on a command that always fails."""
    write(repo, "package.json", json.dumps(
        {"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}))
    write(repo, "pyproject.toml", "[project]\nname='x'\n")
    d = detect_project(str(repo))
    assert d["language"] == "py"


def test_go_and_test_file_counts_win_over_manifests(repo):
    write(repo, "go.mod", "module x\n")
    write(repo, "pyproject.toml", "[project]\nname='x'\n")
    for i in range(3):
        write(repo, f"pkg/a{i}_test.go", "package pkg\n")
    d = detect_project(str(repo))
    assert d["language"] == "go"
    assert d["test_cmd"] == ["go", "test", "./..."]
    assert "py" in d["also_found"]


def test_dependency_directories_are_not_scanned(repo):
    write(repo, "pyproject.toml", "[project]\nname='x'\n")
    write(repo, "tests/test_real.py", "")
    for i in range(5):
        write(repo, f"node_modules/dep/test_fake{i}.py", "")
        write(repo, f".venv/lib/test_fake{i}.py", "")
    d = detect_project(str(repo))
    assert d["test_files"] == 1


def test_empty_repo_detects_nothing(repo):
    d = detect_project(str(repo))
    assert d["language"] is None and d["test_cmd"] is None


def test_mcp_registration_created_and_idempotent(repo):
    verdict, path = write_mcp_registration(str(repo))
    assert verdict == "created"
    data = json.loads((repo / ".mcp.json").read_text())
    assert data["mcpServers"]["cafecito"] == {
        "command": "cafecito", "args": ["serve", "--repo", "."]}
    assert mcp_registered(str(repo))
    assert write_mcp_registration(str(repo))[0] == "present"


def test_mcp_registration_preserves_other_servers(repo):
    (repo / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"other": {"command": "x"}}}))
    verdict, _ = write_mcp_registration(str(repo))
    assert verdict == "updated"
    servers = json.loads((repo / ".mcp.json").read_text())["mcpServers"]
    assert servers["other"] == {"command": "x"}
    assert servers["cafecito"]["args"] == ["serve", "--repo", "."]


def test_mcp_registration_refuses_to_clobber_broken_json(repo):
    (repo / ".mcp.json").write_text("{not json")
    assert write_mcp_registration(str(repo))[0] == "conflict"
    assert (repo / ".mcp.json").read_text() == "{not json"


def test_hook_installed_executable_and_idempotent(repo):
    verdict, path = install_post_commit_hook(str(repo))
    assert verdict == "installed"
    assert os.access(path, os.X_OK)
    assert hook_installed(str(repo))
    assert install_post_commit_hook(str(repo))[0] == "present"


def test_hook_does_not_overwrite_an_existing_hook(repo):
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "post-commit").write_text("#!/bin/sh\necho mine\n")
    verdict, detail = install_post_commit_hook(str(repo))
    assert verdict == "conflict" and "cafecito advance" in detail
    assert (hooks / "post-commit").read_text() == "#!/bin/sh\necho mine\n"


def test_hook_advances_the_tip_on_a_real_commit(repo):
    """The whole point: a commit made without the plane still moves the tip."""
    from cafecito.engine import Engine
    write(repo, "a.py", "x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "-m", "first"], cwd=repo, check=True)
    eng = Engine(str(repo))
    start = eng._tip()
    install_post_commit_hook(str(repo))

    hook = repo / ".git" / "hooks" / "post-commit"
    # The installed hook shells out to a `cafecito` on PATH; in-tree tests
    # exercise the same contract through the module entry point.
    hook.write_text(hook.read_text().replace(
        "cafecito advance", "python3 -m cafecito.cli advance"))
    env = dict(os.environ, PATH=os.environ.get("PATH", ""),
               PYTHONPATH=os.pathsep.join(
                   [os.getcwd(), os.environ.get("PYTHONPATH", "")]))
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "--allow-empty", "-m", "out of band"],
                   cwd=repo, check=True, env=env)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    assert head != start
    assert Engine(str(repo))._tip() == head


def test_hook_stays_out_of_the_way_on_feature_branches(repo):
    from cafecito.engine import Engine
    write(repo, "a.py", "x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "-m", "first"], cwd=repo, check=True)
    eng = Engine(str(repo))
    tip = eng._tip()
    install_post_commit_hook(str(repo))
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.write_text(hook.read_text().replace(
        "cafecito advance", "python3 -m cafecito.cli advance"))

    subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=repo, check=True)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        [os.getcwd(), os.environ.get("PYTHONPATH", "")]))
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "--allow-empty", "-m", "wip"],
                   cwd=repo, check=True, env=env)
    assert Engine(str(repo))._tip() == tip
