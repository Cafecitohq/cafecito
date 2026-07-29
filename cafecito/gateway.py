"""cafecito gateway — land GitHub PRs as they arrive (SPEC §6, §7).

`cafecito ingest` asks GitHub every sixty seconds whether anything changed.
The gateway is told. Nothing about landing changes: a delivery arrives, a
worker re-reads the PR, and `ingest_pr` submits the head through the same
engine, the same gate, the same landed log, and reports the same comment and
label. Only the trigger is different.

Three modules, and the split is the security boundary. `webhook.py` handles
bytes from strangers and imports nothing from cafecito. `ghapp.py` holds the
private key and imports nothing from cafecito. This module is the only one
that knows about both, and it is where a token, a repo and an engine meet.

The shape that matters: GitHub abandons a delivery after ten seconds and
never retries it, while `Engine.submit` can block for half an hour in
admission alone. So the handler verifies, projects, enqueues and answers 202,
and a worker pool does the landing. Deliveries are unordered and can be
replayed, so a delivery is treated as a trigger and never as data — the
worker asks GitHub for the PR's current head and claims (PR, head) atomically
before submitting. An out-of-order delivery or a force-push whose predecessor
is still QUEUED therefore collapses into a no-op instead of landing work the
author already replaced. A force-push that arrives after a worker has picked
the PR up does not cancel the landing already in flight: the head it replaced
finishes landing, and the new head lands behind it.

Not yet, deliberately, and stated here so nobody assumes otherwise:

  * no multi-tenancy — one process serves one repo's plane and one App
    installation, and the token it mints is scoped to that one repository,
    so this is enforced rather than merely intended;
  * no hosted state — state stays in `.cafecito/` exactly as `ingest` leaves
    it. There is no database;
  * no web console;
  * no deployment, TLS termination or process supervision. It binds loopback
    and expects a tunnel or reverse proxy in front, and a supervisor to
    restart it;
  * no failed-delivery replay API and no `/meta` hook-IP allowlist. The slow
    catch-up sweep covers the same ground without a second credential path.
"""

from __future__ import annotations

import os
import pathlib
import queue
import shutil
import signal
import socket
import stat
import threading
import time
from typing import Callable

from . import ghapp, ingest, webhook
from .engine import Engine
from .ghapp import GhAppError, TokenCache, redact
from .ingest import IngestState, gh_identity, slug_from_origin

# Everything that touches GitHub is called through `ingest.` rather than
# imported by name: `from .ingest import _comment` would make a second
# binding that monkeypatching the seam does not reach, and a test that
# thinks it is offline would quietly call the real API.

GATEWAY_DEFAULTS = {
    "bind": "127.0.0.1", "port": 8787, "path": "/webhook",
    "app_client_id": "", "app_slug": "", "installation_id": 0,
    "private_key_path": "", "openssl": "openssl",
    "workers": 1, "queue_max": 256, "sweep_s": 900,
    "max_body_bytes": 1 << 20, "skip_drafts": True,
    # 0 → gate_timeout_s * 2, the horizon the engine already uses for a dead
    # in-flight submission. One concept, one number.
    "claim_stale_s": 0,
    # A fork PR's head is attacker-authored code, and the gate's setup_cmd
    # and generator commands inherit this process's environment while running
    # inside a worktree built from that head. "skip" comments and stops;
    # "gate" is what `cafecito ingest` does today, and wants `isolation` set.
    "fork_policy": "skip",
}

FORK_NOTICE = (
    "☕ this PR comes from a fork, and the gateway is configured to skip "
    "forks (`fork_policy`): landing it would run the gate — including any "
    "`setup_cmd` — over code from outside the repo. A maintainer can land it "
    "deliberately with `cafecito submit <sha>`, or run the gateway with "
    "`--land-forks` on a plane whose `isolation` is set and whose `setup_cmd` "
    "is empty (the gate isolates the test command, never setup).")

LOOPBACK = ("127.0.0.1", "::1", "localhost")


class GatewayError(RuntimeError):
    """Operator error: something about the setup is wrong. Exit 2."""


def _stamp(line: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)


# --------------------------------------------------------------- config ----

def _config(engine: Engine) -> dict:
    """Gateway knobs, merged over the defaults. They live in an optional
    `gateway` block in .cafecito/config.json rather than in DEFAULT_CONFIG,
    because the Engine serializes DEFAULT_CONFIG to disk on first run — a key
    added there is a key in every user's config file forever."""
    cfg = dict(GATEWAY_DEFAULTS)
    block = engine.config.get("gateway")
    if isinstance(block, dict):
        cfg.update({k: v for k, v in block.items() if k in GATEWAY_DEFAULTS})
    return cfg


def _secret(args) -> bytes:
    """The webhook secret, from a file or the environment — never a flag.
    argv is world-readable in `ps`, and this is the only thing standing
    between the listener and anyone who learns its URL."""
    path = getattr(args, "secret_file", None)
    if path:
        p = pathlib.Path(path).expanduser()
        try:
            raw = p.read_bytes()
            mode = p.stat().st_mode
        except OSError as ex:
            raise GatewayError(f"cannot read --secret-file {p}: {ex.strerror}")
        if stat.S_IMODE(mode) & 0o077:
            raise GatewayError(f"{p} is mode {stat.S_IMODE(mode):04o} — "
                               f"readable by others; chmod 600 {p}")
        if raw.endswith(b"\n"):
            raw = raw[:-1]
        secret = raw.rstrip(b"\r")
    else:
        # pop, not get: the gate's setup_cmd and the generator commands run
        # with cwd inside a worktree built from the submitted head and inherit
        # this process's environment, so a secret left in os.environ is one
        # `npm ci` lifecycle script away from being read by the code we are
        # gating. Same reason identity is a ContextVar and never an env var.
        env = os.environ.pop("CAFECITO_WEBHOOK_SECRET", "")
        if not env:
            raise GatewayError(
                "no webhook secret: pass --secret-file PATH or set "
                "CAFECITO_WEBHOOK_SECRET. There is no --secret flag on "
                "purpose — argv is world-readable in `ps`.")
        secret = env.encode()
    if len(secret) < 16:
        raise GatewayError("webhook secret is under 16 bytes — generate one "
                           "with `openssl rand -hex 32` and paste the same "
                           "value into the App's webhook settings")
    return secret


def _fork_landing_unsafe(engine: Engine) -> str:
    """Why landing fork PRs on this plane would run code from outside the
    repo on this machine, or '' when the boundary is really there.

    `--land-forks` hands the gate a tree an outsider wrote. Two settings
    decide whether that is contained, and neither is implied by the flag:
    `isolation` puts the test run behind sandbox-exec or a container, and an
    empty `setup_cmd` is what keeps anything from running OUTSIDE that
    boundary — the gate never wraps setup (gate.py `_run_setup`), because
    installs need the real environment and the network."""
    landing = 'fork landing (--land-forks / fork_policy "gate")'
    if engine.config.get("isolation", "none") == "none":
        return (f'refusing {landing} while isolation is "none": a fork\'s '
                f'head is code from outside the repo and the gate would run '
                f'it here, as you, on the machine holding the App key. Set '
                f'"isolation" to "sandbox" or "container" in '
                f'.cafecito/config.json, or land forks by hand with '
                f'`cafecito submit <sha>`.')
    if engine.config.get("setup_cmd"):
        return (f'refusing {landing} while setup_cmd is set: isolation wraps '
                f'the gate\'s TEST command only. setup_cmd runs on the host '
                f'with the real environment and network, so a fork\'s '
                f'package.json lifecycle scripts or its setup.py would '
                f'execute unisolated. Clear setup_cmd, or land forks by hand '
                f'with `cafecito submit <sha>`.')
    return ""


def _preflight(engine: Engine, cfg: dict, args, slug: str,
               mint: bool = True) -> tuple[str, dict]:
    """('', setup) when everything needed is present and consistent;
    (message, {}) otherwise.

    Local checks first, network last, so every failure an operator can cause
    is reachable offline. Minting once at startup turns clock skew or a wrong
    installation id into a message now instead of silence in an hour."""
    try:
        secret = _secret(args)
    except GatewayError as ex:
        return str(ex), {}

    if str(cfg["fork_policy"]) == "gate":
        unsafe = _fork_landing_unsafe(engine)
        if unsafe:
            return unsafe, {}

    key_path = getattr(args, "private_key", None) or cfg["private_key_path"]
    if not key_path:
        return ("no App private key: pass --private-key PATH or set "
                "gateway.private_key_path in .cafecito/config.json"), {}
    resolved = pathlib.Path(key_path).expanduser().resolve()
    # The gate materializes candidate trees and runs setup commands with cwd
    # inside them. A key in the worktree is a key those commands trip over.
    if resolved.is_relative_to(pathlib.Path(engine.repo).resolve()):
        return (f"{resolved} is inside the repository working tree — the "
                f"landing gate runs setup and generator commands in there. "
                f"Keep the App key outside the repo."), {}
    try:
        pem = ghapp.load_private_key(str(resolved))
    except GhAppError as ex:
        return str(ex), {}

    if not cfg["app_client_id"]:
        return ("no App client id: pass --app-client-id Iv23li… (App "
                "settings → About → Client ID)"), {}
    if not int(cfg["installation_id"] or 0):
        return ("no installation id: pass --installation-id N (it is the "
                "trailing number in the App's 'Configure' URL, and it is "
                "`installation.id` on every delivery)"), {}

    for tool, why in (("git", "fetching PR heads"), ("gh", "PR reporting"),
                      (cfg["openssl"], "signing the App JWT")):
        if not shutil.which(tool):
            return f"{tool} is not on PATH — required for {why}", {}

    bind = str(cfg["bind"])
    if bind not in LOOPBACK and not getattr(args, "allow_public_bind", False):
        return (f"refusing to bind {bind}: this speaks plain HTTP and does "
                f"no rate limiting. Put a tunnel or reverse proxy in front, "
                f"or pass --allow-public-bind if you meant it."), {}

    try:
        ghapp._openssl_sign(pem, b"cafecito-gateway-preflight",
                            cfg["openssl"])
    except GhAppError as ex:
        return str(ex), {}

    port = int(cfg["port"])
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((bind, port))
        probe.close()
    except OSError as ex:
        return f"cannot bind {bind}:{port} — {ex.strerror}", {}

    tokens = TokenCache(str(cfg["app_client_id"]), pem,
                        int(cfg["installation_id"]),
                        repo_name=slug.split("/")[-1],
                        openssl=str(cfg["openssl"]))
    if mint:
        try:
            tokens.token()
        except GhAppError as ex:
            return str(ex), {}
    return "", {"secret": secret, "pem": pem, "tokens": tokens}


# --------------------------------------------------------------- worker ----

def _delivery_from_pr(pr: dict, slug: str,
                      installation_id: int) -> webhook.Delivery:
    """A sweep result in the shape a delivery has. `action` says where it came
    from; nothing downstream reads it, because the worker re-reads the PR."""
    return webhook.Delivery(delivery_id=None, action="sweep",
                            number=int(pr["number"]), slug=slug,
                            installation_id=installation_id,
                            is_fork=bool(pr.get("isCrossRepository")),
                            source="sweep")


def _work(item: tuple[str, webhook.Delivery], engine: Engine, slug: str,
          tokens: TokenCache | None, claims: IngestState,
          report: bool) -> str:
    """One queue item, start to finish. Returns what happened."""
    kind, d = item
    # The provider, not its value: this block outlives the token it starts
    # with, and every gh and git subprocess inside asks for a live one.
    with gh_identity(tokens.token if tokens else None):
        if kind == "notice":
            if report:
                ingest._comment(slug, d.number, FORK_NOTICE)
            return "fork-skipped"
        pr = ingest._pr_view(slug, d.number)
        if pr is None:
            return "gone"
        head = pr["headRefOid"]
        if not claims.claim(d.number, head):
            return "duplicate"
        try:
            res = ingest.ingest_pr(engine, slug, claims, pr,
                                   report=report, claimed=True)
        except BaseException:
            claims.release(d.number, head)   # retryable now, not in 30 min
            raise
        verdict = res[1] if res else "skipped"
        claims.finalize(d.number, head, verdict)
        return verdict


def _worker_loop(q: queue.Queue, engine: Engine, slug: str,
                 tokens: TokenCache | None, claims: IngestState, report: bool,
                 note: Callable[[str], None] = _stamp,
                 stop: threading.Event | None = None) -> None:
    """Land queue items until `stop` is set (or a None sentinel arrives).

    Stop-aware rather than sentinel-only: the queue holds up to `queue_max`
    pending landings, and a worker that had to reach a sentinel at the back of
    a full queue would keep landing PRs for as long as the backlog took —
    long after `run_gateway` returned and a supervisor felt free to SIGKILL
    the process mid-git-operation. The sentinel only wakes a blocked get."""
    while stop is None or not stop.is_set():
        try:
            item = q.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            if item is None:
                return
            try:
                note(f"  PR #{item[1].number}: "
                     f"{_work(item, engine, slug, tokens, claims, report)}")
            except Exception as ex:
                # A worker thread never dies: the next delivery still lands.
                note(f"  ! PR #{item[1].number}: {type(ex).__name__}: "
                     f"{redact(str(ex))[:150]}")
        finally:
            q.task_done()


def _sweeper_loop(stop: threading.Event, interval: float, q: queue.Queue,
                  slug: str, installation_id: int, tokens: TokenCache | None,
                  fork_policy: str = "gate",
                  note: Callable[[str], None] = _stamp) -> None:
    """Re-offer every open PR on a slow timer.

    GitHub does not retry a failed delivery, so one dropped while the gateway
    was down is gone. The claim makes an already-landed head free, which is
    what lets this run without a second thought."""
    first = True
    while first or not stop.wait(interval):
        first = False
        try:
            with gh_identity(tokens.token if tokens else None):
                prs = ingest._list_prs(slug)
        except Exception as ex:
            note(f"  ! sweep failed: {redact(str(ex))[:150]}")
            continue
        for pr in prs:
            d = _delivery_from_pr(pr, slug, installation_id)
            if d.is_fork and fork_policy != "gate":
                continue
            try:
                q.put_nowait(("land", d))
            except queue.Full:
                break


def _install_signals(stop: threading.Event) -> None:
    def handler(signum, frame):
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass      # not the main thread — the caller drives `stop` itself


# ------------------------------------------------------------------ run ----

def run_gateway(args) -> int:
    try:
        engine = Engine(getattr(args, "repo", "."))
    except RuntimeError as ex:
        print(ex)
        return 2
    cfg = _config(engine)
    for key, attr in (("bind", "bind"), ("port", "port"), ("path", "path"),
                      ("app_client_id", "app_client_id"),
                      ("installation_id", "installation_id"),
                      ("private_key_path", "private_key"),
                      ("workers", "workers"), ("sweep_s", "sweep")):
        value = getattr(args, attr, None)
        if value not in (None, ""):
            cfg[key] = value
    if getattr(args, "land_forks", False):
        cfg["fork_policy"] = "gate"
    if not str(cfg["path"]).startswith("/"):
        cfg["path"] = "/" + str(cfg["path"])

    slug = getattr(args, "github", None) or slug_from_origin(engine.repo)
    if not slug:
        print("no GitHub repo: pass --github owner/repo or set an origin remote")
        return 2

    err, setup = _preflight(engine, cfg, args, slug)
    if err:
        print(err)
        return 2
    tokens = setup["tokens"]
    if getattr(args, "check", False):
        print(f"gateway: ready — {slug} → {engine.config['branch']} "
              f"(nothing bound; drop --check to serve)")
        return 0

    report = not getattr(args, "no_report", False)
    claims = IngestState(engine.state_dir,
                         stale_s=(int(cfg["claim_stale_s"])
                                  or engine.config["gate_timeout_s"] * 2))
    q: queue.Queue = queue.Queue(maxsize=int(cfg["queue_max"]))

    def sink(d) -> bool:
        try:
            q.put_nowait(("land", d))
            return True
        except queue.Full:
            return False

    def notify(d) -> bool:
        try:
            q.put_nowait(("notice", d))
            return True
        except queue.Full:
            return False

    if report:
        with gh_identity(tokens.token):
            ingest._ensure_labels(slug)

    rcfg = webhook.ReceiverConfig(
        secret=setup["secret"], path=str(cfg["path"]), slug=slug,
        installation_id=int(cfg["installation_id"]),
        app_slug=str(cfg["app_slug"]), skip_drafts=bool(cfg["skip_drafts"]),
        fork_policy=str(cfg["fork_policy"]),
        max_body_bytes=int(cfg["max_body_bytes"]))
    srv = webhook.build_server(rcfg, sink, notify, bind=str(cfg["bind"]),
                               port=int(cfg["port"]), note=_stamp)

    stop = getattr(args, "stop_event", None) or threading.Event()
    _install_signals(stop)
    workers = [threading.Thread(target=_worker_loop, name=f"land-{i}",
                                args=(q, engine, slug, tokens, claims, report),
                                kwargs={"stop": stop})
               for i in range(max(1, int(cfg["workers"])))]
    for t in workers:
        t.start()
    # serve_forever runs off the main thread: shutdown() called from inside
    # the serving thread deadlocks, and the signal handler runs on main.
    serving = threading.Thread(target=srv.serve_forever,
                               kwargs={"poll_interval": 0.5}, daemon=True)
    serving.start()
    if cfg["sweep_s"] and not getattr(args, "no_sweep", False):
        threading.Thread(target=_sweeper_loop, daemon=True,
                         args=(stop, float(cfg["sweep_s"]), q, slug,
                               int(cfg["installation_id"]), tokens,
                               str(cfg["fork_policy"]))).start()

    host, port = srv.server_address[0], srv.server_address[1]
    client = str(cfg["app_client_id"])
    print(f"gateway: {slug} → {engine.config['branch']}")
    print(f"  listening   {host}:{port}{cfg['path']}")
    print(f"  identity    app {client[:8]}… installation "
          f"{cfg['installation_id']}")
    sweep = ("off" if getattr(args, "no_sweep", False)
             else f"every {cfg['sweep_s']}s")
    print(f"  workers     {len(workers)}   sweep {sweep}   "
          f"forks: {cfg['fork_policy']}   "
          f"isolation: {engine.config.get('isolation', 'none')}", flush=True)

    try:
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        stop.set()
    srv.shutdown()
    srv.server_close()
    for _ in workers:
        try:
            q.put_nowait(None)   # wake a blocked get; never wait for room
        except queue.Full:
            pass
    for t in workers:
        # Bounded because the loop is stop-aware: a worker finishes the
        # landing in hand and abandons the backlog. A landing must not be cut
        # off mid-git-operation, and nothing may still be landing after this.
        t.join(timeout=60)
    print("gateway: stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("use: cafecito gateway")
