"""cafecito ghapp — GitHub App identity, stdlib only.

This is the first thing in cafecito that holds a private key, so it imports
nothing from the package: like webhook.py it is reviewable on its own.

Two steps. Sign an RS256 JWT with the App's PEM to authenticate AS the app,
then exchange that JWT for an installation access token, which is what every
repository call actually uses. RS256 is RSASSA-PKCS1-v1_5 over SHA-256, which
`openssl dgst -sha256 -sign` produces directly — the same external-binary
class as git, gh and claude, and the reason no Python dependency joins the
package to sign a 452-byte string. `dgst -sha256 -sign` is the one spelling
both LibreSSL and OpenSSL 3 accept; `pkeyutl -rawin` is OpenSSL-3-only.

The key never reaches argv (`ps` is world-readable) and never reaches disk:
it is written to a pipe and named to openssl as /dev/fd/N. Encrypted keys are
refused before any subprocess starts, because both toolchains prompt for the
passphrase on /dev/tty — not stdin — and would otherwise block forever.

Installation tokens expire in an hour, which is a lifecycle the polling
design never had: TokenCache re-mints five minutes early, and callers hold
the cache rather than a token string, because a landing can easily outlive
the credential it started with.

Not yet: the failed-delivery replay API (`GET /app/hook/deliveries`), the
`/meta` hook-IP allowlist, and multi-installation routing — one process
serves one installation, and its tokens are scoped to one repository.
"""

from __future__ import annotations

import base64
import calendar
import json
import os
import pathlib
import re
import stat
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
API_VERSION = "2022-11-28"
# GitHub's own sample expires at exactly ten minutes, the documented maximum,
# and 401s the moment our clock runs a second fast. Nine, backdated one.
JWT_TTL_S = 540
JWT_BACKDATE_S = 60
TOKEN_MARGIN_S = 300
SIGN_TIMEOUT_S = 10
# The PEM is written into a pipe BEFORE openssl is spawned to read it, so a
# key larger than the pipe buffer (16 KiB on macOS, 64 KiB on Linux) would
# block in os.write forever — SIGN_TIMEOUT_S guards the subprocess, not the
# write. A 4096-bit PKCS#1 key is under 4 KB; nothing legitimate is close.
MAX_KEY_BYTES = 8 << 10

# `gh label create` sits under Issues, not Pull requests — a split that a
# PAT's blanket `repo` scope hides and an App's per-resource grant does not.
PERMISSIONS = {"contents": "read", "issues": "write", "pull_requests": "write"}

_SECRET_RE = re.compile(
    r"gh[psuor]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}")


class GhAppError(RuntimeError):
    """Something in the credential path failed. The message is redacted."""


def redact(text: str, *extra: str) -> str:
    """Mask anything that looks like a token, plus secrets the caller names.
    Everything on the credential path goes through this before it is printed
    or raised — a leaked ghs_ token is an hour of write access."""
    out = _SECRET_RE.sub("***", str(text))
    for s in extra:
        if s and len(s) >= 8:
            out = out.replace(s, "***")
    return out


def load_private_key(path: str) -> bytes:
    """The App's PEM, with the four refusals that matter."""
    p = pathlib.Path(path).expanduser()
    try:
        pem = p.read_bytes()
        mode = p.stat().st_mode
    except OSError as ex:
        raise GhAppError(f"cannot read private key {p}: {ex.strerror}")
    if b"PRIVATE KEY" not in pem:
        raise GhAppError(f"{p} is not a PEM private key")
    if len(pem) > MAX_KEY_BYTES:
        raise GhAppError(f"{p} is {len(pem)} bytes — an App private key is "
                         f"under {MAX_KEY_BYTES // 1024} KB. Point "
                         f"--private-key at the .pem GitHub downloaded, not "
                         f"a key/certificate bundle")
    if b"ENCRYPTED" in pem:
        raise GhAppError(f"{p} is passphrase-protected — openssl would prompt "
                         "on /dev/tty and the gateway would hang; decrypt it "
                         "or download a fresh key")
    if stat.S_IMODE(mode) & 0o077:
        raise GhAppError(f"{p} is mode {stat.S_IMODE(mode):04o} — readable by "
                         f"others; chmod 600 {p}")
    return pem


def _b64u(raw: bytes) -> str:
    """Unpadded base64url. Never `openssl base64`: wrong alphabet, `=`
    padding, and 64-column wrapping."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _openssl_sign(pem: bytes, data: bytes, openssl: str = "openssl") -> bytes:
    """RS256 over `data`. The key goes down a pipe and is named to openssl as
    a /dev/fd path, so it never appears in argv or on disk."""
    if len(pem) > MAX_KEY_BYTES:
        # Checked here too, not only in load_private_key: the write below
        # happens before there is a reader, and an oversized key hangs with
        # no timeout to catch it.
        raise GhAppError(f"refusing to sign with a {len(pem)}-byte key — "
                         f"that is not an App private key")
    r, w = os.pipe()
    tmp = None
    try:
        os.write(w, pem)      # bounded above; the smallest pipe buffer is 16KB
        os.close(w)
        w = -1
        keyarg = f"/dev/fd/{r}"
        pass_fds: tuple[int, ...] = (r,)
        if not os.path.exists("/dev/fd"):   # minimal container without /proc
            fd, tmp = tempfile.mkstemp(prefix="cafecito-key-")
            os.fchmod(fd, 0o600)
            os.write(fd, pem)
            os.close(fd)
            keyarg, pass_fds = tmp, ()
        try:
            proc = subprocess.run(
                [openssl, "dgst", "-sha256", "-sign", keyarg],
                input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=pass_fds, timeout=SIGN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise GhAppError("openssl did not finish signing — a "
                             "passphrase-protected key prompts on /dev/tty")
        except OSError as ex:
            raise GhAppError(f"cannot run {openssl}: {ex}")
        if proc.returncode != 0:
            why = redact(proc.stderr.decode(errors="replace")).strip()
            raise GhAppError(f"openssl could not sign: {why[:150]}")
        if not proc.stdout:
            raise GhAppError("openssl produced an empty signature")
        return proc.stdout
    finally:
        if w != -1:
            os.close(w)
        os.close(r)
        if tmp:
            os.unlink(tmp)


def app_jwt(client_id: str, pem: bytes, now: float | None = None,
            openssl: str = "openssl") -> str:
    """A JWT that authenticates as the App.

    GitHub takes either identifier for `iss` but types them differently, and
    it is strict about it: a client ID is a string (`Iv23li…`), while an app
    id must arrive as a JSON integer. Quoting a numeric app id earns
    `'Issuer' claim ('iss') must be an Integer` — observed against the live
    API — so send whichever the operator pasted, in its own type."""
    ts = int(time.time() if now is None else now)
    issuer: str | int = str(client_id)
    if issuer.isdigit():
        issuer = int(issuer)
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {"exp": ts + JWT_TTL_S, "iat": ts - JWT_BACKDATE_S,
              "iss": issuer}

    def seg(obj) -> str:
        return _b64u(json.dumps(obj, separators=(",", ":"),
                                sort_keys=True).encode())

    signing_input = f"{seg(header)}.{seg(claims)}".encode("ascii")
    return (signing_input.decode("ascii") + "."
            + _b64u(_openssl_sign(pem, signing_input, openssl)))


def _api_post(url: str, body: dict | None, headers: dict,
              timeout: int = 15) -> tuple[int, dict]:
    """POST JSON, return (status, parsed). The one network seam in this
    module, so tests replace exactly this."""
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as ex:
        raw = ex.read().decode("utf-8", errors="replace")
        try:
            return ex.code, json.loads(raw) if raw.strip() else {}
        except ValueError:
            return ex.code, {"message": raw[:150]}
    except (urllib.error.URLError, OSError, ValueError) as ex:
        raise GhAppError(f"could not reach {API}: {redact(str(ex))[:150]}")


def _expiry(value, fallback: float) -> float:
    if isinstance(value, str):
        try:
            return float(calendar.timegm(
                time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
        except ValueError:
            pass
    return fallback


def mint_installation_token(client_id: str, pem: bytes, installation_id: int,
                            repo_name: str = "",
                            openssl: str = "openssl") -> tuple[str, float]:
    """Exchange an App JWT for an installation access token, scoped down to
    one repository and three permissions — an installation covering fifty
    repos must not hand this process a token covering fifty repos."""
    jwt = app_jwt(client_id, pem, openssl=openssl)
    body: dict = {"permissions": dict(PERMISSIONS)}
    if repo_name:
        body["repositories"] = [repo_name]
    status, data = _api_post(
        f"{API}/app/installations/{int(installation_id)}/access_tokens",
        body,
        {"Authorization": f"Bearer {jwt}",
         "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": API_VERSION,
         "Content-Type": "application/json",
         "User-Agent": "cafecito-gateway"})
    if status != 201:
        message = data.get("message") if isinstance(data, dict) else ""
        hint = ("; a 401 here is usually clock skew or the wrong client id"
                if status == 401 else "")
        raise GhAppError(f"installation token refused ({status}): "
                         f"{redact(str(message))[:150]}{hint}")
    token = data.get("token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        raise GhAppError("installation token response carried no token")
    return token, _expiry(data.get("expires_at"), time.time() + 3600)


class TokenCache:
    """One installation's token, re-minted before it dies.

    Callers hold the cache and pass `cache.token` — the bound method, not its
    value. A landing runs for minutes and reports at the end; a token string
    snapshotted at the start is exactly the credential that has gone stale by
    the time the verdict is posted."""

    def __init__(self, client_id: str, pem: bytes, installation_id: int,
                 repo_name: str = "", openssl: str = "openssl", mint=None):
        self._client_id = client_id
        self._pem = pem
        self._installation_id = installation_id
        self._repo_name = repo_name
        self._openssl = openssl
        self._mint = mint
        self._lock = threading.Lock()
        self._token = ""
        self._expires = 0.0

    def token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires - TOKEN_MARGIN_S:
                return self._token
            mint = self._mint or mint_installation_token
            self._token, self._expires = mint(
                self._client_id, self._pem, self._installation_id,
                self._repo_name, self._openssl)
            return self._token

    def __repr__(self) -> str:
        return (f"<TokenCache installation={self._installation_id} "
                f"exp={int(self._expires)}>")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("use: cafecito gateway")
