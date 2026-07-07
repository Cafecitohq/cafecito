#!/usr/bin/env python3
"""Agent-generated conflict corpus.

Human OSS repos under-produce attributable conflicts because maintainers
coordinate socially before conflicts form and painful branches never merge.
Agent fleets have neither property. This script measures that directly:

  1. TASK GEN — for each target file, a model drafts N realistic, independent
     backlog tasks scoped to that file (the kind a fleet operator would fan
     out to N agents in one sprint).
  2. FLEET RUN — each task goes to a fresh headless agent in its own detached
     worktree at the SAME base commit, with edit-only tools and no knowledge
     of the other agents. Non-empty diffs are committed to `cafecito/agent-*`
     branches with the task brief as the commit message — so downstream
     intents are REAL intents, not commit-message proxies.
  3. ANALYSIS — every changeset pair gets the experiment A treatment
     (same-base merge-tree + symbol write sets; attribution is trivial since
     all pairs share one base). Conflicting pairs are written in
     find_conflicts.py's schema, so experiment_b.py and validate_b.py run on
     them UNCHANGED via --results workdir/results/agent.

Deliberate bias, stated up front: tasks are concentrated on hotspot files to
measure conflict *behavior* under contention, not fleet-wide conflict *rates*.
The human corpus answers "how often"; this corpus answers "what happens when".

Usage:
  python3 agent_corpus.py --repo workdir/repos/sympy \
      --targets sympy/core/sympify.py sympy/core/cache.py \
      --tasks-per-target 4 [--model sonnet] [--workers 3] [--reuse]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import pathlib
import re
import subprocess
import sys
import time

from gitutil import git, git_rc, show
from oracle import write_set

TASK_GEN_PROMPT = """\
You are drafting a sprint backlog for the file `{path}` in the {repo} project. \
Below is the file (possibly truncated). Produce exactly {n} small, independent, \
realistic engineering tasks for THIS file, the kind a team lead would fan out to \
{n} different engineers in the same sprint. Mix improvement kinds (docstrings, \
error messages, input validation, small refactors, tiny helpers). Each task must \
be completable by changing ONLY this file (plus its test file if needed), touch \
at most ~40 lines, add no dependencies, and keep all existing tests passing.
Output STRICT JSON, nothing else: [{{"title": "...", "brief": "2-4 sentences"}}, ...]

FILE `{path}`:
{content}
"""

AGENT_PROMPT = """\
You are a coding agent in a fleet, working alone in this repository checkout.

TASK: {title}
{brief}

Rules: change ONLY `{target}` (and `{test_file}` if the task genuinely needs a \
test). Keep the diff small and focused (roughly 40 changed lines max), match the \
existing style exactly, do not reformat unrelated code, add no dependencies. Do \
not run tests or shell commands. Make the edits, then stop and summarize what \
you changed in one sentence.
"""


def claude_p(prompt: str, model: str, timeout: int, cwd: str | None = None,
             agent_tools: bool = False) -> str:
    cmd = ["claude", "-p", prompt, "--model", model]
    if agent_tools:
        cmd += ["--permission-mode", "acceptEdits",
                "--allowedTools", "Edit,Write,Read,Grep,Glob", "--max-turns", "25"]
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"claude failed: {r.stderr.strip()[:300]}")
    return r.stdout


def test_file_for(target: str) -> str:
    p = pathlib.Path(target)
    return str(p.parent / "tests" / f"test_{p.stem}.py")


def gen_tasks(repo: str, base: str, target: str, n: int, model: str) -> list[dict]:
    content = show(repo, base, target) or ""
    lines = content.splitlines()[:400]
    body = "\n".join(lines) + ("\n… (truncated)" if len(content.splitlines()) > 400 else "")
    out = claude_p(TASK_GEN_PROMPT.format(path=target, repo=pathlib.Path(repo).name,
                                          n=n, content=body), model, 240)
    m = re.search(r"\[.*\]", out, re.DOTALL)
    if not m:
        raise RuntimeError(f"task gen for {target}: no JSON in output")
    tasks = json.loads(m.group(0))[:n]
    for t in tasks:
        t["target"] = target
    return tasks


def run_agent(repo: str, base: str, idx: int, task: dict, model: str,
              timeout: int) -> dict:
    """One fleet member: worktree at base → agent edits → commit → branch ref."""
    branch = f"cafecito/agent-{idx}"
    wt = pathlib.Path(repo).parent / f".agent-wt-{idx}"
    git_rc(repo, "branch", "-D", branch)
    git(repo, "worktree", "add", "--quiet", "-b", branch, str(wt), base)
    rec = {"idx": idx, **task, "head": None, "files": [], "note": ""}
    try:
        t0 = time.time()
        try:
            claude_p(AGENT_PROMPT.format(title=task["title"], brief=task["brief"],
                                         target=task["target"],
                                         test_file=test_file_for(task["target"])),
                     model, timeout, cwd=str(wt), agent_tools=True)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            rec["note"] = f"agent error: {str(e)[:150]}"
            return rec
        rec["elapsed_s"] = round(time.time() - t0, 1)
        code, status, _ = git_rc(str(wt), "status", "--porcelain")
        if code != 0 or not status.strip():
            rec["note"] = "no changes produced"
            return rec
        git(str(wt), "add", "-A")
        git(str(wt), "-c", "user.name=cafecito-agent",
            "-c", "user.email=agent@cafecito.local",
            "commit", "-q", "-m", f"{task['title']}\n\n{task['brief']}")
        rec["head"] = git(str(wt), "rev-parse", "HEAD").strip()
        rec["files"] = git(str(wt), "diff", "--name-only", base, "HEAD").splitlines()
    finally:
        git_rc(repo, "worktree", "remove", "--force", str(wt))
    return rec


def analyze(repo: str, base: str, changesets: list[dict], out_dir: pathlib.Path,
            name: str) -> None:
    ws = {c["head"]: write_set(repo, base, c["head"]) for c in changesets}
    rows, conflicts = [], []
    for i, a in enumerate(changesets):
        for b in changesets[i + 1:]:
            code, out, _ = git_rc(repo, "merge-tree", "--write-tree", "--name-only",
                                  "--no-messages", f"--merge-base={base}",
                                  a["head"], b["head"])
            if code not in (0, 1):
                continue
            sym_a, files_a = ws[a["head"]]
            sym_b, files_b = ws[b["head"]]
            row = {
                "a": {"head": a["head"], "base": base, "merge": a["head"],
                      "subject": a["title"]},
                "b": {"head": b["head"], "base": base, "merge": b["head"],
                      "subject": b["title"]},
                "textual_conflict": code == 1,
                "file_overlap": sorted(files_a & files_b),
                "symbol_overlap": sorted(sym_a & sym_b),
            }
            rows.append(row)
            if code == 1:
                conflicts.append({**row,
                    "sim": {"ours": a["head"], "theirs": b["head"], "base": base,
                            "replayed": a["head"]},
                    "conflicted": sorted({p for p in out.splitlines()[1:] if p})})

    n = len(rows) or 1
    sym_dis = sum(1 for r in rows if not r["symbol_overlap"])
    file_dis = sum(1 for r in rows if not r["file_overlap"])
    textual = sum(1 for r in rows if r["textual_conflict"])
    silent = sum(1 for r in rows if not r["textual_conflict"] and r["symbol_overlap"])
    print(f"""
== cafecito phase 0 · agent corpus · {name} ==============================
agents with usable changesets:  {len(changesets)}
changeset pairs (same base):    {len(rows)}

symbol-disjoint (would land in parallel):  {100*sym_dis/n:5.1f}%
file-disjoint:                             {100*file_dis/n:5.1f}%
textual conflict (git 3-way fails):        {100*textual/n:5.1f}%
silent risk (clean merge, symbols collide):{100*silent/n:5.1f}%
(hotspot-biased by design — measures behavior under contention, not fleet rates)
==========================================================================""")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{name}_pairs.json").write_text(json.dumps(rows, indent=1))
    (out_dir / f"{name}_a.json").write_text(json.dumps(
        {"repo": name, "since": f"agent-corpus@{base[:12]}", "pairs": conflicts,
         "summary": {"n": len(rows), "textual_conflict": textual,
                     "symbol_disjoint": sym_dis}}, indent=1))
    print(f"[{name}] {textual} conflicting pairs → {out_dir / f'{name}_a.json'}\n"
          f"next: experiment_b.py --repo <repo> --results {out_dir}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--targets", nargs="+", required=True)
    ap.add_argument("--tasks-per-target", type=int, default=4)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--agent-timeout", type=int, default=900)
    ap.add_argument("--reuse", action="store_true",
                    help="skip task-gen/fleet phases if state files exist")
    ap.add_argument("--include", nargs="*", default=[],
                    help="changesets.json files from earlier runs at the SAME base "
                         "commit to fold into the analysis (dedup by head sha)")
    args = ap.parse_args()

    repo = str(pathlib.Path(args.repo).resolve())
    name = pathlib.Path(repo).name
    here = pathlib.Path(__file__).parent
    state_dir = here / "workdir" / "agent" / name
    state_dir.mkdir(parents=True, exist_ok=True)
    out_dir = here / "workdir" / "results" / "agent"
    base = git(repo, "rev-parse", "HEAD").strip()

    tasks_file = state_dir / "tasks.json"
    if args.reuse and tasks_file.exists():
        tasks = json.loads(tasks_file.read_text())
    else:
        tasks = []
        for t in args.targets:
            print(f"[{name}] generating {args.tasks_per_target} tasks for {t} …",
                  file=sys.stderr)
            tasks += gen_tasks(repo, base, t, args.tasks_per_target, args.model)
        tasks_file.write_text(json.dumps(tasks, indent=1))
    print(f"[{name}] {len(tasks)} tasks", file=sys.stderr)

    cs_file = state_dir / "changesets.json"
    if args.reuse and cs_file.exists():
        changesets = json.loads(cs_file.read_text())
    else:
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(run_agent, repo, base, i, t, args.model,
                              args.agent_timeout): i for i, t in enumerate(tasks)}
            for fut in concurrent.futures.as_completed(futs):
                rec = fut.result()
                state = "ok" if rec["head"] else f"SKIP ({rec['note']})"
                print(f"[{name}] agent-{rec['idx']} {state}: {rec['title'][:60]}",
                      file=sys.stderr)
                results.append(rec)
        results.sort(key=lambda r: r["idx"])
        cs_file.write_text(json.dumps(results, indent=1))
        changesets = results
    for extra in args.include:
        changesets += json.loads(pathlib.Path(extra).read_text())
    changesets = [c for c in changesets if c.get("head")]
    seen: set[str] = set()
    changesets = [c for c in changesets
                  if not (c["head"] in seen or seen.add(c["head"]))]
    if len(changesets) < 2:
        print("not enough usable changesets", file=sys.stderr)
        return 1

    analyze(repo, base, changesets, out_dir, name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
