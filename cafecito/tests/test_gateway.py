"""The gateway service: the worker that lands a delivery, and `run_gateway`.

The receiver lives in test_webhook.py and the credential path in
test_ghapp.py — this file is the part that knows about both plus an engine,
and the seams it drives are pinned in test_ingest.py. Every test runs
offline: no network, no real GitHub, no `gh`, and the only sockets bound are
loopback.

test_ingest.py's tests pass unmodified through the extraction of `ingest_pr`
— that is the proof the extraction changed no behaviour — with exactly one
deliberate exception, documented there: an unfetchable head is a retryable
state now, not a permanent rejection.
"""

import hashlib
import hmac
import http.client
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import types

import pytest

from cafecito import gateway, ghapp, ingest, webhook
from cafecito.engine import DEFAULT_CONFIG, Engine
from cafecito.ingest import IngestState

SECRET = b"0123456789abcdef0123456789abcdef"
SLUG = "acme/widget"
INSTALL = 12345678


# -------------------------------------------------------------- fixtures ---

@pytest.fixture()
def repo(tmp_path):
    """A real git repo one level down: the gateway refuses a private key
    inside the worktree, so the tests need somewhere outside it."""
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "mod.py").write_text("x = 1\n")
    (root / "tests" / "test_mod.py").write_text(
        "def test_ok():\n    assert True\n")

    def sh(*args):
        subprocess.run(args, cwd=root, check=True, capture_output=True)

    sh("git", "init", "-q", "-b", "main")
    sh("git", "add", "-A")
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
       "-m", "i")
    return root


def pr_branch(repo, name, content):
    def sh(*args):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)

    sh("git", "checkout", "-q", "-b", name, "main")
    (repo / "mod.py").write_text(content)
    sh("git", "add", "-A")
    sh("git", "-c", "user.name=pr", "-c", "user.email=p@p", "commit", "-q",
       "-m", name)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    sh("git", "checkout", "-q", "main")
    return head


def make_engine(repo):
    eng = Engine(str(repo))
    eng.config["test_cmd"] = [sys.executable, "-c", "pass"]
    return eng


def _which(tool):
    import shutil
    return shutil.which(tool)


@pytest.fixture()
def pem(tmp_path):
    """A throwaway 2048-bit key. Never a committed fixture: this repo is
    public, and a leaked App key is good forever."""
    if not _which("openssl"):
        pytest.skip("openssl not on PATH")
    p = tmp_path / "keys" / "app.pem"
    p.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["openssl", "genrsa", "-out", str(p), "2048"],
                   check=True, capture_output=True)
    p.chmod(0o600)
    return p


@pytest.fixture()
def secret_file(tmp_path):
    p = tmp_path / "secrets" / "webhook"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(SECRET + b"\n")
    p.chmod(0o600)
    return p


def _fake_tokens(values=("ghs_" + "A" * 30,)):
    it = iter(values)
    last = {"v": None}

    def token():
        try:
            last["v"] = next(it)
        except StopIteration:
            pass
        return last["v"]

    return types.SimpleNamespace(token=token)


def _delivery(number=7, **kw):
    return webhook.Delivery(delivery_id=kw.get("delivery_id", "d1"),
                            action=kw.get("action", "opened"), number=number,
                            slug=SLUG, installation_id=INSTALL,
                            is_fork=kw.get("is_fork", False),
                            source=kw.get("source", "webhook"))


def _set_config(repo, **over):
    """Rewrite the plane's config.json — run_gateway builds its own Engine."""
    eng = Engine(str(repo))
    (eng.state_dir / "config.json").write_text(
        json.dumps({**eng.config, **over}))


# ----------------------------------------------------------------- worker ---

def test_two_deliveries_for_one_head_produce_one_landing(repo, monkeypatch):
    eng = make_engine(repo)
    head = pr_branch(repo, "feature", "x = 2\n")
    comments = []
    monkeypatch.setattr(ingest, "_pr_view", lambda slug, n: {
        "number": n, "title": "bump x", "headRefOid": head})
    monkeypatch.setattr(ingest, "_comment",
                        lambda s, n, b: comments.append((n, b)))
    monkeypatch.setattr(ingest, "_label", lambda s, n, v: None)
    claims = IngestState(eng.state_dir)

    first = gateway._work(("land", _delivery()), eng, SLUG, _fake_tokens(),
                          claims, True)
    second = gateway._work(("land", _delivery(delivery_id="d2")), eng, SLUG,
                           _fake_tokens(), claims, True)
    assert first == "landed" and second == "duplicate"
    assert eng.status()["landed"] == 1
    assert len(comments) == 1


def test_two_workers_racing_one_pr_land_it_once(repo, monkeypatch):
    eng = make_engine(repo)
    head = pr_branch(repo, "feature", "x = 2\n")
    comments = []
    barrier = threading.Barrier(2, timeout=20)

    def view(slug, n):
        barrier.wait()
        return {"number": n, "title": "bump x", "headRefOid": head}

    monkeypatch.setattr(ingest, "_pr_view", view)
    monkeypatch.setattr(ingest, "_comment",
                        lambda s, n, b: comments.append((n, b)))
    monkeypatch.setattr(ingest, "_label", lambda s, n, v: None)
    claims = IngestState(eng.state_dir)
    out = {}

    def go(key):
        out[key] = gateway._work(("land", _delivery(delivery_id=key)), eng,
                                 SLUG, _fake_tokens(), claims, True)

    ta = threading.Thread(target=go, args=("a",))
    tb = threading.Thread(target=go, args=("b",))
    ta.start(); tb.start(); ta.join(60); tb.join(60)
    assert sorted(out.values()) == ["duplicate", "landed"], out
    assert eng.status()["landed"] == 1
    assert len(comments) == 1


def test_a_failed_landing_releases_its_claim_and_the_worker_survives(
        repo, monkeypatch):
    eng = make_engine(repo)
    head = pr_branch(repo, "feature", "x = 2\n")
    monkeypatch.setattr(ingest, "_pr_view", lambda slug, n: {
        "number": n, "title": "bump x", "headRefOid": head})
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("gh exploded with ghs_" + "C" * 30)
        return (7, "landed")

    monkeypatch.setattr(ingest, "ingest_pr", flaky)
    claims = IngestState(eng.state_dir)
    notes = []
    q = queue.Queue()
    q.put(("land", _delivery()))
    q.put(("land", _delivery(delivery_id="d2")))
    q.put(None)
    gateway._worker_loop(q, eng, SLUG, _fake_tokens(), claims, True,
                         note=notes.append)
    assert calls["n"] == 2                 # the claim was released, not stuck
    assert any("RuntimeError" in n for n in notes)
    assert not any("ghs_C" in n for n in notes)


def test_a_transient_failure_is_retried_and_not_a_verdict(repo, monkeypatch):
    """A fetch that failed because GitHub blinked is not a judgement on the
    PR. Recording it as `rejected` made the claim permanent, and the sweep —
    the only retry that exists, since GitHub never redelivers — could never
    get past it. The author is told once, not once per attempt."""
    eng = make_engine(repo)
    head = pr_branch(repo, "feature", "x = 2\n")
    monkeypatch.setattr(ingest, "_pr_view", lambda slug, n: {
        "number": n, "title": "bump x", "headRefOid": head})
    comments = []
    monkeypatch.setattr(ingest, "_comment",
                        lambda s, n, b: comments.append(b))
    monkeypatch.setattr(ingest, "_label", lambda s, n, v: None)
    fetches = {"n": 0}

    def flaky_fetch(repo_path, slug, number, sha):
        fetches["n"] += 1
        return fetches["n"] > 2

    monkeypatch.setattr(ingest, "_fetch_pr_head", flaky_fetch)
    claims = IngestState(eng.state_dir)
    work = ("land", _delivery())
    assert gateway._work(work, eng, SLUG, _fake_tokens(), claims,
                         True) == "unfetched"
    assert gateway._work(work, eng, SLUG, _fake_tokens(), claims,
                         True) == "unfetched"
    assert gateway._work(work, eng, SLUG, _fake_tokens(), claims,
                         True) == "landed"
    assert eng.status()["landed"] == 1
    assert len(comments) == 2                      # one notice, one verdict
    assert "could not fetch" in comments[0]
    assert "landed" in comments[1]


def test_an_admission_timeout_is_contention_not_a_verdict(repo, monkeypatch):
    """`admission timeout behind <agent>` says another changeset held the
    in-flight registration too long — nothing about this head."""
    eng = make_engine(repo)
    monkeypatch.setattr(ingest, "_fetch_pr_head",
                        lambda repo, slug, n, sha: True)
    comments = []
    monkeypatch.setattr(ingest, "_comment",
                        lambda s, n, b: comments.append(b))
    monkeypatch.setattr(ingest, "_label", lambda s, n, v: None)

    class Contended:
        repo = str(eng.repo)

        def submit(self, ref, agent="", title=""):
            return {"verdict": "rejected",
                    "reason": "admission timeout behind pr/4"}

    state = IngestState(eng.state_dir)
    pr = {"number": 12, "title": "t", "headRefOid": "e" * 40}
    assert ingest.ingest_pr(Contended(), SLUG, state, pr) == (12, "contended")
    assert comments == []                        # nothing to tell a human yet
    assert state.seen(12, "e" * 40) is False     # and it is owed a retry


def test_work_already_in_the_tip_is_recorded_landed_without_a_comment(
        repo, monkeypatch):
    eng = make_engine(repo)
    monkeypatch.setattr(ingest, "_fetch_pr_head",
                        lambda repo, slug, n, sha: True)
    comments = []
    monkeypatch.setattr(ingest, "_comment",
                        lambda s, n, b: comments.append(b))
    monkeypatch.setattr(ingest, "_label", lambda s, n, v: None)

    class Redelivered:
        repo = str(eng.repo)

        def submit(self, ref, agent="", title=""):
            return {"verdict": "rejected",
                    "reason": "changeset already in tip"}

    state = IngestState(eng.state_dir)
    pr = {"number": 4, "title": "t", "headRefOid": "b" * 40}
    assert ingest.ingest_pr(Redelivered(), SLUG, state, pr) == (4, "landed")
    assert comments == []
    assert state.seen(4, "b" * 40)


def test_identity_is_bound_per_block_and_asked_per_call():
    assert ingest._gh_env() is None
    provider = _fake_tokens(("tok-one", "tok-two")).token
    with ingest.gh_identity(provider):
        first = ingest._gh_env()["GH_TOKEN"]
        second = ingest._gh_env()["GH_TOKEN"]
        assert ingest._gh_env()["GH_NO_UPDATE_NOTIFIER"] == "1"
        assert "PATH" in ingest._gh_env()
    assert ingest._gh_env() is None
    # a provider, not a snapshot: a landing outlives the token it started with
    assert (first, second) == ("tok-one", "tok-two")


def test_a_long_landing_reports_with_a_fresh_token(repo, monkeypatch):
    eng = make_engine(repo)
    head = pr_branch(repo, "feature", "x = 2\n")
    monkeypatch.setattr(ingest, "_pr_view", lambda slug, n: {
        "number": n, "title": "t", "headRefOid": head})
    seen = []

    def slow_landing(engine, slug, state, pr, report=True, claimed=False):
        seen.append(ingest._gh_env()["GH_TOKEN"])   # fetch, at the start
        seen.append(ingest._gh_env()["GH_TOKEN"])   # comment, 30 minutes later
        return (pr["number"], "landed")

    monkeypatch.setattr(ingest, "ingest_pr", slow_landing)
    tokens = _fake_tokens(("ghs_old", "ghs_fresh"))
    gateway._work(("land", _delivery()), eng, SLUG, tokens,
                  IngestState(eng.state_dir), True)
    assert seen == ["ghs_old", "ghs_fresh"]


def _setup_cmd_refusing(var):
    return [sys.executable, "-c",
            f"import os,sys; sys.exit(1 if {var!r} in os.environ else 0)"]


def test_the_gate_setup_command_never_inherits_the_token(repo, monkeypatch):
    """run_gate hands the TEST command a scrubbed env, but _run_setup and the
    generator commands inherit this process's environment while running code
    from the submitted head. That is why identity is a ContextVar and never
    os.environ."""
    eng = make_engine(repo)
    eng.config["setup_cmd"] = _setup_cmd_refusing("GH_TOKEN")
    head = pr_branch(repo, "feature", "x = 2\n")
    monkeypatch.setattr(ingest, "_fetch_pr_head",
                        lambda repo, slug, n, sha: True)
    monkeypatch.setattr(ingest, "_comment", lambda s, n, b: None)
    monkeypatch.setattr(ingest, "_label", lambda s, n, v: None)
    state = IngestState(eng.state_dir)
    pr = {"number": 11, "title": "t", "headRefOid": head}
    with ingest.gh_identity(lambda: "ghs_" + "D" * 30):
        acted = ingest.ingest_pr(eng, SLUG, state, pr)
    assert acted == (11, "landed"), acted
    assert os.environ.get("GH_TOKEN") is None


def test_the_gate_setup_command_never_inherits_the_webhook_secret(
        repo, monkeypatch):
    """Same environment, same reasoning: `_secret` CONSUMES
    CAFECITO_WEBHOOK_SECRET rather than reading it, because the secret is the
    one thing standing between the listener and anyone who learns its URL,
    and the gate's setup_cmd runs code from the submitted head."""
    monkeypatch.setenv("CAFECITO_WEBHOOK_SECRET", SECRET.decode())
    assert gateway._secret(types.SimpleNamespace(secret_file=None)) == SECRET
    assert "CAFECITO_WEBHOOK_SECRET" not in os.environ

    eng = make_engine(repo)
    eng.config["setup_cmd"] = _setup_cmd_refusing("CAFECITO_WEBHOOK_SECRET")
    head = pr_branch(repo, "feature", "x = 2\n")
    monkeypatch.setattr(ingest, "_fetch_pr_head",
                        lambda repo, slug, n, sha: True)
    monkeypatch.setattr(ingest, "_comment", lambda s, n, b: None)
    monkeypatch.setattr(ingest, "_label", lambda s, n, v: None)
    pr = {"number": 13, "title": "t", "headRefOid": head}
    acted = ingest.ingest_pr(eng, SLUG, IngestState(eng.state_dir), pr)
    assert acted == (13, "landed"), acted


def test_a_closed_pr_is_dropped_without_submitting(repo, monkeypatch):
    eng = make_engine(repo)
    monkeypatch.setattr(ingest, "_pr_view", lambda slug, n: None)
    monkeypatch.setattr(ingest, "ingest_pr", lambda *a, **k: pytest.fail(
        "submitted a PR that is gone"))
    assert gateway._work(("land", _delivery()), eng, SLUG, _fake_tokens(),
                         IngestState(eng.state_dir), True) == "gone"


def test_a_fork_notice_is_a_comment_and_nothing_else(repo, monkeypatch):
    eng = make_engine(repo)
    bodies = []
    monkeypatch.setattr(ingest, "_comment", lambda s, n, b: bodies.append(b))
    monkeypatch.setattr(ingest, "_pr_view", lambda slug, n: pytest.fail(
        "a fork notice must not touch the PR"))
    out = gateway._work(("notice", _delivery(is_fork=True)), eng, SLUG,
                        _fake_tokens(), IngestState(eng.state_dir), True)
    assert out == "fork-skipped"
    assert len(bodies) == 1 and "fork" in bodies[0]


def test_a_backlog_does_not_outlive_the_stop_signal(repo, monkeypatch):
    """The sentinel sits BEHIND up to `queue_max` pending landings, so a
    sentinel-only worker keeps landing PRs long after run_gateway has
    returned and a supervisor feels free to SIGKILL the process — cutting off
    a landing mid-git-operation, the one thing the join exists to prevent."""
    eng = make_engine(repo)
    stop = threading.Event()
    landed, started = [], threading.Event()

    def slow(item, *a, **k):
        landed.append(item[1].number)
        started.set()
        time.sleep(0.2)
        return "landed"

    monkeypatch.setattr(gateway, "_work", slow)
    q = queue.Queue()
    for n in range(20):
        q.put(("land", _delivery(number=n)))
    t = threading.Thread(
        target=gateway._worker_loop, daemon=True,   # a regression must fail,
        args=(q, eng, SLUG, None, IngestState(eng.state_dir), False),
        kwargs={"note": lambda line: None, "stop": stop})   # not wedge pytest
    t.start()
    assert started.wait(5)
    stop.set()
    t.join(timeout=10)
    assert not t.is_alive()
    assert len(landed) < 20 and q.qsize() > 0    # abandoned, not drained


# ---------------------------------------------------------------- service ---

def _args(repo, **kw):
    base = dict(repo=str(repo), github=SLUG, bind=None, port=0, path=None,
                app_client_id="Iv23liAbCdEf", installation_id=INSTALL,
                private_key=None, secret_file=None, workers=1, sweep=None,
                no_sweep=True, no_report=True, land_forks=False,
                allow_public_bind=False, check=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _loopback_bind_allowed() -> bool:
    """Functional probe, not an assumption: this repo's own plane gates with
    `isolation: sandbox`, whose profile denies `network*`. Binding a loopback
    listener is a network operation, so every test below that starts a real
    server is unrunnable inside our own gate — it fails there and passes
    everywhere else, which is the worst shape a test can have. Probe the
    capability and skip, the way test_isolation.py does for sandbox-exec."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


needs_loopback = pytest.mark.skipif(
    not _loopback_bind_allowed(),
    reason="environment denies binding a loopback socket (sandboxed gate)")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _await_port(port, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return True
        except OSError:
            time.sleep(0.05)
    return False


def _signed_post(port, number, delivery_id):
    """One signed delivery, by hand: the receiver's own helpers live in
    test_webhook.py and this file needs only the status code."""
    body = json.dumps({
        "action": "synchronize", "number": number,
        "pull_request": {"number": number, "title": "t", "draft": False,
                         "head": {"repo": {"full_name": SLUG}}},
        "repository": {"full_name": SLUG},
        "installation": {"id": INSTALL}}).encode()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/webhook", body, {
        "Content-Type": "application/json",
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": "sha256=" + hmac.new(
            SECRET, body, hashlib.sha256).hexdigest()})
    resp = conn.getresponse()
    resp.read()
    conn.close()
    return resp.status


def test_no_secret_is_an_operator_error(repo, monkeypatch, capsys):
    monkeypatch.delenv("CAFECITO_WEBHOOK_SECRET", raising=False)
    assert gateway.run_gateway(_args(repo)) == 2
    out = capsys.readouterr().out
    assert "--secret-file" in out and "CAFECITO_WEBHOOK_SECRET" in out


def test_a_short_secret_is_refused(repo, monkeypatch, capsys):
    monkeypatch.setenv("CAFECITO_WEBHOOK_SECRET", "hunter2")
    assert gateway.run_gateway(_args(repo)) == 2
    assert "16 bytes" in capsys.readouterr().out


def test_a_public_bind_needs_saying_so(repo, secret_file, pem, monkeypatch,
                                       capsys):
    monkeypatch.setattr(gateway.shutil, "which", lambda t: "/usr/bin/" + t)
    args = _args(repo, secret_file=str(secret_file), private_key=str(pem),
                 bind="0.0.0.0")
    assert gateway.run_gateway(args) == 2
    assert "--allow-public-bind" in capsys.readouterr().out


def test_a_key_inside_the_worktree_is_refused(repo, secret_file, capsys):
    inside = repo / "app.pem"
    inside.write_text("-----BEGIN RSA PRIVATE KEY-----\nzz\n"
                      "-----END RSA PRIVATE KEY-----\n")
    inside.chmod(0o600)
    args = _args(repo, secret_file=str(secret_file), private_key=str(inside))
    assert gateway.run_gateway(args) == 2
    out = capsys.readouterr().out
    assert "working tree" in out and "setup" in out


@needs_loopback
def test_landing_forks_without_a_boundary_is_refused(repo, secret_file, pem,
                                                     monkeypatch, capsys):
    """`--land-forks` is a policy, not a mitigation: it hands the gate a tree
    an outsider wrote. isolation is what contains the test run, and an empty
    setup_cmd is what keeps anything from running outside that boundary —
    the gate never wraps setup. Documented is not enforced, so both are
    startup refusals, like the non-loopback bind."""
    monkeypatch.setattr(gateway.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(
        ghapp, "mint_installation_token",
        lambda *a, **k: ("ghs_" + "F" * 30, time.time() + 3600))
    args = _args(repo, secret_file=str(secret_file), private_key=str(pem),
                 land_forks=True, check=True)

    assert gateway.run_gateway(args) == 2
    out = capsys.readouterr().out
    assert "isolation" in out and "--land-forks" in out

    _set_config(repo, isolation="sandbox", setup_cmd=["npm", "ci"])
    assert gateway.run_gateway(args) == 2
    out = capsys.readouterr().out
    assert "setup_cmd" in out and "test" in out.lower()

    _set_config(repo, isolation="sandbox", setup_cmd=[])
    assert gateway.run_gateway(args) == 0
    assert "ready" in capsys.readouterr().out


@needs_loopback
def test_check_verifies_everything_and_binds_nothing(repo, secret_file, pem,
                                                     monkeypatch, capsys):
    monkeypatch.setattr(gateway.shutil, "which", lambda t: "/usr/bin/" + t)
    minted = []

    def fake_mint(client_id, key, installation_id, repo_name="",
                  openssl="openssl"):
        minted.append((client_id, installation_id, repo_name))
        return "ghs_" + "E" * 30, time.time() + 3600

    monkeypatch.setattr(ghapp, "mint_installation_token", fake_mint)
    port = _free_port()
    args = _args(repo, secret_file=str(secret_file), private_key=str(pem),
                 check=True, port=port)
    assert gateway.run_gateway(args) == 0
    assert minted == [("Iv23liAbCdEf", INSTALL, "widget")]
    assert "ready" in capsys.readouterr().out
    # nothing is listening afterwards
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.close()


def test_the_sweep_re_offers_open_prs_and_the_claim_absorbs_the_repeat(
        repo, monkeypatch):
    eng = make_engine(repo)
    head = pr_branch(repo, "feature", "x = 2\n")
    monkeypatch.setattr(ingest, "_list_prs", lambda slug: [
        {"number": 7, "title": "bump x", "headRefOid": head,
         "isCrossRepository": False},
        {"number": 8, "title": "from a fork", "headRefOid": head,
         "isCrossRepository": True}])
    q = queue.Queue()
    stop = threading.Event()
    stop.set()
    gateway._sweeper_loop(stop, 0.01, q, SLUG, INSTALL, _fake_tokens(),
                          fork_policy="skip", note=lambda line: None)
    items = [q.get_nowait() for _ in range(q.qsize())]
    assert [i[1].number for i in items] == [7]      # the fork is not swept
    assert items[0][1].source == "sweep" and items[0][1].delivery_id is None

    monkeypatch.setattr(ingest, "_pr_view", lambda slug, n: {
        "number": n, "title": "bump x", "headRefOid": head})
    monkeypatch.setattr(ingest, "_comment", lambda s, n, b: None)
    monkeypatch.setattr(ingest, "_label", lambda s, n, v: None)
    claims = IngestState(eng.state_dir)
    assert gateway._work(items[0], eng, SLUG, _fake_tokens(), claims,
                         True) == "landed"
    assert gateway._work(items[0], eng, SLUG, _fake_tokens(), claims,
                         True) == "duplicate"
    assert eng.status()["landed"] == 1


@needs_loopback
def test_the_202_never_waits_for_a_landing(repo, secret_file, pem,
                                           monkeypatch):
    """The property the whole enqueue-and-answer design exists for: GitHub
    abandons a delivery after ten seconds and never retries it, while a
    landing takes minutes. Deliveries must be answered while a worker is
    busy."""
    monkeypatch.setattr(gateway.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(
        ghapp, "mint_installation_token",
        lambda *a, **k: ("ghs_" + "F" * 30, time.time() + 3600))
    monkeypatch.setattr(ingest, "_pr_view", lambda slug, n: {
        "number": n, "title": "t", "headRefOid": "f" * 40})
    busy, release = threading.Event(), threading.Event()

    def blocked(*a, **k):
        busy.set()
        release.wait(30)
        return (7, "landed")

    monkeypatch.setattr(ingest, "ingest_pr", blocked)
    port = _free_port()
    stop = threading.Event()
    args = _args(repo, secret_file=str(secret_file), private_key=str(pem),
                 port=port, workers=1)
    args.stop_event = stop
    t = threading.Thread(target=lambda: gateway.run_gateway(args), daemon=True)
    t.start()
    try:
        assert _await_port(port)
        assert _signed_post(port, 7, "a") == 202
        assert busy.wait(10)                     # the one worker is now stuck
        t0 = time.time()
        assert [_signed_post(port, 8, f"b{i}") for i in range(3)] == [202] * 3
        assert time.time() - t0 < 5.0            # GitHub's budget is 10s
    finally:
        release.set()
        stop.set()
        t.join(timeout=30)
    assert not t.is_alive()


@needs_loopback
def test_shutdown_releases_the_socket_and_joins_the_workers(
        repo, secret_file, pem, monkeypatch):
    monkeypatch.setattr(gateway.shutil, "which", lambda t: "/usr/bin/" + t)
    monkeypatch.setattr(
        ghapp, "mint_installation_token",
        lambda *a, **k: ("ghs_" + "F" * 30, time.time() + 3600))
    stop = threading.Event()
    args = _args(repo, secret_file=str(secret_file), private_key=str(pem),
                 port=0, workers=2)
    args.stop_event = stop
    out = {}
    t = threading.Thread(target=lambda: out.update(rc=gateway.run_gateway(args)))
    t.start()
    time.sleep(1.0)
    stop.set()
    t.join(timeout=30)
    assert not t.is_alive()
    assert out.get("rc") == 0
    assert not [x for x in threading.enumerate() if x.name.startswith("land-")]


def test_gateway_config_is_a_block_and_not_in_default_config(repo):
    eng = Engine(str(repo))
    assert "gateway" not in DEFAULT_CONFIG
    assert not [k for k in DEFAULT_CONFIG if k.startswith("gateway")]
    (eng.state_dir / "config.json").write_text(json.dumps(
        {**eng.config, "gateway": {"port": 9999, "fork_policy": "gate",
                                   "nonsense": True}}))
    cfg = gateway._config(Engine(str(repo)))
    assert cfg["port"] == 9999 and cfg["fork_policy"] == "gate"
    assert cfg["bind"] == "127.0.0.1" and cfg["workers"] == 1
    assert "nonsense" not in cfg
