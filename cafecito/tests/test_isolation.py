import shutil
import socket
import subprocess
import sys

import pytest

from cafecito import isolation
from cafecito.engine import Engine
from cafecito.facts import FactsStore
from cafecito.gate import run_gate

def _sandbox_runs() -> bool:
    """Functional probe, not just presence: sandbox-exec can be on PATH yet
    unable to run (e.g. inside another sandbox on some macOS versions)."""
    if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
        return False
    r = subprocess.run(["sandbox-exec", "-p", "(version 1)(allow default)",
                        "/usr/bin/true"], capture_output=True)
    return r.returncode == 0


needs_sandbox = pytest.mark.skipif(not _sandbox_runs(),
                                   reason="needs working macOS sandbox-exec")


def _network_refuses_normally() -> bool:
    """True in an ordinary environment: connecting to a closed local port
    raises ConnectionRefused. Inside an already-sandboxed run (our own
    dogfood gate, say) the OS denies the attempt instead — the mode-none
    counterproof below can't be observed there."""
    s = socket.socket()
    try:
        s.connect(("127.0.0.1", 9))
        return False        # something answered on the discard port?!
    except ConnectionRefusedError:
        return True
    except OSError:
        return False
    finally:
        s.close()


needs_open_network = pytest.mark.skipif(
    not _network_refuses_normally(),
    reason="environment already denies network")

CMD = ["python3", "-m", "pytest", "-q"]


# ---------------------------------------------------------------- unit layer

def test_wrap_none_is_identity():
    assert isolation.wrap(CMD, "none") == CMD


def test_wrap_sandbox_argv_and_profile(tmp_path):
    argv = isolation.wrap(CMD, "sandbox", write_roots=[str(tmp_path)])
    assert argv[:2] == ["sandbox-exec", "-p"] and argv[3:] == CMD
    profile = argv[2]
    assert "(deny network*)" in profile
    assert "(deny file-write*)" in profile
    assert f'(subpath "{tmp_path.resolve()}")' in profile
    assert '(subpath "/private/var/folders")' in profile


def test_profile_quotes_awkward_paths():
    profile = isolation.sandbox_profile(['/tmp/we"ird'])
    assert '\\"ird' in profile


def test_wrap_container_argv(monkeypatch, tmp_path):
    monkeypatch.setattr(isolation.shutil, "which",
                        lambda c: "/usr/bin/docker" if c == "docker" else None)
    argv = isolation.wrap(CMD, "container", worktree=str(tmp_path),
                          image="python:3.12")
    assert argv[:4] == ["docker", "run", "--rm", "--network=none"]
    assert f"{tmp_path.resolve()}:/work" in argv
    assert ["-w", "/work"] == argv[argv.index("-w"):argv.index("-w") + 2]
    assert argv[-len(CMD) - 1:] == ["python:3.12", *CMD]


def test_container_runtime_pin_and_fallback(monkeypatch):
    monkeypatch.setattr(isolation.shutil, "which",
                        lambda c: "/bin/podman" if c == "podman" else None)
    assert isolation.container_runtime() == "podman"
    assert isolation.container_runtime("podman") == "podman"
    assert isolation.container_runtime("docker") is None


def test_unavailable_reasons(monkeypatch):
    assert isolation.unavailable("none") is None
    assert "container_image" in isolation.unavailable("container")
    monkeypatch.setattr(isolation.shutil, "which", lambda c: None)
    assert "no container runtime" in isolation.unavailable("container", "img")
    assert "not on PATH" in isolation.unavailable("container", "img", "docker")
    assert "unknown" in isolation.unavailable("warp-field")
    monkeypatch.setattr(isolation.sys, "platform", "linux")
    assert "macOS-only" in isolation.unavailable("sandbox")


# ------------------------------------------------------------- engine layer

NET_PROBE = (
    "import socket\n\n\n"
    "def test_network_is_denied():\n"
    "    s = socket.socket()\n"
    "    try:\n"
    "        s.connect(('127.0.0.1', 9))\n"
    "        raise AssertionError('connect succeeded')\n"
    "    except PermissionError:\n"
    "        pass  # the sandbox said no — exactly the point\n"
    "    finally:\n"
    "        s.close()\n")


@pytest.fixture()
def repo(tmp_path):
    def sh(*args):
        subprocess.run(args, cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / "tests").mkdir()
    (tmp_path / "mod.py").write_text("x = 1\n")
    (tmp_path / "tests" / "test_mod.py").write_text(NET_PROBE)
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


def make_engine(repo, mode, **cfg):
    eng = Engine(str(repo))
    eng.config["test_cmd"] = [sys.executable, "-m", "pytest", "-q",
                              "-p", "no:cacheprovider"]
    eng.config["isolation"] = mode
    eng.config.update(cfg)
    return eng


@needs_sandbox
def test_sandboxed_landing_lands_and_network_is_really_denied(repo):
    # the fixture's test PASSES only when connect() raises PermissionError,
    # i.e. only when the gate truly ran it inside the sandbox
    eng = make_engine(repo, "sandbox")
    res = eng.submit(branch_touch(repo, "wa"), agent="a", title="probe")
    assert res["verdict"] == "landed", res


@needs_open_network
def test_unisolated_gate_fails_the_probe(repo):
    # same fixture without the sandbox: connect() raises ConnectionRefused,
    # the probe test reddens — proof the wrapper above changed real behavior
    eng = make_engine(repo, "none")
    res = eng.submit(branch_touch(repo, "wb"), agent="b", title="probe")
    assert res["verdict"] == "escalated"


def test_unavailable_backend_reddens_gate_not_silent_fallback(repo, monkeypatch):
    monkeypatch.setattr(isolation.shutil, "which", lambda c: None)
    eng = make_engine(repo, "container", container_image="python:3.12")
    res = eng.submit(branch_touch(repo, "wc"), agent="c", title="probe")
    assert res["verdict"] == "escalated"
    assert "isolation unavailable" in res["gate"]["summary"]


@needs_sandbox
def test_facts_recorded_unisolated_are_not_inherited_by_sandbox(repo, tmp_path_factory):
    # a green fact minted with the network open must not satisfy a sandboxed
    # gate: the isolation mode is part of the fact key
    def sh(*args):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)

    (repo / "tests" / "test_mod.py").write_text(
        "def test_ok():\n    assert True\n")
    sh("git", "add", "-A")
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t",
       "commit", "-q", "-m", "plain")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    facts = FactsStore(tmp_path_factory.mktemp("facts"))
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    first = run_gate(str(repo), head, ["tests/test_mod.py"], cmd, facts=facts)
    assert first["green"] and first["memo"]["runs"] == 1
    replay = run_gate(str(repo), head, ["tests/test_mod.py"], cmd, facts=facts)
    assert replay["memo"] == {"hits": 1, "runs": 0}
    sandboxed = run_gate(str(repo), head, ["tests/test_mod.py"], cmd,
                         facts=facts, isolation_mode="sandbox")
    assert sandboxed["green"] and sandboxed["memo"]["runs"] == 1
