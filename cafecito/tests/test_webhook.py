"""The untrusted edge: everything the receiver does with bytes a stranger sent.

Most of what is pinned here is a refusal. Every test runs offline — no
network, no real GitHub — and the only sockets bound are loopback on port 0.
"""

import contextlib
import hashlib
import hmac
import http.client
import json
import pathlib
import re
import socket
import threading
import time
import types

import pytest

from cafecito import ghapp, webhook

SECRET = b"0123456789abcdef0123456789abcdef"
SLUG = "acme/widget"
INSTALL = 12345678


# ---------------------------------------------------------- http helpers ---

def sign(body: bytes, secret: bytes = SECRET) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def payload(action="opened", number=7, slug=SLUG, installation=INSTALL,
            fork=False, head_repo_null=False, draft=False, sender="someone"):
    if head_repo_null:
        head = {"sha": "a" * 40, "ref": "f", "repo": None}
    else:
        head = {"sha": "a" * 40, "ref": "f",
                "repo": {"full_name": "outsider/widget" if fork else slug}}
    return {"action": action, "number": number,
            "pull_request": {"number": number, "title": "bump x",
                             "draft": draft, "head": head},
            "repository": {"full_name": slug},
            "installation": {"id": installation},
            "sender": {"login": sender}}


def _loopback_bind_allowed() -> bool:
    """Functional probe, not an assumption. This repo's own plane gates with
    `isolation: sandbox`, whose profile denies `network*` — so a real listener
    cannot be started inside our own gate. Every test here that drives HTTP for
    real needs one, and a test that fails only inside the gate is worse than a
    test that says why it was skipped. Same pattern test_isolation.py has used
    since v0.11.0: probe the capability, never assume it."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


@contextlib.contextmanager
def receiver(accept=True, notify_ok=True, **over):
    if not _loopback_bind_allowed():
        pytest.skip("environment denies binding a loopback socket "
                    "(sandboxed gate) — the pure-function tests below still run")
    seen, notices, notes = [], [], []
    cfg = webhook.ReceiverConfig(secret=SECRET, path="/webhook", slug=SLUG,
                                 installation_id=INSTALL, **over)

    def sink(d):
        if not accept:
            return False
        seen.append(d)
        return True

    def notify(d):
        if not notify_ok:
            return False
        notices.append(d)
        return True

    srv = webhook.build_server(cfg, sink, notify, bind="127.0.0.1",
                               port=0, note=notes.append)
    t = threading.Thread(target=srv.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    try:
        yield types.SimpleNamespace(port=srv.server_address[1], seen=seen,
                                    notices=notices, notes=notes, srv=srv)
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def raw_post(port, headers=(), body=b"", path="/webhook"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest("POST", path, skip_accept_encoding=True)
    for k, v in headers:
        conn.putheader(k, v)
    conn.endheaders()
    if body:
        conn.send(body)
    resp = conn.getresponse()
    out = (resp.status, resp.read(), dict(resp.getheaders()))
    conn.close()
    return out


def deliver(port, obj=None, *, body=None, secret=SECRET, sig=None,
            event="pull_request", delivery="d1", ctype="application/json",
            length="auto", path="/webhook"):
    body = json.dumps(obj).encode() if body is None else body
    hdrs = []
    if ctype:
        hdrs.append(("Content-Type", ctype))
    if length == "auto":
        hdrs.append(("Content-Length", str(len(body))))
    elif length is not None:
        hdrs.append(("Content-Length", str(length)))
    if event is not None:
        hdrs.append(("X-GitHub-Event", event))
    if delivery is not None:
        hdrs.append(("X-GitHub-Delivery", delivery))
    if sig != "omit":
        hdrs.append(("X-Hub-Signature-256", sig or sign(body, secret)))
    return raw_post(port, hdrs, body, path)


def stalled_request(port, headers, sent=b""):
    """Send headers (and optionally a few body bytes) and then simply stop —
    without closing. Returns whatever the server answers, or b'' if it never
    does. A client that hangs up is a different case, and easier."""
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        s.sendall(headers + sent)
        try:
            return s.recv(200)
        except (TimeoutError, OSError):
            return b""
    finally:
        s.close()


def test_valid_delivery_is_accepted_and_projected():
    with receiver() as r:
        status, _, _ = deliver(r.port, payload())
        assert status == 202
        assert len(r.seen) == 1
        d = r.seen[0]
        assert (d.action, d.number, d.slug) == ("opened", 7, SLUG)
        assert d.installation_id == INSTALL and d.is_fork is False
        assert d.delivery_id == "d1" and d.source == "webhook"
        # the head sha is deliberately NOT carried: a delivery is a trigger
        assert not any("a" * 40 == str(v) for v in d)


def test_one_flipped_body_byte_is_rejected():
    with receiver() as r:
        body = json.dumps(payload()).encode()
        tampered = body.replace(b'"opened"', b'"openeD"')
        status, _, _ = deliver(r.port, body=tampered, sig=sign(body))
        assert status == 403 and r.seen == []


def test_missing_signature_header_is_not_a_skip():
    with receiver() as r:
        status, _, _ = deliver(r.port, payload(), sig="omit")
        assert status == 403 and r.seen == []


def test_legacy_sha1_signature_is_never_a_fallback():
    with receiver() as r:
        body = json.dumps(payload()).encode()
        sha1 = "sha1=" + hmac.new(SECRET, body, hashlib.sha1).hexdigest()
        hdrs = [("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
                ("X-GitHub-Event", "pull_request"),
                ("X-GitHub-Delivery", "d1"),
                ("X-Hub-Signature", sha1)]
        status, _, _ = raw_post(r.port, hdrs, body)
        assert status == 403 and r.seen == []


def test_signature_is_over_raw_bytes_not_reserialized_json():
    with receiver() as r:
        body = json.dumps(payload(), indent=2).encode()
        canonical = json.dumps(json.loads(body)).encode()
        status, _, _ = deliver(r.port, body=body, sig=sign(canonical))
        assert status == 403 and r.seen == []


def test_signature_prefix_and_case_must_match():
    with receiver() as r:
        body = json.dumps(payload()).encode()
        bare = sign(body).split("=", 1)[1]
        assert deliver(r.port, body=body, sig=bare)[0] == 403
        assert deliver(r.port, body=body, sig=sign(body).upper())[0] == 403
        assert r.seen == []


def test_content_length_is_required():
    with receiver() as r:
        assert deliver(r.port, payload(), length=None)[0] == 411
        assert deliver(r.port, payload(), length="twelve")[0] == 411
        assert r.seen == []


def test_a_superscript_is_not_a_digit():
    """Headers decode as ISO-8859-1, where 0xB2 becomes "²": str.isdigit()
    says yes and int() then raises, so the guard has to be ASCII-explicit or
    it admits exactly what the next line cannot parse."""
    assert "²".isdigit() is True
    assert webhook._LEN_RE.fullmatch("²") is None
    with receiver() as r:
        for bad in ("²", "1²", " 12 3", "0x10", "+7", "1_0"):
            status, out, _ = deliver(r.port, payload(), length=bad)
            assert status == 411, bad
        assert r.seen == []


def test_oversized_body_gets_a_status_not_a_broken_pipe():
    """Replying 413 without draining makes the sender see a connection
    error, and GitHub records a failed delivery instead of our answer."""
    # GitHub's own payload cap is 25 MiB, so the drain has to reach at least
    # that far or a legitimately oversized delivery gets an RST anyway.
    assert webhook.MAX_DRAIN_BYTES >= 25 << 20
    with receiver(max_body_bytes=1024) as r:
        body = b"x" * 4096
        status, out, _ = deliver(r.port, body=body)
        assert status == 413 and b"too large" in out
        assert r.seen == []


def test_a_form_encoded_body_is_drained_before_the_415():
    """form-urlencoded is the misconfiguration this branch exists to
    diagnose. Answering 4xx with bytes still queued in the kernel's receive
    buffer sends an RST, and the operator sees a connection error instead of
    the sentence telling them what to change."""
    with receiver() as r:
        status, out, _ = deliver(r.port, body=b"payload=" + b"x" * (2 << 20),
                                 ctype="application/x-www-form-urlencoded")
        assert status == 415 and b"application/json" in out
        assert r.seen == []


def test_a_stalled_client_does_not_take_the_server_down():
    with receiver() as r:
        s = socket.create_connection(("127.0.0.1", r.port), timeout=5)
        s.sendall(b"POST /webhook HTTP/1.1\r\nHost: x\r\n"
                  b"Content-Type: application/json\r\n"
                  b"Content-Length: 1000\r\n\r\n0123456789")
        s.close()
        time.sleep(0.2)
        assert deliver(r.port, payload())[0] == 202
        assert len(r.seen) == 1


def test_a_client_that_stalls_without_closing_is_answered(monkeypatch):
    """The close-based test above hits EOF, which is the easy case. A client
    that declares a length and then sends nothing produces a read timeout,
    which BaseHTTPRequestHandler swallows: without an explicit answer the
    connection just dies, no status is sent and no line reaches the log."""
    monkeypatch.setattr(webhook, "BODY_TIMEOUT_S", 0.3)
    with receiver(max_body_bytes=1024) as r:
        answer = stalled_request(
            r.port, b"POST /webhook HTTP/1.1\r\nHost: x\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 900\r\n\r\n", b"012345")
        assert b"408" in answer
        # a length nobody could ever send is still a 413, not a dead socket
        answer = stalled_request(
            r.port, b"POST /webhook HTTP/1.1\r\nHost: x\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 9999999999\r\n\r\n")
        assert b"413" in answer
        assert r.seen == []
        assert deliver(r.port, payload())[0] == 202


def test_unwanted_events_are_dropped_before_parsing(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("parsed an event we do not act on")

    monkeypatch.setattr(webhook.json, "loads", boom)
    with receiver() as r:
        for event in ("push", "issue_comment", "check_run"):
            status, out, _ = deliver(r.port, payload(), event=event,
                                     delivery=f"d-{event}")
            assert status == 200 and b"ignored" in out
        assert r.seen == []


def test_ping_is_answered_but_never_queued():
    with receiver() as r:
        status, out, _ = deliver(r.port, {"zen": "hi"}, event="ping")
        assert status == 200 and b"pong" in out
        assert r.seen == []


def test_actions_without_new_code_are_ignored():
    with receiver() as r:
        for i, action in enumerate(("labeled", "closed", "edited",
                                    "assigned", "review_requested")):
            status, out, _ = deliver(r.port, payload(action=action),
                                     delivery=f"a{i}")
            assert status == 200 and b"action" in out
        assert r.seen == []


def test_another_repository_is_ignored():
    with receiver() as r:
        status, out, _ = deliver(r.port, payload(slug="someone/else"))
        assert status == 200 and b"wrong repository" in out
        assert r.seen == []


def test_another_installation_is_ignored():
    with receiver() as r:
        status, out, _ = deliver(r.port, payload(installation=999))
        assert status == 200 and b"wrong installation" in out
        assert r.seen == []


def test_our_own_app_does_not_chase_its_own_tail():
    with receiver(app_slug="cafecito") as r:
        status, out, _ = deliver(r.port, payload(sender="cafecito[bot]"))
        assert status == 200 and b"self" in out
        assert r.seen == []


def test_redelivery_is_queued_once():
    with receiver() as r:
        body = json.dumps(payload()).encode()
        first = deliver(r.port, body=body, delivery="same-guid")
        second = deliver(r.port, body=body, delivery="same-guid")
        assert first[0] == 202 and second[0] == 200
        assert b"duplicate" in second[1]
        assert len(r.seen) == 1


def test_unparseable_authenticated_body_is_a_400():
    with receiver() as r:
        for i, body in enumerate((b"{", b"[]", b"null", b"1", b"\xff\xfe")):
            status, _, _ = deliver(r.port, body=body, delivery=f"p{i}")
            assert status == 400, body
        assert r.seen == []


def test_malformed_fields_never_leave_the_module():
    with receiver() as r:
        bad = [payload(), payload(), payload(), payload()]
        bad[0]["repository"]["full_name"] = "../../etc/passwd"
        bad[1]["number"] = "7; rm -rf /"
        bad[1]["pull_request"]["number"] = "7; rm -rf /"
        bad[2]["installation"] = {"id": "not-an-int"}
        bad[3]["action"] = "opened; touch /tmp/x"
        for i, obj in enumerate(bad):
            status, out, _ = deliver(r.port, obj, delivery=f"m{i}")
            assert status == 400 and b"malformed" in out, obj
        assert r.seen == []


def test_nothing_from_the_working_tree_is_served():
    with receiver() as r:
        conn = http.client.HTTPConnection("127.0.0.1", r.port, timeout=5)
        for path in ("/", "/webhook.py", "/../cafecito/gateway.py"):
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read()
            assert resp.status == 404, path
            assert b"def " not in body
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        assert resp.status == 200 and b"ok" in resp.read()
        conn.close()


def test_a_query_string_is_tolerated_but_the_path_is_not():
    with receiver() as r:
        assert deliver(r.port, payload(), path="/webhook?token=x")[0] == 202
        assert deliver(r.port, payload(), path="/webhookx",
                       delivery="d2")[0] == 404
        assert len(r.seen) == 1


def test_form_encoded_bodies_are_refused():
    """Configured as form-urlencoded, GitHub signs `payload=%7B…`, not the
    JSON — the HMAC would be over bytes nobody expects."""
    with receiver() as r:
        status, _, _ = deliver(r.port, payload(),
                               ctype="application/x-www-form-urlencoded")
        assert status == 415 and r.seen == []


def test_drafts_are_skipped_until_they_are_ready():
    with receiver() as r:
        status, out, _ = deliver(r.port, payload(action="synchronize",
                                                 draft=True), delivery="d-a")
        assert status == 200 and b"draft" in out
        assert deliver(r.port, payload(action="ready_for_review", draft=True),
                       delivery="d-b")[0] == 202
        assert len(r.seen) == 1


def test_fork_prs_are_skipped_and_told_once():
    with receiver() as r:
        assert deliver(r.port, payload(fork=True), delivery="f1")[0] == 200
        assert deliver(r.port, payload(fork=True, action="synchronize"),
                       delivery="f2")[0] == 200
        assert r.seen == []
        assert len(r.notices) == 1        # not one per push
    with receiver(fork_policy="gate") as r:
        assert deliver(r.port, payload(fork=True))[0] == 202
        assert len(r.seen) == 1 and r.seen[0].is_fork is True


def test_a_fork_notice_that_could_not_be_queued_is_not_remembered():
    """Marking the PR notified before the notice is actually queued means a
    full queue swallows the only comment its author will ever get. Same
    ordering the delivery log already gets right."""
    with receiver(notify_ok=False) as r:
        assert deliver(r.port, payload(fork=True), delivery="f1")[0] == 200
        assert r.notices == []
        assert r.srv.forks.seen_or_add(f"{SLUG}#7") is False


def test_a_deleted_fork_leaves_head_repo_null():
    with receiver() as r:
        status, out, _ = deliver(r.port, payload(head_repo_null=True))
        assert status == 200 and b"fork-skipped" in out
        assert r.seen == []


def test_a_full_queue_forgets_the_delivery_so_it_can_come_back():
    with receiver(accept=False) as r:
        status, _, headers = deliver(r.port, payload(), delivery="busy")
        assert status == 503 and headers.get("Retry-After")
        assert r.srv.deliveries.seen_or_add("busy") is False


def test_the_receiver_imports_nothing_from_the_package():
    """The trust boundary is an import rule, so it is enforced and not
    merely intended: webhook.py and ghapp.py are reviewable on their own."""
    for mod in (webhook, ghapp):
        src = pathlib.Path(mod.__file__).read_text()
        assert not re.search(r"^\s*(from\s+\.|from\s+cafecito|import\s+cafecito)",
                             src, re.M), mod.__name__
        for name, value in vars(mod).items():
            if isinstance(value, types.ModuleType):
                assert not value.__name__.startswith("cafecito"), name


def test_the_server_header_does_not_advertise_python():
    with receiver() as r:
        _, _, headers = deliver(r.port, payload())
        assert headers.get("Server") == "cafecito"
        assert "Python" not in headers.get("Server", "")


def test_no_log_line_carries_the_secret_or_the_signature():
    with receiver() as r:
        body = json.dumps(payload()).encode()
        signature = sign(body)
        deliver(r.port, body=body, sig=signature)
        deliver(r.port, body=body, sig="sha256=" + "0" * 64, delivery="bad")
        joined = "\n".join(r.notes)
        assert joined
        assert SECRET.decode() not in joined
        assert signature not in joined and "0" * 64 not in joined
