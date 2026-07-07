"""cafecito engine v0.1 — the landing pipeline.

State lives in <repo>/.cafecito/ (log.jsonl, leases.json, state.json, lock).
Multiple MCP server processes (one per connected agent session) share it via
an advisory file lock; landing bookkeeping is serialized, agents never are.

Pipeline per submit (SPEC §6, v0.1 subset):
  merge-base → write set (oracle) → merge-tree vs landed tip
    clean     → candidate
    conflict  → live regenerative merge against the tip → candidate | escalate
  → landing gate (changeset tests + impact tests, ALWAYS — clean merges too)
  → landed log append → materialized branch ref advances

The materialized branch (default `cafecito/main`) is a normal git branch:
humans, CI, and deploy tooling see ordinary commits. Agents never rebase.
"""

from __future__ import annotations

import fcntl
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "oracle"))

from gate import impact_tests, run_gate  # noqa: E402
from gitutil import git, git_rc  # noqa: E402
from regen import live_regen  # noqa: E402
from writeset import write_set  # noqa: E402

DEFAULT_CONFIG = {
    "branch": "cafecito/main",
    "test_cmd": [sys.executable, "-m", "pytest", "-q", "--tb=line",
                 "-p", "no:cacheprovider"],
    "reconciler_model": "sonnet",
    "lease_ttl_s": 900,
    "gate_timeout_s": 900,
}


class Engine:
    def __init__(self, repo: str):
        self.repo = str(pathlib.Path(repo).resolve())
        if git_rc(self.repo, "rev-parse", "--git-dir")[0] != 0:
            raise RuntimeError(f"not a git repository: {self.repo}")
        self.state_dir = pathlib.Path(self.repo) / ".cafecito"
        self.state_dir.mkdir(exist_ok=True)
        self.config = dict(DEFAULT_CONFIG)
        cfg = self.state_dir / "config.json"
        if cfg.exists():
            self.config.update(json.loads(cfg.read_text()))
        else:
            cfg.write_text(json.dumps(self.config, indent=1))
        self._init_state()

    # ------------------------------------------------------------- state ---

    def _init_state(self) -> None:
        with self._lock():
            sf = self.state_dir / "state.json"
            if not sf.exists():
                tip = git(self.repo, "rev-parse", "HEAD").strip()
                git(self.repo, "update-ref", f"refs/heads/{self.config['branch']}", tip)
                sf.write_text(json.dumps({"tip": tip, "initialized_at": time.time()}))

    def _lock(self):
        return _FileLock(self.state_dir / "lock")

    def _tip(self) -> str:
        return json.loads((self.state_dir / "state.json").read_text())["tip"]

    def _set_tip(self, tip: str) -> None:
        git(self.repo, "update-ref", f"refs/heads/{self.config['branch']}", tip)
        (self.state_dir / "state.json").write_text(
            json.dumps({"tip": tip, "updated_at": time.time()}))

    def _append_log(self, entry: dict) -> None:
        entry["at"] = time.time()
        with (self.state_dir / "log.jsonl").open("a") as f:
            f.write(json.dumps(entry) + "\n")

    def _log_entries(self, limit: int = 50) -> list[dict]:
        p = self.state_dir / "log.jsonl"
        if not p.exists():
            return []
        lines = p.read_text().splitlines()[-limit:]
        return [json.loads(l) for l in lines]

    def _leases(self) -> dict:
        p = self.state_dir / "leases.json"
        leases = json.loads(p.read_text()) if p.exists() else {}
        now = time.time()
        return {k: v for k, v in leases.items() if v["expires"] > now}

    def _save_leases(self, leases: dict) -> None:
        (self.state_dir / "leases.json").write_text(json.dumps(leases, indent=1))

    # -------------------------------------------------------------- tools ---

    def sync(self, agent: str | None = None, create_worktree: bool = False) -> dict:
        tip = self._tip()
        out = {"tip": tip, "branch": self.config["branch"],
               "hint": f"work from {tip[:12]}; commit; then submit your HEAD sha"}
        if create_worktree:
            wt = pathlib.Path(tempfile.mkdtemp(
                prefix=f"cafecito-{(agent or 'agent')[:12]}-"))
            path = wt / "wt"
            git(self.repo, "worktree", "add", "--detach", "--quiet", str(path), tip)
            out["worktree"] = str(path)
        return out

    def reserve(self, keys: list[str], agent: str, ttl: int | None = None,
                intent: str = "") -> dict:
        with self._lock():
            leases = self._leases()
            conflicts = [
                {"key": k, "holder": v["agent"], "intent": v.get("intent", ""),
                 "expires_in_s": round(v["expires"] - time.time())}
                for k, v in leases.items()
                if k in keys and v["agent"] != agent
            ]
            if conflicts:
                return {"granted": False, "conflicts": conflicts}
            expires = time.time() + (ttl or self.config["lease_ttl_s"])
            for k in keys:
                leases[k] = {"agent": agent, "intent": intent, "expires": expires}
            self._save_leases(leases)
            return {"granted": True, "keys": keys,
                    "expires_in_s": round(expires - time.time())}

    def status(self, limit: int = 20) -> dict:
        entries = self._log_entries(limit)
        return {
            "tip": self._tip(),
            "branch": self.config["branch"],
            "landed": sum(1 for e in self._log_entries(10_000)
                          if e["verdict"] == "landed"),
            "escalated": sum(1 for e in self._log_entries(10_000)
                             if e["verdict"] == "escalated"),
            "recent": [{k: e.get(k) for k in
                        ("id", "verdict", "title", "reason", "gate", "regen_s")}
                       for e in reversed(entries)],
            "active_leases": self._leases(),
        }

    def submit(self, ref: str, agent: str = "", title: str = "") -> dict:
        code, out, err = git_rc(self.repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
        if code != 0:
            return {"verdict": "rejected", "reason": f"unknown ref {ref!r}"}
        head = out.strip()
        cs_id = f"cs_{uuid.uuid4().hex[:10]}"
        with self._lock():
            tip = self._tip()
            code, base, _ = git_rc(self.repo, "merge-base", tip, head)
            if code != 0:
                return {"verdict": "rejected", "reason": "no common history with tip"}
            base = base.strip()
            if base == head:
                return {"verdict": "rejected", "reason": "changeset already in tip"}
            title = title or git(self.repo, "log", "-1", "--format=%s", head).strip()
            symbols, files = write_set(self.repo, base, head)

            code, out, _ = git_rc(self.repo, "merge-tree", "--write-tree",
                                  "--name-only", "--no-messages",
                                  f"--merge-base={base}", tip, head)
            regen_s, conflicted = None, set()
            if code == 0:
                tree = out.splitlines()[0].strip()
                candidate = git(self.repo, "commit-tree", tree, "-p", tip,
                                "-m", f"land: {title[:70]}\n\ncafecito: {cs_id}").strip()
            elif code == 1:
                conflicted = {p for p in out.splitlines()[1:] if p}
                intent_in = git(self.repo, "log", "--format=- %B",
                                f"{base}..{head}")[:2000]
                landed_titles = "\n".join(
                    f"- {e['title']}" for e in self._log_entries(50)
                    if e["verdict"] == "landed"
                    and set(e.get("files", [])) & conflicted)
                result, why = live_regen(
                    self.repo, base, tip, head, conflicted,
                    landed_titles, intent_in,
                    model=self.config["reconciler_model"])
                if result is None:
                    entry = {"id": cs_id, "verdict": "escalated", "title": title,
                             "agent": agent, "head": head, "reason": why,
                             "conflicted": sorted(conflicted)}
                    self._append_log(entry)
                    return {"verdict": "escalated", "id": cs_id, "reason": why,
                            "conflicted": sorted(conflicted)}
                regen_files, regen_s = result
                tree = out.splitlines()[0].strip()
                candidate = _commit_files(self.repo, tree, tip, regen_files,
                                          f"land: {title[:70]}\n\ncafecito: {cs_id} "
                                          f"(regenerated)")
            else:
                return {"verdict": "rejected", "reason": "merge-tree error"}

            gate_files = sorted(impact_tests(
                self.repo, set(files) | conflicted, candidate))
            gate = run_gate(self.repo, candidate, gate_files,
                            self.config["test_cmd"],
                            timeout=self.config["gate_timeout_s"])
            if not gate["green"]:
                entry = {"id": cs_id, "verdict": "escalated", "title": title,
                         "agent": agent, "head": head,
                         "reason": "failed landing gate", "gate": gate}
                self._append_log(entry)
                return {"verdict": "escalated", "id": cs_id,
                        "reason": "failed landing gate", "gate": gate}

            self._set_tip(candidate)
            if agent:  # landing releases the agent's leases
                leases = {k: v for k, v in self._leases().items()
                          if v["agent"] != agent}
                self._save_leases(leases)
            entry = {"id": cs_id, "verdict": "landed", "title": title,
                     "agent": agent, "head": head, "landed": candidate,
                     "files": sorted(files), "symbols": sorted(symbols),
                     "regen_s": regen_s, "gate": gate}
            self._append_log(entry)
            return {"verdict": "landed", "id": cs_id, "tip": candidate,
                    "regenerated": regen_s is not None, "gate": gate}


def _commit_files(repo: str, tree: str, parent: str, files: dict[str, str],
                  message: str) -> str:
    """New commit = `tree` with `files` overwritten. Plumbing, temp index."""
    with tempfile.TemporaryDirectory() as td:
        env = {**os.environ, "GIT_INDEX_FILE": str(pathlib.Path(td) / "idx")}

        def g(*args, inp=None):
            r = subprocess.run(["git", "-C", repo, *args], env=env, input=inp,
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"git {args[0]}: {r.stderr.strip()[:150]}")
            return r.stdout

        g("read-tree", tree)
        for path, content in files.items():
            blob = g("hash-object", "-w", "--stdin", inp=content).strip()
            g("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
        new_tree = g("write-tree").strip()
        return g("commit-tree", new_tree, "-p", parent, "-m", message).strip()


class _FileLock:
    def __init__(self, path: pathlib.Path):
        self.path = path

    def __enter__(self):
        self.fd = open(self.path, "w")
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        self.fd.close()
        return False
