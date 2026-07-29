"""cafecito webhook — the untrusted edge of the hosted gateway.

Everything here runs on bytes an unauthenticated stranger sent us, so this
module imports nothing from cafecito: no engine, no git, no `gh`, no token,
no private key. A reviewer can read this one file and know the whole of what
an attacker can reach, and a test asserts the rule mechanically instead of
trusting it. The price is a slug regex duplicated from ingest.py; that
duplication is the point.

The receiver's job is to say no cheaply and then get out of the way. It
verifies `X-Hub-Signature-256` over the raw body BEFORE anything parses it,
then projects the verified payload down to seven checked scalars and hands
those to a queue. Nothing else survives the boundary — no title, no branch
name, no URL, not even the head sha — so no attacker-controlled string can
reach argv, a filesystem path, a git ref or a comment body by way of this
file. The head is deliberately absent: a delivery is a trigger, not data,
and the worker re-reads the current head from GitHub. That is what makes an
out-of-order or force-push delivery collapse into a no-op — while it is still
queued — rather than landing work the author already replaced.

GitHub gives a delivery ten seconds and does not retry it. So the handler
never touches the network, never takes a landing lock and never blocks: the
slowest thing it does is one HMAC over at most a megabyte.

Not yet, and stated so nobody assumes otherwise: no source-IP allowlist from
GitHub's /meta (a signature proves authenticity, an IP does not, and the
allowlist needs a network call plus periodic refresh); no TLS — bind
loopback and put a tunnel or reverse proxy in front; no multi-tenant secret
selection by X-GitHub-Hook-ID, because one process serves one repo.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, NamedTuple

# Actions that mean "there is new code here". `synchronize` is the workhorse
# (it fires on force-push too); `ready_for_review` matters only because we
# skip drafts, and a draft that goes ready may never see another commit.
ACTIONS = ("opened", "reopened", "synchronize", "ready_for_review")
EVENTS = ("pull_request", "ping")

READ_CHUNK = 64 << 10
# A 4xx sent while bytes are still queued in the kernel's receive buffer
# reaches the sender as an RST — a broken pipe, not our status — and GitHub
# records a failed delivery instead of the diagnosis. Drain, then answer.
# 25 MiB is GitHub's own payload cap, so a legitimately oversized delivery
# still gets the 413 that names the limit.
MAX_DRAIN_BYTES = 25 << 20
# Reading a body gets its own deadline, shorter than the connection's: a
# client that declares a length and then sends nothing must be answered and
# released, not left holding a thread until the socket times out.
BODY_TIMEOUT_S = 5

_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$")
# NOT str.isdigit(): headers decode as ISO-8859-1, where 0xB2 becomes "²" —
# isdigit() says yes and the int() on the next line raises.
_LEN_RE = re.compile(r"[0-9]{1,19}")
_ACTION_RE = re.compile(r"^[a-z_]{1,64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_MAX_NUMBER = 10 ** 9
_MAX_INSTALLATION = 10 ** 12


class Malformed(ValueError):
    """The body verified, but it is not a payload we can read."""


class Delivery(NamedTuple):
    """Everything that crosses the trust boundary. Seven scalars, each one
    type- and range-checked before this is constructed."""
    delivery_id: str | None   # X-GitHub-Delivery; None when sweep-originated
    action: str
    number: int
    slug: str
    installation_id: int
    is_fork: bool
    source: str               # "webhook" | "sweep"


class ReceiverConfig(NamedTuple):
    secret: bytes
    path: str
    slug: str
    installation_id: int
    app_slug: str = ""
    skip_drafts: bool = True
    fork_policy: str = "skip"          # "skip" | "gate"
    max_body_bytes: int = 1 << 20


# ------------------------------------------------------------ primitives ----

def verify_signature(secret: bytes, body: bytes, header: str | None) -> bool:
    """Constant-time check of X-Hub-Signature-256 over the RAW body.

    The legacy SHA-1 `X-Hub-Signature` is never consulted: falling back to it
    when the SHA-256 header is absent is a self-inflicted downgrade. An absent
    header is a failure, never a skip — GitHub omits the header when no secret
    is configured, and `if header: verify()` accepts every forgery."""
    if not header or not header.isascii():
        return False
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


class DeliveryLog:
    """Bounded, in-memory set of delivery ids already accepted.

    Cheap short-circuit for redeliveries and for our own retry storms; it is
    not the correctness mechanism (IngestState.claim is, and it is durable).
    In-memory on purpose: a restart forgetting ids costs one re-read of a PR,
    while a file here would need its own lock and its own pruning."""

    def __init__(self, capacity: int = 4096):
        self.capacity = capacity
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}

    def seen_or_add(self, key: str | None) -> bool:
        if not key:
            return False
        with self._lock:
            if key in self._seen:
                return True
            self._seen[key] = time.time()
            while len(self._seen) > self.capacity:
                self._seen.pop(next(iter(self._seen)))
            return False

    def forget(self, key: str | None) -> None:
        """Un-remember a delivery we accepted but could not queue, so a
        redelivery still has a chance."""
        if not key:
            return
        with self._lock:
            self._seen.pop(key, None)


def _int(value, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Malformed("not an integer")
    if not 1 <= value <= hi:
        raise Malformed("integer out of range")
    return value


def _dict(value) -> dict:
    if not isinstance(value, dict):
        raise Malformed("not an object")
    return value


def project(payload: dict, delivery_id: str | None = None,
            source: str = "webhook") -> Delivery:
    """The seven scalars, checked. Raises Malformed on anything else.

    Policy (which repo, which actions, forks, drafts) is deliberately NOT
    decided here: this function's only job is that nothing untyped or
    unbounded gets past it."""
    action = payload.get("action")
    if not isinstance(action, str) or not _ACTION_RE.match(action):
        raise Malformed("bad action")

    pr = _dict(payload.get("pull_request"))
    number = _int(payload.get("number", pr.get("number")), _MAX_NUMBER)

    repo = _dict(payload.get("repository"))
    slug = repo.get("full_name")
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise Malformed("bad repository")

    # The installation id comes from the verified body. The
    # X-GitHub-Hook-Installation-Target-ID header carries the APP id for an
    # App webhook, and using it produces silent 404s on the token endpoint.
    installation = _dict(payload.get("installation"))
    installation_id = _int(installation.get("id"), _MAX_INSTALLATION)

    head = pr.get("head")
    if not isinstance(head, dict):
        is_fork = True                       # unreadable head: assume hostile
    else:
        head_repo = head.get("repo")         # null when the fork was deleted
        is_fork = (not isinstance(head_repo, dict)
                   or head_repo.get("full_name") != slug)

    if delivery_id is not None and not _ID_RE.match(delivery_id):
        delivery_id = None
    return Delivery(delivery_id=delivery_id, action=action, number=number,
                    slug=slug, installation_id=installation_id,
                    is_fork=bool(is_fork), source=source)


# -------------------------------------------------------------- receiver ----

class _Receiver(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # every reply must carry Content-Length
    timeout = 10                    # without this a stalled client holds a
    server_version = "cafecito"     # thread forever (slowloris, measured)
    sys_version = ""

    # -- plumbing ----------------------------------------------------------

    def version_string(self) -> str:
        return "cafecito"

    def log_message(self, fmt, *args) -> None:
        """Silence the default log: it prints the raw request line, which a
        stranger wrote. Every line this server emits is built from fields we
        chose ourselves."""

    def _note(self, status: int, decision: str, d: Delivery | None = None,
              delivery_id: str | None = None) -> None:
        ident = delivery_id or (d.delivery_id if d else None) or "-"
        pr = f" pr={d.number}" if d else ""
        self.server.note(f"{self.client_address[0]} {self.command or '-'} "
                         f"{status} {decision}{pr} delivery={ident}")

    def _reply(self, status: int, decision: str, d: Delivery | None = None,
               delivery_id: str | None = None, headers=()) -> None:
        body = json.dumps({"status": decision}).encode() + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        if status >= 400:
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self._note(status, decision, d, delivery_id)

    # -- body --------------------------------------------------------------

    def _deadline(self, seconds: float | None) -> None:
        try:
            self.connection.settimeout(seconds)
        except OSError:
            pass

    def _declared(self) -> int | None:
        """The declared body length, or None when Content-Length is absent or
        is not a plain ASCII integer."""
        raw = (self.headers.get("Content-Length") or "").strip()
        return int(raw) if _LEN_RE.fullmatch(raw) else None

    def _drain(self, declared: int, cap: int = MAX_DRAIN_BYTES) -> bool:
        """Read and discard the declared body so a 4xx is not answered with
        an RST. False when the client stalled or hung up part-way."""
        remaining = min(declared, cap)
        while remaining > 0:
            try:
                chunk = self.rfile.read(min(remaining, READ_CHUNK))
            except OSError:          # TimeoutError included: it subclasses it
                return False
            if not chunk:
                return True          # EOF: nothing left in the queue
            remaining -= len(chunk)
        return True

    def _body(self, limit: int) -> bytes | None:
        # http.server does not decode Transfer-Encoding: chunked. Without a
        # length there is no body to read, and a proxy that re-chunks would
        # otherwise hand us b"" and a signature that never matches.
        declared = self._declared()
        if declared is None:
            self._reply(411, "length required")
            return None
        if declared > limit:
            self._drain(declared, max(MAX_DRAIN_BYTES, limit))
            self._reply(413, "too large")
            return None
        buf = bytearray()
        while len(buf) < declared:
            try:
                chunk = self.rfile.read(min(declared - len(buf), READ_CHUNK))
            except OSError:
                # A stalled client is a status, not a silence:
                # BaseHTTPRequestHandler swallows the socket timeout and
                # closes, so without this the sender never learns anything
                # and the line never reaches the log.
                self._reply(408, "request timeout")
                return None
            if not chunk:
                self._reply(400, "truncated body")
                return None
            buf += chunk
        return bytes(buf)

    # -- routes ------------------------------------------------------------

    def _route(self) -> str:
        # An operator may configure the App's webhook URL with a query
        # string; the path still has to match exactly.
        return self.path.split("?", 1)[0]

    def do_GET(self) -> None:
        if self._route() == "/healthz":
            self._reply(200, "ok")
            return
        self._reply(404, "not found")

    def do_POST(self) -> None:
        cfg: ReceiverConfig = self.server.cfg
        if self._route() != cfg.path:
            self._reply(404, "not found")
            return
        self._deadline(BODY_TIMEOUT_S)
        try:
            ctype = (self.headers.get("Content-Type")
                     or "").split(";")[0].strip()
            # form-urlencoded signs `payload=%7B...`, not the JSON — the HMAC
            # would be over bytes nobody expects. Refuse the encoding
            # outright, but drain first: form-urlencoded is the documented
            # misconfiguration this branch exists to diagnose, and an
            # undrained 415 reaches the operator as a connection error.
            if ctype.lower() != "application/json":
                self._drain(self._declared() or 0,
                            max(MAX_DRAIN_BYTES, cfg.max_body_bytes))
                self._reply(415, "send application/json")
                return
            body = self._body(cfg.max_body_bytes)
        finally:
            self._deadline(self.timeout)
        if body is None:
            return
        if not verify_signature(cfg.secret, body,
                                self.headers.get("X-Hub-Signature-256")):
            self._reply(403, "bad signature")
            return

        # Past this line the bytes are authentic. Nothing above it parsed
        # them, and nothing above it said anything a stranger could learn
        # from beyond "no".
        event = self.headers.get("X-GitHub-Event") or ""
        if event == "ping":
            self._reply(200, "pong")
            return
        if event != "pull_request":
            self._reply(200, "ignored")
            return

        delivery_id = self.headers.get("X-GitHub-Delivery")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._reply(400, "unparseable")
            return
        if not isinstance(payload, dict):
            self._reply(400, "unparseable")
            return
        try:
            d = project(payload, delivery_id)
        except Malformed as ex:
            self._reply(400, f"malformed: {ex}")
            return

        decision = self._decide(cfg, payload, d)
        if decision:
            self._reply(200, decision, d)
            return
        if self.server.deliveries.seen_or_add(d.delivery_id):
            self._reply(200, "duplicate", d)
            return
        if not self.server.sink(d):
            # Forget the id: this delivery was never worked, and a redelivery
            # (or the sweep) has to be able to pick it up.
            self.server.deliveries.forget(d.delivery_id)
            self._reply(503, "busy", d, headers=(("Retry-After", "30"),))
            return
        self._reply(202, "accepted", d)

    def _decide(self, cfg: ReceiverConfig, payload: dict,
                d: Delivery) -> str | None:
        """The reason to drop this delivery, or None to work it."""
        if d.slug != cfg.slug:
            return "wrong repository"
        if d.installation_id != cfg.installation_id:
            return "wrong installation"
        # Our own comment, label and push all generate deliveries — a
        # feedback loop polling never had. The event and action allowlists
        # already drop all three; this is the third independent layer.
        if cfg.app_slug:
            sender = payload.get("sender")
            login = sender.get("login") if isinstance(sender, dict) else None
            if login == f"{cfg.app_slug}[bot]":
                return "self"
        if d.action not in ACTIONS:
            return "action"
        pr = payload.get("pull_request") or {}
        if cfg.skip_drafts and pr.get("draft") is True \
                and d.action != "ready_for_review":
            return "draft"
        if d.is_fork and cfg.fork_policy != "gate":
            key = f"{d.slug}#{d.number}"
            if not self.server.forks.seen_or_add(key):
                # Same ordering as the delivery log below: a notice that could
                # not be queued was never sent, and remembering it would mean
                # nobody ever tells this PR's author why it was ignored.
                if not self.server.notify(d):
                    self.server.forks.forget(key)
            return "fork-skipped"
        return None


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        """A client that hangs up mid-reply is normal traffic, not an
        incident. Never print a traceback: it would carry request bytes."""
        self.note(f"{client_address[0]} - connection error")


def build_server(cfg: ReceiverConfig, sink: Callable[[Delivery], bool],
                 notify: Callable[[Delivery], bool] | None = None,
                 bind: str = "127.0.0.1", port: int = 8787,
                 note: Callable[[str], None] | None = None) -> _Server:
    """A receiver bound and ready to serve. `sink` queues a landing and
    `notify` queues the one comment a skipped fork PR gets; both return False
    when the queue is full, and this module then un-remembers the delivery so
    a redelivery or the sweep can still pick it up."""
    srv = _Server((bind, port), _Receiver)
    srv.cfg = cfg
    srv.sink = sink
    srv.notify = notify or (lambda d: True)
    srv.note = note or (lambda line: None)
    srv.deliveries = DeliveryLog()
    srv.forks = DeliveryLog(capacity=512)
    return srv


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("use: cafecito gateway")
