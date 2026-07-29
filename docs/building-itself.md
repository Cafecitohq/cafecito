# We asked cafecito to build its own features. In parallel.

*Published 2026-07-10 · revised 2026-07-29: brought current to v0.18.0, with new
split-screen footage and six claims from the original corrected. The first version said
collisions on our own repo were regenerated live, and it claimed zero escalations. Neither
was true, and the log always said so. (The old headline called cafecito a "merge plane,"
which breaks the vocabulary rule this post spends three thousand words enforcing. That is
fixed too.) See [What the log actually says](#what-the-log-actually-says).*

*What you can check, and what you can't. Public: the source, the tags, the CI workflow, the
closed PR — and the landings themselves, because every landing since 6 July is a commit on
the public `cafecito/main` branch, created by the engine and carrying `Signed-off-by:
cafecito-engine` (the git author stays the maintainer's identity). All but the first four
also carry a `Changeset-Id` trailer, which shipped in the fifth landing: `git log
cafecito/main --grep=Changeset-Id` counts 46 of the 50. Not public: the landed log.
`.cafecito/` is line 5 of our `.gitignore`, so gate times, escalations, inherited facts and
no-signal flags are local state on our machine. Every number below that comes from that file
is labelled as coming from the log, and the only way to check that class of claim is to run
this on your own repo and read your own.*

---

We launched [cafecito](https://cafeci.to) on 7 July — an integration control plane for AI
agent fleets. It exists because your AI agents write code faster than they can merge it:
point several at one repository and the first one to land forces the rest to rebuild on top
of it — pull the new code, re-run the tests, get back in line. The bet is that almost all of
that waiting buys nothing. Work that touches different code should land at the same time; we
call that **commuting**. Work that genuinely overlaps should be rewritten from both sides'
intent rather than diffed — **regenerated**. Only contradictions should reach a person —
**escalated**.

Then we did the only honest thing you can do with a thesis like that: **we stopped merging
our own code.** Since the v0.1 sprint, every feature of cafecito has been landed *through
cafecito* — with one early exception we count in full below — the part that works out which
symbols a change actually writes, the reconciler that rewrites two overlapping changes from
both intents, the rule that lockfiles and other generated files are regenerated from their
manifests rather than merged as text, the cache that lets a re-gate skip tests whose inputs
did not change, gates that run in parallel waves, the swarm, the PR gateway, one-command
setup.

Counted on 29 July, at log entry 84: 52 submissions, of which **50 landed and 2 escalated**,
plus 32 tip advances. `git log cafecito/main` is the changelog. The suite at the tip is **185
tests across 25 files**, up from 67 test functions in 8 files at v0.2.0 (`git grep -c 'def
test_' v0.2.0 -- 'cafecito/tests/test_*.py'`), all of them landed by the machinery they test.
This revision has to land too, which will make it 85 entries and 51 landings.

The footage below is the system working. The rest of this post is the four times it broke,
or caught something that was already broken. Each one turned into a shipped feature, and
that half is the more useful half.

## Thirty-five seconds of it working

![cafecito swarm and watch, split-screen — a real fleet lands while the dashboard streams it](https://raw.githubusercontent.com/cafecitohq/cafecito/main/examples/swarm-split.gif)

One tmux frame, two panes. On the left, `cafecito swarm` takes one sentence, has a planner
decompose it into independent tasks, and runs three real coding agents in parallel
worktrees. On the right, `cafecito watch` streams the fleet live: which task holds which
advisory lease — leases warn, they never lock — which changeset is in the gate, what landed.
It ends with three gated landings, a landed branch of ordinary git commits, and a green suite
at the tip.

Nothing is simulated — real `claude` CLI workers, real gates, real landings. Two things to
be straight about. That is screen time, not run time: thirty-five seconds is the recording
with idle trimmed, and the live run takes two to four minutes. And the repo in the frame is
a fixture the script builds from scratch — a small coffee shop app — not cafecito itself.
Reproduce it: `./examples/demo_swarm_split.sh`.

## Two agents, two changesets, commuting

For v0.4.0 we needed two features at once — `cafecito swarm` (one goal in, a parallel fleet
out) and `cafecito watch` (the dashboard above). So we ran the experiment on ourselves: one
scaffold changeset defining the shared contract, then **two coding agents in parallel
worktrees off the same tip**, each building its module, forbidden from touching shared
files.

The first agent's changeset landed. The second — authored against the *pre-first* tip —
landed on the *post-first* tip with no reconciliation: its log entry records `regen_s: null`,
no regeneration time, because there was nothing to reconcile. Nobody rebuilt on top and got
back in line, and no human touched it. Two agents' work, provably disjoint, commuting through
the product they were building. That is the whole thesis in one log entry, and the two
landings are 48 seconds apart.

The three landings are public commits — the scaffold at `6071b910`, swarm at `dd40899d`,
watch at `7947da6b`, each parented on the one before. The two worker commits are not; they
lived in throwaway worktrees. The log records the worker shas; git records their parent —
both worker commits name `6071b910` as parent, but they live on no branch, so you can only
check that if you have our object store. Take the ordering from the public commits and the
parentage from us.

## Failure one: we shipped two broken releases

Honesty is the house style, so: **v0.5.0 and v0.5.1 are broken releases**, and the story of
why is the most useful thing in this post.

Building wave-parallel gates, a refactor spliced a region of the engine — and silently
swallowed the neighboring `advance()` method, the one that moves the plane's tip when a
commit reaches the deploy branch without landing. The landing gate let it through, because
**no test had ever pinned `advance`**. Our own doctrine — *the gate only catches what tests
pin* — held perfectly. Our coverage had a hole, and the doctrine walked right through it.

It compounded beautifully: `advance` was exactly what our release process used to keep the
landed branch synced with those out-of-band commits, so the release batch failed *silently
at that step*, the branches diverged, and v0.5.1 was tagged **without containing its own
fix**. That last one you can check without our log: `git merge-base --is-ancestor $(git
rev-list -n1 v0.5.2) $(git rev-list -n1 v0.5.1)` returns false. In the log, the fingerprint
is an absence — there is a tip advance for every release version bump we have ever made
except three: v0.5.0 and v0.5.1, where the method that writes those entries had been
deleted, and v0.15.0, which needed no advance because its bump landed through the plane.

Recovery: one plain git merge commit to bring the diverged branches back together (no history
rewrites on a public repo), regression tests for everything the splice could have touched,
both bad tags left immutable, v0.5.2 as the superseding release, and two new rules in the
book — grep a splice for every `def` it spans, and a release isn't done until every branch
head is verified identical. The second rule is written down where our own agents read it, in
[CLAUDE.md](../CLAUDE.md): a session that touches `main` ends with `main`, `cafecito/main`
and both origin refs at one sha, or says out loud why not. v0.15.0 later turned it into a CI
job that reddens by itself.

If you're evaluating tools like ours, ask every vendor for this story. A landing gate is a
property of your *test suite*, not of anyone's engine — including ours.

## Failure two: the first sandboxed gate refused the sandbox

v0.11.0 gave the gate isolation backends — `sandbox` wraps every test invocation in macOS
`sandbox-exec` (network denied except unix sockets, which test runners need; writes confined
to the gate's own worktree), `container` does the same with docker/podman. Naturally we
flipped our own plane to `sandbox` **before** landing the feature, so the changeset that
implements the boundary had to land through it.

It escalated. Twice — and those two attempts are still the only escalations in the log.

Inside the sandbox the gate confines `TMPDIR` to its own scratch directory, which put
pytest's temp tree under a path containing `cafecito-`, and one of our own tests asserted
that string never appears in `git worktree list` output. The log's first escalation records
the verdict and the reason: `failed landing gate`, with
`test_doctor.py::test_gc_cleans_leases_inflight_and_worktrees`. An over-broad assertion that
had been latently wrong for anyone running tests with an unlucky `TMPDIR`, surfaced by the
first sandboxed gate that ever ran it. The second submission failed
`test_isolation.py::test_unisolated_gate_fails_the_probe` and taught the counterpart lesson:
tests that *prove the absence* of a boundary (connect refused is not connect denied) must
probe their environment first, because inside a sandboxed gate the boundary is already
there. Both fixes landed in the same changeset — verdict `landed`, gate 0.66s, two
verification facts inherited from the escalated attempt, main green.

That is what an escalation is supposed to look like. The gate did not pick a side, did not
paper anything over, and did not let a green-looking changeset through: it handed back a
failing test name and waited. Our plane has been set to `sandbox` ever since. That switch
lives in `.cafecito/config.json`, which attests to today's setting and not to what each gate
actually ran under — the log does not record isolation mode. What is public is the mechanism,
in [`cafecito/isolation.py`](../cafecito/isolation.py) and [SECURITY.md](../SECURITY.md), and
`cafecito doctor` will tell you which backend your own plane is using.

## Failure three: a repo of ours drifted out of its own plane

On 21 July we set up a plane in a second repo of ours, an app we build with the same fleet.
Nineteen minutes later a human ran `advance` and watched it swallow three commits that had
never touched the plane. No landing, no gate, no log entry. Nothing was broken and nothing
was noisy — the tip had simply stopped being the truth.

The cause was where the plane was registered. A local-scope `claude mcp add` writes into one
client's config, keyed to one absolute directory on one machine. The parallel agent sessions
ran in worktrees one directory over, whose config had no cafecito server in it at all — so
they had no `submit` tool, and an agent with no `submit` tool commits. The reason is now
written in our source rather than in a commit message: local scope "binds to one directory
on one machine, which is how sessions end up silently committing around the plane."

Three fixes, in the order they were needed:

- **Registration that travels with the repo.** A checked-in `.mcp.json` with a
  path-relative `--repo .`, so every clone, worktree and teammate finds the same plane
  instead of depending on one machine's client state.
- **A detector, because the failure was silent.** A CI job that fetches the landed branch
  and fails when it trails the deploy branch: `cafecito/main trails main — commits bypassed
  the plane`, with the recovery command printed in the error
  ([`.github/workflows/ci.yml`](../.github/workflows/ci.yml)).
- **A hook, so a bypass strands nothing.** A post-commit hook that advances the tip when a
  commit reaches main without the plane — a maintainer on main, a web edit pulled down, an
  agent session with no MCP.

v0.15.0 turned all three into `cafecito init`, one command from a bare repo to a working
plane, with gate detection that scores pytest/npm/go by test-file count rather than guessing,
and cargo detected by manifest as a last-resort fallback. v0.16.0 added `--ci`, which
scaffolds the workflow and generates the test job *from the plane's own config*, so the gate
CI runs is the gate agents land on and the two cannot drift apart. One grace note: v0.15.0 is
the only release whose version bump landed through the plane — `git rev-list -n1 v0.15.0`
points at a commit the engine created, carrying `Changeset-Id: cs_7740115fa0`. cafecito gated
and landed its own release.

Two caveats, because this is the weakest-evidenced section in the post. The incident was in
a private repo, so it is the one story here you cannot check; what is public is the
mechanism, in [`cafecito/onboard.py`](../cafecito/onboard.py) and the README, and the fixes,
which are commits. And we never went back and ran `init` there. That repo's main now sits 80
commits ahead of its plane. Three commits of drift wasn't an anomaly — it was the first three
of eighty, and it kept growing for exactly as long as none of the mechanisms above were
installed. It is the counterfactual for this whole section, and it is ours.

## Failure four: no model has ever heard of us

The fourth one took six releases to notice, and it closes the loop the first three left
open.

Everything above made the plane *reachable* from any clone. None of it made an agent *know
it should use it*. cafecito postdates every model's training cutoff, so a session can learn
the plane exists from exactly two places: the MCP tool descriptions, and the instructions
file it already reads. The tools say how to land. Nothing said that you should — and an agent
that doesn't know it should land will happily `git commit`, which strands the tip and leaves
the next landing building on history the plane never saw.

So v0.18.0 made `cafecito init` write documentation. It appends a landing stanza to
`CLAUDE.md` *and* `AGENTS.md` — both, because a plane that works for only one vendor's agent
is not a plane — delimited by markers, so a re-run is a no-op and your edits inside the block
survive. `cafecito doctor` warns when the stanza is missing. The load-bearing line is the
last bullet in the generated text: *if the cafecito tools are not available in this session,
say so and stop rather than committing around the plane.* The tool asks agents to refuse to
work around it.

Then we ran it on ourselves and found the joke. Our own `CLAUDE.md` documented the
vocabulary, the stdlib-only rule, the gate rule, sign-off, the four-heads push invariant and
the release ritual — and never once told a session to call `submit`. The repo that invented
landing had no instruction to land. The stanza went in as part of the same changeset, with
one repo-specific clause: the maintainer release ritual is the single sanctioned bypass, and
feature work still goes through `submit`.

The bypass problem is measured, not imagined. The log carries 32 tip advances — the hook
catching up after a commit reached main outside the plane. Seventeen are the release version
bumps our own docs sanction. The other fifteen are not; among them: the site's front page,
the launch post, LAUNCH.md, two site catch-ups, an untracked build artifact, the phase0
oracle validation, the domain-strategy commit, the demo recording — and, least defensibly,
two commits that added engine features straight to main: the v0.1 product sprint itself, and
an early commit after it that added a `cafecito sync` CLI command. Every one of the 32 is a
tip movement no gate ran on. That is the same drift story we tell about someone else's repo
in failure three, and it is worth counting exactly rather than saying "several."

The general form, for anyone shipping developer infrastructure right now: your product is
invisible to every model until the repository itself explains it. Onboarding has to write
documentation, not just configuration.

## Two smaller ones, since we're counting

**Our own parallelism found a bug inside our own optimization, on the night of the first
PyPI release.** Verification facts are what make gating cheap — a content-addressed record
that a given test passed against a given set of input files, so a re-gate re-runs only what
changed. The store was written to tolerate concurrent writers: gates write through
`os.replace`, and a lost update was declared acceptable, because facts are an optimization
and never correctness. That reasoning was right about updates and wrong about files. Every
writer used the *same* temporary filename, so one gate's replace could move the file out from
under another's, and the losing gate died with `FileNotFoundError`. Not a lost fact — a dead
gate. It was caught in the wild rather than in review; the regression test carries the
sighting date in a comment and hammers the store with eight threads and a hundred writes
each, asserting both no errors and no orphaned temp files. The fix is four lines plus one
sentence of doctrine appended to the module docstring: **lost updates are acceptable, lost
gates are not.** ([`cafecito/facts.py`](../cafecito/facts.py),
[`test_facts.py`](../cafecito/tests/test_facts.py))

**And once we shipped a safety feature that re-serialized the product for two hours.**
v0.13.0 added drift containment to the swarm: when a worker edits files beyond its
assignment, nobody has leased those files, so reserve them immediately and turn a
landing-time collision into an intent-time wait. Sound reasoning, wrong granularity — those
leases were whole-file keys, and file-level locking is exactly how a parallel system becomes
a serial one. A sibling editing a *different function in the same file* commutes and should
never have contended. v0.14.0, two hours and fifteen minutes later, derived the keys from the
same symbol-level write-set analysis the engine already used, guarded by the rule that any
analysis failure or unreadable file widens back to the whole-file key: uncertainty never
narrows. The fix was free because the analysis already existed; the swarm just hadn't been
asking for it. The generalizable part is that locks added for safety default to the coarsest
granularity available, and the coarse default stays invisible until you already own a finer
lens and notice you didn't use it.

## What else shipped

- **v0.7.0, the PR gateway.** `cafecito ingest` keeps your normal GitHub flow: open PRs, and
  a poller lands them through the plane, then reports the verdict back as a comment and a
  label. Fork PRs included; re-pushed heads re-ingested; never auto-closed. Its first real
  run is public — [PR #1](https://github.com/Cafecitohq/cafecito/pull/1) on our own repo,
  opened, ingested, landed as an engine-created commit, labeled `cafecito:landed`,
  commented with the receipt, closed by a human. Read that receipt closely and it says `gate:
  no test signal`. It was a README change; nothing in the suite was implicated; the gate ran
  and collected nothing. The feature's first production input was the pull request that
  documents it, and it landed unverified.
- **v0.8.0, retry with gate feedback.** When a regenerated changeset fails the gate, the
  failing output is fed back into a second regeneration attempt rather than going straight to
  a human ([`test_regen_retry.py`](../cafecito/tests/test_regen_retry.py)). Like the
  reconciler it sits on top of, it has never had cause to fire on this repo.
- **v0.12.0, write sets beyond Python.** Symbol extraction and input tracking for
  TypeScript/JavaScript and Go, stdlib-only, on one soundness rule: over-inclusion is always
  safe, because a spurious entry merely re-runs a test, and omission never is. The scanners
  refuse to see through tsconfig path aliases, bundler aliases, package workspaces,
  vendoring, `go:embed` and cgo — each returns "I don't know", and the test simply runs.
- **v0.14.1, the first PyPI release,** so `pipx install cafecito` works. The interesting
  change in it was the facts race above, which landed ten minutes earlier.
- **v0.17.0, no code at all.** A version number spent so pypi.org would pick up a rewritten
  README and launch post, after real prospective users told us the old copy was
  unintelligible. Your PyPI front page is frozen at your last release, so a pure docs fix
  costs a release. That pass also caught two stale claims sitting on the front door: the
  README advertised four MCP tools when `swarm` had made it five, and the sample `init`
  output still printed `cafecito 0.15.0` when 0.16.0 was current.

## What the log actually says

Everything in this section comes from `.cafecito/log.jsonl`, the file you cannot see, except
the one row that says otherwise. Counts are a snapshot of a file that moves several times a
week — this one is as of 29 July 2026, at log entry 84, and this revision's own landing will
make it 85 entries and 51 landings.

| | |
|---|---|
| log entries | 84 |
| submissions | 52 |
| **landed** | **50** |
| escalated to a human | 2 — the same changeset, twice, on its way in |
| **regenerated** | **0** |
| out-of-band commits absorbed (tip advances) | 32 — 17 sanctioned version bumps, 15 not |
| landings that ran tests | 30 — median **0.9s**, slowest 6.1s |
| landings with no test signal | 20 — 14 docs-only, 6 not |
| verification facts inherited, all time | 2 |
| suite at the tip | 185 tests across 25 files |
| gate isolation (from `.cafecito/config.json`, not the log — the log does not record isolation mode) | `sandbox` since v0.11.0 |

The row to argue with is the zero.

**Regeneration has never fired here.** No landing has a regeneration time recorded, none is
flagged regenerated, and there is not one conflicted file anywhere in the log. Every landing
either commuted or merged cleanly as text and was gated anyway. That is one honest sentence
with two halves, and both matter:

- It **is** evidence for the commutativity thesis: three weeks of real parallel feature work
  by agent sessions on one small codebase produced not a single overlap the engine had to
  reconcile.
- It is **not** evidence that regeneration works. Our own repo has never exercised the
  reconciler in anger. That evidence is elsewhere and checkable: 14 of 16 validatable real
  conflicts in [phase0](https://github.com/cafecitohq/cafecito/tree/main/phase0) dissolved
  with *both* sides' suites green; 7 of the 30 automatic landings in
  [the benchmark](https://github.com/cafecitohq/cafecito/tree/main/bench) regenerated live
  against an accumulated main; and you can watch a reconciler call happen in the recorded
  [`examples/demo.sh`](../examples/demo.sh) run.

**The gate runs on every landing; tests don't always.** Every landing goes through the gate,
textually clean merges included — we enforced that after our benchmark landed two red mains
without it, once through conflict markers in an uncovered file and once through an ungated
clean merge flipping behavior under an already-landed test. When a changeset implicates
tests, they really run: 30 of the 50 landings executed tests, median 0.9 seconds. The other
20 recorded `no test signal` rather than a fake pass — and here the flattering version of
the sentence is the false one. Fourteen of those 20 were documentation. The other six were
not: the scaffold that defined the swarm/watch contract (`cafecito/cli.py`,
`cafecito/fleetstate.py` — new modules nothing tested yet), an early engine hotfix, the CI
workflow, the benchmark harness, the PyPI publishing workflow with `pyproject.toml`, and the
demo scripts. Two files of the shipped package landed with nothing run against them. A gate
with no signal verifies nothing, and we would rather say that than average it into a 96%
green rate. The v0.5.0 saga taught the same lesson from the other direction: the gate is
exactly as strong as your tests.

**Fact inheritance has barely paid off here, for a structural reason.** Each gate re-runs
only the tests a change could affect, and skips a test entirely when it has already seen
that exact test pass against that exact set of input files, hashed by content. Across every
gate this repo has ever run, that has happened twice — both on the retry of the escalated
sandbox changeset. Nearly every changeset here touches the engine, so almost no test's
inputs survive intact from one landing to the next. We extended that input tracking to
TypeScript, JavaScript and Go for an ecosystem our own stdlib-only Python repo is the worst
possible case for demonstrating. The race counter for concurrent re-landings is instrumented
and has never had cause to fire here either.

**The two escalations were the system working,** and they were the sandbox story above — the
gate refusing the isolation feature it now runs inside until that same changeset fixed a
latently wrong assertion in our own tests. Nothing here has ever escalated because two
changesets contradicted each other. That fires in the benchmark corpus, three times: once
where two agents' tests assert contradictory exact `__repr__` formats, so no version of the
code satisfies both, and twice where a regeneration failed to preserve an acceptance test by
name and the shadowing guard refused it.

The 10 July version of this post got six things wrong. They are corrected here rather than
quietly deleted, because a post whose entire bet is *check this against the repo* does not
get to fail that check. Two were substantive: it said collisions on this repo "were
regenerated (live, against the accumulated tip)", which was never true, and it claimed "Zero
escalations" three paragraphs after narrating both of them. Four more were smaller, and none
of them trivia: its byline promised the landed log was verifiable in the repo, when
`.cafecito/` has been gitignored the whole time; it said every landing, textually clean
merges included, runs real tests — true of the gate, false of the tests, 20 times out of 50;
it said the log shows `raced` counters on real landings, when that counter has never had
cause to fire here; and it cited a log field, `regenerated: false`, that does not exist (the
key is `regen_s`). In both substantive cases the true version is the stronger claim. The most
recent landing in the table above is the docs changeset that scrubbed the same false claim
off the rest of the site.

## Try it on your repo

```sh
pipx install cafecito
cd your-repo && cafecito init                  # detects your gate, registers the plane
cafecito init --ci                             # optional: CI gate + the drift guard
cafecito swarm "your goal here" --agents 3     # or: cafecito ingest, for your open PRs
cafecito watch                                 # and watch it land
```

`init` also writes the landing stanza into `CLAUDE.md` and `AGENTS.md`, because otherwise
your agents have no way to know any of this exists. `cafecito doctor` re-checks the lot,
including whether your gate can collect tests at all — a gate that collects nothing lands
everything unverified.

Numbers, methodology and the honesty boxes live in the repo:
[the benchmark](https://github.com/cafecitohq/cafecito/tree/main/bench) (including a
generously-modeled speculative-queue baseline) and
[the experiments](https://github.com/cafecitohq/cafecito/tree/main/phase0). The full argument
is in [the launch post](launch-post.md).

## Honesty box

The landed log is not published — `.cafecito/` is gitignored — so every count in the table
above is ours to assert and yours to reproduce. Regeneration has never fired on this repo,
so nothing in this post demonstrates it; the corpora do, and they are small (n=16 validated
conflicts — genuine ones being rare is itself the finding). Twenty of our 50 landings had no
test signal at all, which means the gate verified nothing on those, and six of them were not
documentation: one shipped two files of the package itself. A self-hosted log is also a
sample of one repository's habits — our changesets are disjoint partly because a small team
with leases plans them that way. The drift incident in failure three is in a private repo
you cannot inspect, and that repo is still 80 commits out of sync because we never applied
our own fix to it. The `plane-sync` guard has never been observed reddening on a real
bypass; we know it works by reading it. Sandboxed gates are macOS today; the container
backend is built but not proven against a live runtime. cafecito is still a single-repo
control plane: webhooks and a hosted multi-repo app are not built. If you run this on your
repos and get different numbers, we want the data.

*— the cafecito authors · hello@cafeci.to*

*cafecito is Apache-2.0. The coffee is load-bearing.*