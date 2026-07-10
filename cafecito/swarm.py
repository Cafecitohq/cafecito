"""cafecito swarm — one goal in, a parallel fleet out.

A planner agent decomposes a goal into mutually-independent tasks with
pairwise-disjoint file paths; a fleet of worker agents builds each task in its
own worktree; every changeset goes through the same landing gate as any other
submit. Changesets that commute land on `cafecito/main`; collisions the engine
can't reconcile escalate. The swarm never resolves a merge conflict itself.

Planning is factored behind a `call(prompt) -> str` seam so the decomposition
logic is unit-testable without the `claude` CLI (see tests/test_swarm.py).
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import subprocess

from .engine import Engine
from .fleetstate import SwarmState
from .gitutil import git, git_rc

# ------------------------------------------------------------------ planning ---

PLANNER_PROMPT = """\
You are the planner for a parallel fleet of coding agents working on ONE repo.

GOAL: {goal}

Repo file listing (partial):
{listing}

Decompose the goal into AT MOST {agents} tasks. The tasks run in PARALLEL in
separate worktrees, so they MUST be mutually independent: no task may depend on
another's output, and their file paths MUST be pairwise disjoint (no two tasks
touch the same path).

Output STRICT JSON and nothing else: a single JSON array of task objects, each
  {{"id": "<short-slug>",
    "title": "<one line>",
    "brief": "<2-4 sentences of what to build and how>",
    "paths": ["<1-3 repo file paths this task may create or modify>"]}}

Rules: ids are short lowercase slugs; each task lists 1-3 paths; paths across
tasks never overlap; prefer small, self-contained, testable units of work.
"""

WORKER_PROMPT = """\
You are a coding agent contributing to this project, working alone in this
repository checkout as part of a parallel fleet.

GOAL (for context): {goal}

YOUR TASK: {title}
{brief}

Rules: create or modify ONLY these paths: {paths}. If the task calls for tests,
they must be deterministic and use only the standard library + pytest. Match
the existing code style. Do not run tests or any shell commands. When done,
stop and summarize what you changed in one line.
"""

_JSON_ARRAY = re.compile(r"\[.*\]", re.DOTALL)


def _claude_plan(prompt: str, model: str) -> str:
    r = subprocess.run(
        ["claude", "-p", prompt, "--model", model],
        capture_output=True, text=True, timeout=240)
    if r.returncode != 0:
        raise RuntimeError(f"planner failed: {r.stderr.strip()[:200]}")
    return r.stdout


def plan_tasks(goal: str, listing: str, agents: int, call) -> list[dict]:
    """Decompose `goal` into <= `agents` independent tasks with disjoint paths.

    `call(prompt) -> str` is the model seam: it returns the planner's raw text
    (possibly wrapped in prose). We extract the first JSON array, validate each
    task's shape, cap the count, and drop any task whose paths overlap an
    already-accepted task. Returns [] if no valid array can be parsed.
    """
    text = call(PLANNER_PROMPT.format(goal=goal, listing=listing, agents=agents))
    m = _JSON_ARRAY.search(text or "")
    if not m:
        return []
    try:
        raw = json.loads(m.group(0))
    except (ValueError, TypeError):
        return []
    if not isinstance(raw, list):
        return []

    tasks: list[dict] = []
    claimed: set[str] = set()
    for item in raw:
        if len(tasks) >= agents:
            break
        if not isinstance(item, dict):
            continue
        tid = item.get("id")
        title = item.get("title")
        brief = item.get("brief")
        paths = item.get("paths")
        if not (isinstance(tid, str) and tid.strip()):
            continue
        if not (isinstance(title, str) and title.strip()):
            continue
        if not (isinstance(brief, str) and brief.strip()):
            continue
        if not (isinstance(paths, list) and paths):
            continue
        paths = [p for p in paths if isinstance(p, str) and p.strip()]
        if not (1 <= len(paths) <= 3):
            continue
        if claimed & set(paths):  # overlaps an accepted task — drop the offender
            continue
        claimed.update(paths)
        tasks.append({"id": tid.strip(), "title": title.strip(),
                      "brief": brief.strip(), "paths": paths})
    return tasks


def _run_worker(prompt: str, model: str, timeout: int, cwd: str):
    """The worker-agent seam (monkeypatchable in tests)."""
    return subprocess.run(
        ["claude", "-p", prompt, "--model", model,
         "--permission-mode", "acceptEdits",
         "--allowedTools", "Edit,Write,Read,Grep,Glob", "--max-turns", "30"],
        cwd=cwd, capture_output=True, text=True, timeout=timeout)


def _drifted(changed: list[str], declared: list[str]) -> list[str]:
    """Files the agent touched outside its assigned paths. Drift is reported,
    not blocked — admission control and the gate own correctness; the swarm
    owns transparency."""
    return sorted(set(changed) - set(declared))


def _repo_listing(repo: str, cap: int = 200) -> str:
    """A compact file listing of the landed tip, source files first, capped."""
    code, out, _ = git_rc(repo, "ls-tree", "-r", "--name-only", "HEAD")
    if code != 0:
        return ""
    files = [p for p in out.splitlines() if p]
    src_ext = (".py", ".js", ".ts", ".go", ".rs", ".java", ".rb", ".c", ".h",
               ".cpp", ".sh")

    def rank(p: str) -> tuple[int, str]:
        return (0 if p.endswith(src_ext) else 1, p)

    files.sort(key=rank)
    return "\n".join(files[:cap])


# ------------------------------------------------------------------ execution ---

def _run_task(engine: Engine, state: SwarmState, goal: str, task: dict,
              model: str, timeout: int) -> tuple[str, str, str]:
    """Build and submit one task. Returns (id, verdict, detail)."""
    tid = task["id"]
    title = task["title"]
    brief = task["brief"]
    paths = task["paths"]
    agent = f"swarm/{tid}"
    wt = None
    try:
        state.update(tid, "planning", title=title, detail="reserving")

        keys = [f"file:{p}" for p in paths]
        lease = engine.reserve(keys=keys, agent=agent, intent=title)
        contention = ""
        if not lease.get("granted"):  # leases are advisory — proceed anyway
            conflicts = lease.get("conflicts", [])
            holders = ", ".join(f"{c['key']} held by {c['holder']}"
                                for c in conflicts)
            contention = f"contention: {holders}"

        wt = engine.sync(agent=agent, create_worktree=True)["worktree"]
        state.update(tid, "working",
                     detail=(contention or f"building in {wt}"))

        prompt = WORKER_PROMPT.format(goal=goal, title=title, brief=brief,
                                      paths=", ".join(paths))
        r = _run_worker(prompt, model, timeout, wt)
        if r.returncode != 0:
            detail = f"agent failed: {r.stderr.strip()[:120]}"
            state.update(tid, "failed", detail=detail)
            return (tid, "failed", detail)

        status = git(wt, "status", "--porcelain").strip()
        if not status:
            state.update(tid, "failed", detail="no changes")
            return (tid, "failed", "no changes")

        git(wt, "add", "-A")
        git(wt, "-c", "user.name=cafecito-swarm",
            "-c", "user.email=swarm@cafecito.local", "commit", "-q", "-s",
            "-m", f"{title}\n\n{brief}")
        head = git(wt, "rev-parse", "HEAD").strip()
        changed = git(wt, "show", "--name-only", "--format=", head).split()
        drift = _drifted(changed, paths)
        drift_note = f" · drifted: {', '.join(drift[:4])}" if drift else ""

        state.update(tid, "submitting", detail=f"head {head[:12]}{drift_note}")
        result = engine.submit(head, agent=agent, title=title)
        verdict = result.get("verdict", "rejected")
        if verdict == "landed":
            gate = result.get("gate") or {}
            secs = gate.get("seconds")
            detail = f"gate {secs}s" if secs is not None else "landed"
            if result.get("regenerated"):
                detail += " (regenerated)"
            detail += drift_note
            state.update(tid, "landed", detail=detail)
            return (tid, "landed", detail)
        # escalated / rejected — the engine never resolves a merge conflict
        detail = result.get("reason", verdict) + drift_note
        state.update(tid, "escalated", detail=detail)
        return (tid, "escalated", detail)
    except Exception as exc:  # noqa: BLE001 — any task failure is per-task
        detail = str(exc)[:150]
        state.update(tid, "failed", detail=detail)
        return (tid, "failed", detail)
    finally:
        if wt:
            git_rc(engine.repo, "worktree", "remove", "--force", wt)


def _run_task_with_retry(engine: Engine, state: SwarmState, goal: str,
                         task: dict, model: str, timeout: int,
                         retries: int) -> tuple[str, str, str]:
    """Failed or escalated attempts get retried with the failure fed back to
    a FRESH agent in a FRESH worktree off the CURRENT tip."""
    tid, verdict, detail = _run_task(engine, state, goal, task, model, timeout)
    attempt = 1
    while verdict in ("failed", "escalated") and attempt <= retries:
        attempt += 1
        state.update(tid, "working", detail=f"retry {attempt}: {detail[:80]}")
        retry_task = dict(task, brief=(
            f"{task['brief']}\n\nPREVIOUS ATTEMPT FAILED: {detail[:300]}. "
            "Address that failure explicitly and complete the task."))
        tid, verdict, detail = _run_task(engine, state, goal, retry_task,
                                         model, timeout)
        detail = f"{detail} (attempt {attempt})"
        state.update(tid, "landed" if verdict == "landed" else verdict,
                     detail=detail)
    return (tid, verdict, detail)


def run_swarm(args) -> int:
    engine = Engine(args.repo)
    state = SwarmState(engine.state_dir)
    state.start(args.goal)

    listing = _repo_listing(engine.repo)
    call = lambda p: _claude_plan(p, args.planner_model)  # noqa: E731
    try:
        tasks = plan_tasks(args.goal, listing, args.agents, call)
    except Exception as exc:  # noqa: BLE001
        print(f"planning failed: {exc}")
        state.finish()
        return 1

    if not tasks:
        print("planner produced no valid tasks")
        state.finish()
        return 1

    print(f"swarm: {len(tasks)} task(s) planned for goal: {args.goal}")
    for t in tasks:
        print(f"  [{t['id']}] {t['title']}  ({', '.join(t['paths'])})")

    if getattr(args, "dry_run", False):
        print("\n(dry run — nothing executed)")
        state.finish()
        return 0

    retries = getattr(args, "retries", 1)
    results: list[tuple[str, str, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.agents) as pool:
        futures = {pool.submit(_run_task_with_retry, engine, state, args.goal,
                               t, args.model, args.timeout, retries): t
                   for t in tasks}
        for fut in concurrent.futures.as_completed(futures):
            results.append(fut.result())

    state.finish()

    results.sort(key=lambda r: r[0])
    print("\n== swarm ==================================================")
    print(f"{'task':24} {'verdict':10} detail")
    for tid, verdict, detail in results:
        print(f"{tid[:24]:24} {verdict:10} {detail[:60]}")

    tip = engine.status(limit=1)
    print(f"tip: {tip['tip'][:12]} on {tip['branch']}")

    landed = sum(1 for _, v, _ in results if v == "landed")
    print(f"landed {landed}/{len(results)}")
    return 0 if landed == len(results) else 1
