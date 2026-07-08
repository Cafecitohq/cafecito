import hashlib
import subprocess
import sys
import textwrap

import pytest

from cafecito.engine import Engine, _match_generated

GEN_CMD = [sys.executable, "-c", textwrap.dedent("""\
    import hashlib, pathlib
    m = pathlib.Path("manifest.txt").read_text()
    h = hashlib.sha256(m.encode()).hexdigest()[:16]
    pathlib.Path("lock.txt").write_text(f"LOCK {h}\\n" + m.upper())
    """)]

MANIFEST = "".join(f"dep{i}=1\n" for i in range(1, 16))


def lock_for(manifest: str) -> str:
    # like real lockfiles, content depends globally on the manifest (integrity
    # hash), so any two different bumps collide on the hash line
    h = hashlib.sha256(manifest.encode()).hexdigest()[:16]
    return f"LOCK {h}\n" + manifest.upper()


@pytest.fixture()
def repo(tmp_path):
    def sh(*args, cwd=tmp_path):
        subprocess.run(args, cwd=cwd, check=True, capture_output=True)

    (tmp_path / "manifest.txt").write_text(MANIFEST)
    (tmp_path / "lock.txt").write_text(lock_for(MANIFEST))
    sh("git", "init", "-q", "-b", "main")
    sh("git", "add", "-A")
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "i")
    return tmp_path


def branch_with(repo, name, dep_line, new_value):
    """A branch off main where one manifest dep changes and the lockfile is
    regenerated locally (as an agent's tooling would)."""
    def sh(*args):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)

    sh("git", "checkout", "-q", "-b", name, "main")
    manifest = (repo / "manifest.txt").read_text().replace(
        f"dep{dep_line}=1", f"dep{dep_line}={new_value}")
    (repo / "manifest.txt").write_text(manifest)
    (repo / "lock.txt").write_text(lock_for(manifest))
    sh("git", "add", "-A")
    sh("git", "-c", "user.name=a", "-c", "user.email=a@a", "commit", "-q", "-m",
       f"bump dep{dep_line}")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    sh("git", "checkout", "-q", "main")
    return head


def make_engine(repo, gen_cmd=GEN_CMD):
    eng = Engine(str(repo))
    eng.config["test_cmd"] = [sys.executable, "-c", "pass"]
    eng.config["generated"] = {"lock.txt": gen_cmd}
    return eng


def test_lockfile_conflict_regenerates_deterministically(repo):
    eng = make_engine(repo)
    a = branch_with(repo, "wa", 2, 9)     # top of manifest
    b = branch_with(repo, "wb", 14, 7)    # bottom — manifest merges clean
    ra = eng.submit(a, agent="a", title="bump dep2")
    assert ra["verdict"] == "landed"
    rb = eng.submit(b, agent="b", title="bump dep14")
    assert rb["verdict"] == "landed", rb
    # the landing regenerated the lock from the MERGED manifest — no LLM
    entry = eng._log_entries(1)[0]
    assert entry.get("generated") == ["lock.txt"]
    assert entry.get("regen_s") is None  # no reconciler call happened
    tip = eng.status()["tip"]
    merged_manifest = subprocess.run(
        ["git", "show", f"{tip}:manifest.txt"], cwd=repo,
        capture_output=True, text=True).stdout
    assert "dep2=9" in merged_manifest and "dep14=7" in merged_manifest
    lock = subprocess.run(["git", "show", f"{tip}:lock.txt"], cwd=repo,
                          capture_output=True, text=True).stdout
    assert lock == lock_for(merged_manifest)


def test_generator_failure_escalates(repo):
    eng = make_engine(repo, gen_cmd=[sys.executable, "-c", "raise SystemExit(1)"])
    a = branch_with(repo, "wa", 2, 9)
    b = branch_with(repo, "wb", 14, 7)
    assert eng.submit(a, agent="a", title="bump dep2")["verdict"] == "landed"
    rb = eng.submit(b, agent="b", title="bump dep14")
    assert rb["verdict"] == "escalated"
    assert "generator failed" in rb["reason"]


def test_generator_must_produce_file(repo):
    eng = make_engine(repo, gen_cmd=[sys.executable, "-c", "pass"])
    a = branch_with(repo, "wa", 2, 9)
    b = branch_with(repo, "wb", 14, 7)
    assert eng.submit(a, agent="a", title="bump dep2")["verdict"] == "landed"
    rb = eng.submit(b, agent="b", title="bump dep14")
    assert rb["verdict"] == "escalated"
    assert "did not produce" in rb["reason"]


def test_match_generated_patterns():
    cmd = ["true"]
    got = _match_generated(
        {"package-lock.json", "web/package-lock.json", "src/app.ts"},
        {"package-lock.json": cmd})
    assert set(got) == {"package-lock.json", "web/package-lock.json"}
    got = _match_generated({"deep/Cargo.lock"}, {"**/Cargo.lock": "cargo generate-lockfile"})
    assert got == {"deep/Cargo.lock": ["cargo", "generate-lockfile"]}
    assert _match_generated({"src/app.ts"}, {"*.lock": cmd}) == {}
