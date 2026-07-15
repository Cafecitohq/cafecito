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
import fnmatch
import json
import os
import pathlib
import re
import shlex
import subprocess
import tempfile
import time
import uuid

from .facts import FactsStore
from .gate import _is_test_file, impact_tests, run_gate, test_family
from .gitutil import git, git_rc
from .regen import live_regen
from .writeset import write_set

DEFAULT_CONFIG = {
    "branch": "cafecito/main",
    "test_cmd": ["python3", "-m", "pytest", "-q", "--tb=line",
                 "-p", "no:cacheprovider"],
    "reconciler_model": "sonnet",
    "lease_ttl_s": 900,
    "gate_timeout_s": 900,
    "require_signal": False,
    # generated files never get merged OR LLM-regenerated: their generator is
    # re-run against the merged sources. Map path pattern -> command, e.g.
    #   "package-lock.json": ["npm", "install", "--package-lock-only"]
    # The command runs with cwd = the file's directory, inheriting the
    # environment (generators need caches/registries). Operator opt-in.
    "generated": {},
    "generator_timeout_s": 300,
    # gate_mode "impact" runs the changeset's impact tests; "full" runs every
    # test file in the tree, memoized by input closure — only tests whose
    # closure the landing touched actually execute (SPEC: verification facts).
    "gate_mode": "impact",
    # full mode collects test files of the family the test_cmd can execute,
    # derived from the command (pytest→py, vitest/jest/npx→js, go→go).
    # Set explicitly (e.g. ["js"]) when the runner isn't recognizable.
    "gate_families": [],
    "memoize": True,
    # a REGENERATED candidate that fails the gate gets this many fresh
    # reconciler attempts with the gate failure fed back (deterministic
    # generator output never retries — same inputs, same result)
    "regen_retries": 1,
    # prepare bare gate worktrees before tests (npm ci, pip install -e ., …);
    # runs with the real environment, unlike the tests themselves
    "setup_cmd": [],
    "setup_timeout_s": 600,
    # run gate tests behind an isolation boundary (isolation.py):
    # "none" | "sandbox" (macOS sandbox-exec: no network, writes confined
    # to the gate worktree) | "container" (docker/podman, --network=none;
    # requires container_image). Unavailable backends redden the gate —
    # never a silent fallback to unisolated runs. setup_cmd still runs on
    # the host with the real environment.
    "isolation": "none",
    "container_image": "",
    "container_runtime": "",
}


_RUNNER_FAMILY = {"pytest": "py", "python": "py", "tox": "py", "nox": "py",
                  "vitest": "js", "jest": "js", "mocha": "js", "node": "js",
                  "npx": "js", "npm": "js", "pnpm": "js", "yarn": "js",
                  "bun": "js", "deno": "js", "go": "go", "gotestsum": "go"}


def key_path(key: str) -> str:
    """The repo path a lease key covers: `file:<path>` and the oracle's
    `<lang>:<path>::<qual>` both map to <path>; anything else covers itself."""
    if key.startswith("file:"):
        return key[5:]
    head, sep, _ = key.partition("::")
    if sep and ":" in head:
        return head.split(":", 1)[1]
    return key


def keys_overlap(a: str, b: str) -> bool:
    """Granularity-aware lease overlap. Identical keys overlap; a `file:` key
    overlaps every key on its path (symbol leases live inside it); two
    distinct symbols in one file do NOT overlap — symbol-disjoint writers
    commute, so their leases must not contend either."""
    if a == b:
        return True
    if key_path(a) != key_path(b):
        return False
    return a.startswith("file:") or b.startswith("file:")


class Engine:
    def __init__(self, repo: str):
        self.repo = str(pathlib.Path(repo).resolve())
        if git_rc(self.repo, "rev-parse", "--git-dir")[0] != 0:
            raise RuntimeError(f"not a git repository: {self.repo}")
        self.state_dir = pathlib.Path(self.repo) / ".cafecito"
        self.state_dir.mkdir(exist_ok=True)
        self._exclude_state_dir()
        self.config = dict(DEFAULT_CONFIG)
        cfg = self.state_dir / "config.json"
        if cfg.exists():
            self.config.update(json.loads(cfg.read_text()))
        else:
            cfg.write_text(json.dumps(self.config, indent=1))
        self._init_state()

    # ------------------------------------------------------------- state ---

    def _exclude_state_dir(self) -> None:
        """Keep engine state out of the user's git history via info/exclude —
        `git add -A` must never stage .cafecito/ (dogfood-adjacent finding:
        a committed landed log breaks branch switching)."""
        code, out, _ = git_rc(self.repo, "rev-parse", "--git-common-dir")
        if code != 0:
            return
        exclude = pathlib.Path(out.strip())
        if not exclude.is_absolute():
            exclude = pathlib.Path(self.repo) / exclude
        exclude = exclude / "info" / "exclude"
        try:
            existing = exclude.read_text() if exclude.exists() else ""
            if ".cafecito/" not in existing:
                exclude.parent.mkdir(parents=True, exist_ok=True)
                with exclude.open("a") as f:
                    f.write("\n# cafecito engine state\n.cafecito/\n")
        except OSError:
            pass

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
            safe = re.sub(r"[^A-Za-z0-9_-]", "-", agent or "agent")[:12]
            wt = pathlib.Path(tempfile.mkdtemp(prefix=f"cafecito-{safe}-"))
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
                if v["agent"] != agent
                and any(keys_overlap(k, q) for q in keys)
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
            "inflight": [{"agent": e.get("agent"), "title": e.get("title"),
                          "for_s": round(time.time() - e.get("started", 0))}
                         for e in self._inflight().values()],
        }

    def submit(self, ref: str, agent: str = "", title: str = "") -> dict:
        code, out, err = git_rc(self.repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
        if code != 0:
            return {"verdict": "rejected", "reason": f"unknown ref {ref!r}"}
        head = out.strip()
        cs_id = f"cs_{uuid.uuid4().hex[:10]}"

        # ---- admission: wait until our write set is disjoint from every
        # in-flight submission, then register. Commuting submissions pass
        # straight through and gate CONCURRENTLY; colliders queue here. ----
        deadline = time.time() + self.config["gate_timeout_s"] * 2
        clash: list[str] = []
        while True:
            with self._lock():
                tip = self._tip()
                code, base, _ = git_rc(self.repo, "merge-base", tip, head)
                if code != 0:
                    return {"verdict": "rejected",
                            "reason": "no common history with tip"}
                base = base.strip()
                if base == head:
                    return {"verdict": "rejected",
                            "reason": "changeset already in tip"}
                title = title or git(self.repo, "log", "-1", "--format=%s",
                                     head).strip()
                symbols, files = write_set(self.repo, base, head)
                infl = self._inflight()
                clash = sorted({e.get("agent") or k for k, e in infl.items()
                                if set(e["symbols"]) & set(symbols)
                                or set(e["files"]) & set(files)})
                if not clash:
                    infl[cs_id] = {"symbols": sorted(symbols),
                                   "files": sorted(files), "agent": agent,
                                   "title": title, "started": time.time()}
                    self._save_inflight(infl)
                    break
            if time.time() > deadline:
                return {"verdict": "rejected",
                        "reason": f"admission timeout behind {', '.join(clash)}"}
            time.sleep(0.3)

        try:
            return self._build_gate_land(cs_id, head, agent, title, tip, base,
                                         symbols, files)
        finally:
            with self._lock():
                infl = self._inflight()
                if infl.pop(cs_id, None) is not None:
                    self._save_inflight(infl)

    def _gate_families(self) -> set[str]:
        """Test-file families full gate mode may collect: the explicit
        config wins; otherwise derived from what the test_cmd can run.
        Unrecognizable runners default to py — the pre-multi-language
        behavior — rather than handing a runner files it can't execute."""
        explicit = self.config.get("gate_families")
        if explicit:
            return set(explicit)
        fams = set()
        for tok in self.config["test_cmd"]:
            base = pathlib.PurePosixPath(tok).name
            for runner, fam in _RUNNER_FAMILY.items():
                if base == runner or base.startswith(runner + "3") \
                        or base.startswith(runner + "."):
                    fams.add(fam)
        return fams or {"py"}

    def _build_gate_land(self, cs_id, head, agent, title, tip, base,
                         symbols, files) -> dict:
        """Build the candidate and gate it with the lock RELEASED — this is
        wave parallelism. If a commuting landing advanced the tip while we
        gated, rebase and re-gate: memoized facts make the re-gate near-free
        (untouched closures inherit)."""
        feedback = ""
        regen_attempts = 0
        for attempt in range(10):
            built = self._build_candidate(cs_id, head, agent, title, tip, base,
                                          feedback=feedback)
            if "verdict" in built:
                return built
            candidate, conflicted = built["candidate"], built["conflicted"]
            if built["regen_s"] is not None:
                regen_attempts += 1

            if self.config.get("gate_mode") == "full":
                listing = git(self.repo, "ls-tree", "-r", "--name-only",
                              candidate).splitlines()
                fams = self._gate_families()
                gate_files = sorted(p for p in listing if _is_test_file(p)
                                    and test_family(p) in fams)
            else:
                gate_files = sorted(impact_tests(
                    self.repo, set(files) | conflicted, candidate))
            facts = FactsStore(self.state_dir) if self.config.get("memoize", True) \
                else None
            gate = run_gate(self.repo, candidate, gate_files,
                            self.config["test_cmd"],
                            timeout=self.config["gate_timeout_s"], facts=facts,
                            setup_cmd=self.config.get("setup_cmd") or None,
                            setup_timeout=self.config.get("setup_timeout_s", 600),
                            isolation_mode=self.config.get("isolation", "none"),
                            container_image=self.config.get("container_image", ""),
                            container_runtime=self.config.get("container_runtime", ""))
            no_signal_refused = (gate["green"] and gate.get("no_signal")
                                 and self.config.get("require_signal"))
            if not gate["green"] or no_signal_refused:
                # a regenerated candidate earns fresh reconciler attempts with
                # the gate failure fed back, before anyone gets escalated to
                if (not no_signal_refused and built["regen_s"] is not None
                        and regen_attempts <= self.config.get("regen_retries", 1)):
                    feedback = gate.get("summary", "")[:400]
                    continue
                reason = ("no test signal (require_signal)" if no_signal_refused
                          else "failed landing gate")
                entry = {"id": cs_id, "verdict": "escalated", "title": title,
                         "agent": agent, "head": head,
                         "reason": reason, "gate": gate,
                         "regen_attempts": regen_attempts or None}
                self._append_log(entry)
                return {"verdict": "escalated", "id": cs_id,
                        "reason": reason, "gate": gate}
            feedback = ""

            with self._lock():
                if self._tip() == tip:
                    self._set_tip(candidate)
                    if agent:  # landing releases the agent's leases
                        leases = {k: v for k, v in self._leases().items()
                                  if v["agent"] != agent}
                        self._save_leases(leases)
                    entry = {"id": cs_id, "verdict": "landed", "title": title,
                             "agent": agent, "head": head, "landed": candidate,
                             "files": sorted(files), "symbols": sorted(symbols),
                             "regen_s": built["regen_s"], "gate": gate}
                    if regen_attempts > 1:
                        entry["regen_attempts"] = regen_attempts
                    if attempt:
                        entry["raced"] = attempt
                    if built["gen_s"] is not None:
                        entry["gen_s"] = built["gen_s"]
                        entry["generated"] = sorted(built["gen_map"])
                    self._append_log(entry)
                    return {"verdict": "landed", "id": cs_id, "tip": candidate,
                            "regenerated": built["regen_s"] is not None,
                            "gate": gate}
                tip = self._tip()
            # tip moved while we gated: rebase against it and go again
            code, nb, _ = git_rc(self.repo, "merge-base", tip, head)
            if code != 0:
                return {"verdict": "rejected",
                        "reason": "no common history with tip"}
            base = nb.strip()
            symbols, files = write_set(self.repo, base, head)

        entry = {"id": cs_id, "verdict": "escalated", "title": title,
                 "agent": agent, "head": head,
                 "reason": "landing raced 10 times; resubmit"}
        self._append_log(entry)
        return {"verdict": "escalated", "id": cs_id,
                "reason": "landing raced 10 times; resubmit"}

    def _build_candidate(self, cs_id, head, agent, title, tip, base,
                         feedback: str = "") -> dict:
        """Candidate commit for tip+head: clean merge, or split conflict into
        generated (deterministic regeneration) and code (reconciler) paths.
        Returns build info, or a verdict dict on escalation/rejection."""
        code, out, _ = git_rc(self.repo, "merge-tree", "--write-tree",
                              "--name-only", "--no-messages",
                              f"--merge-base={base}", tip, head)
        regen_s, conflicted = None, set()
        gen_s, gen_map = None, {}
        if code == 0:
            tree = out.splitlines()[0].strip()
            candidate = git(self.repo, "commit-tree", tree, "-p", tip,
                            "-m", _land_message(title, cs_id)).strip()
        elif code == 1:
            conflicted = {p for p in out.splitlines()[1:] if p}
            gen_map = _match_generated(conflicted,
                                       self.config.get("generated") or {})
            code_conf = conflicted - set(gen_map)
            regen_files: dict[str, str] = {}
            if code_conf:
                intent_in = git(self.repo, "log", "--format=- %B",
                                f"{base}..{head}")[:2000]
                landed_titles = "\n".join(
                    f"- {e['title']}" for e in self._log_entries(50)
                    if e["verdict"] == "landed"
                    and set(e.get("files", [])) & code_conf)
                result, why = live_regen(
                    self.repo, base, tip, head, code_conf,
                    landed_titles, intent_in,
                    model=self.config["reconciler_model"], feedback=feedback)
                if result is None:
                    entry = {"id": cs_id, "verdict": "escalated",
                             "title": title, "agent": agent, "head": head,
                             "reason": why, "conflicted": sorted(conflicted)}
                    self._append_log(entry)
                    return {"verdict": "escalated", "id": cs_id,
                            "reason": why, "conflicted": sorted(conflicted)}
                regen_files, regen_s = result
            tree = out.splitlines()[0].strip()
            if gen_map:
                gres, why = _run_generators(
                    self.repo, tree, tip, regen_files, gen_map,
                    self.config["generator_timeout_s"])
                if gres is None:
                    entry = {"id": cs_id, "verdict": "escalated",
                             "title": title, "agent": agent, "head": head,
                             "reason": why, "conflicted": sorted(conflicted)}
                    self._append_log(entry)
                    return {"verdict": "escalated", "id": cs_id,
                            "reason": why, "conflicted": sorted(conflicted)}
                gen_files, gen_s = gres
                regen_files = {**regen_files, **gen_files}
            candidate = _commit_files(self.repo, tree, tip, regen_files,
                                      _land_message(title, cs_id,
                                                    regenerated=True))
        else:
            return {"verdict": "rejected", "reason": "merge-tree error"}
        return {"candidate": candidate, "conflicted": conflicted,
                "regen_s": regen_s, "gen_s": gen_s, "gen_map": gen_map}

    def advance(self, to: str = "HEAD") -> dict:
        """Follow out-of-band commits: move the landed tip to a descendant.

        For commits made outside cafecito (maintainer pushes docs to main).
        The target must contain the current tip; the move is recorded in the
        landed log."""
        code, out, _ = git_rc(self.repo, "rev-parse", "--verify", f"{to}^{{commit}}")
        if code != 0:
            return {"verdict": "rejected", "reason": f"unknown ref {to!r}"}
        new = out.strip()
        with self._lock():
            tip = self._tip()
            if new == tip:
                return {"verdict": "noop", "tip": tip}
            code, _, _ = git_rc(self.repo, "merge-base", "--is-ancestor", tip, new)
            if code != 0:
                return {"verdict": "rejected",
                        "reason": "target does not contain the landed tip"}
            self._set_tip(new)
            self._append_log({"id": f"adv_{uuid.uuid4().hex[:10]}",
                              "verdict": "advanced", "head": new,
                              "title": f"tip advanced to {new[:12]}"})
            return {"verdict": "advanced", "tip": new}

    def _inflight(self) -> dict:
        p = self.state_dir / "inflight.json"
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            d = {}
        horizon = time.time() - self.config["gate_timeout_s"] * 2
        return {k: v for k, v in d.items() if v.get("started", 0) > horizon}

    def _save_inflight(self, d: dict) -> None:
        p = self.state_dir / "inflight.json"
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, indent=1))
        os.replace(tmp, p)


def _match_generated(conflicted: set[str], gen_config: dict) -> dict[str, list[str]]:
    """Conflicted paths covered by the operator's generated-file config.
    Patterns fnmatch against the full path and the basename."""
    out: dict[str, list[str]] = {}
    for p in sorted(conflicted):
        for pat, cmd in gen_config.items():
            if fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(pathlib.Path(p).name, pat):
                out[p] = cmd if isinstance(cmd, list) else shlex.split(cmd)
                break
    return out


def _run_generators(repo: str, tree: str, parent: str, seed_files: dict[str, str],
                    gen_map: dict[str, list[str]], timeout: int):
    """Deterministic regeneration: materialize the merged state, delete each
    generated file (its conflicted content is marker garbage), run its
    generator with cwd = the file's directory, read the result back.

    Returns ({path: content}, seconds) or (None, reason)."""
    stage = _commit_files(repo, tree, parent, seed_files, "cafecito generator-stage")
    root = pathlib.Path(tempfile.mkdtemp(prefix="cafecito-gen-"))
    wt = root / "wt"
    t0 = time.time()
    try:
        git(repo, "worktree", "add", "--detach", "--quiet", str(wt), stage)
        for path, cmd in sorted(gen_map.items()):
            target = wt / path
            target.unlink(missing_ok=True)
            try:
                r = subprocess.run(cmd, cwd=target.parent, capture_output=True,
                                   text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                return None, f"generator timeout for {path}"
            except OSError as ex:
                return None, f"generator failed for {path}: {ex}"
            if r.returncode != 0:
                return None, (f"generator failed for {path}: "
                              f"{(r.stderr or r.stdout).strip()[:150]}")
            if not target.exists():
                return None, f"generator did not produce {path}"
        contents = {p: (wt / p).read_text(errors="replace") for p in gen_map}
        return (contents, round(time.time() - t0, 1)), None
    finally:
        git_rc(repo, "worktree", "remove", "--force", str(wt))


def _land_message(title: str, cs_id: str, regenerated: bool = False) -> str:
    lines = [
        f"land: {title[:70]}",
        "",
        f"Changeset-Id: {cs_id}",
    ]
    if regenerated:
        lines.append("Regenerated: true")
    lines.append("Signed-off-by: cafecito-engine <engine@cafecito.local>")
    return "\n".join(lines)


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
