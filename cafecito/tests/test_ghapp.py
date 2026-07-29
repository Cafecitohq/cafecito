"""GitHub App credentials: the JWT, the key, and the installation token.

This is the first thing in cafecito that holds a private key, so most of what
is pinned here is a refusal — and one signature that really verifies. No
network: the single POST is behind one seam.
"""

import json
import subprocess
import time
import types

import pytest

from cafecito import ghapp
from cafecito.ghapp import GhAppError

INSTALL = 12345678


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


def test_jwt_structure(monkeypatch):
    monkeypatch.setattr(ghapp, "_openssl_sign", lambda *a, **k: b"sig")
    now = 1_700_000_000
    token = ghapp.app_jwt("Iv23liAbCdEf", b"pem", now=now)
    head, claims, _ = token.split(".")

    def dec(seg):
        import base64
        return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))

    assert dec(head) == {"alg": "RS256", "typ": "JWT"}
    c = dec(claims)
    assert isinstance(c["iss"], str) and c["iss"] == "Iv23liAbCdEf"
    assert c["exp"] - c["iat"] == 600      # 9 minutes forward, 1 back
    assert c["exp"] - now == 540           # never the documented 10-min max


def test_a_numeric_app_id_is_issued_as_an_integer(monkeypatch):
    """Observed against the live API: quoting a numeric app id earns
    `'Issuer' claim ('iss') must be an Integer`. A client ID is a string and
    an app id is an int, and GitHub is strict about which is which."""
    monkeypatch.setattr(ghapp, "_openssl_sign", lambda *a, **k: b"sig")

    def issuer(value):
        import base64
        seg = ghapp.app_jwt(value, b"pem").split(".")[1]
        return json.loads(base64.urlsafe_b64decode(
            seg + "=" * (-len(seg) % 4)))["iss"]

    assert issuer("123456") == 123456
    assert issuer(123456) == 123456
    assert issuer("Iv23liAbCdEf") == "Iv23liAbCdEf"


def test_base64url_is_unpadded_and_url_safe(monkeypatch):
    assert ghapp._b64u(b"") == ""
    assert ghapp._b64u(b"\xfb\xff") == "-_8"
    assert ghapp._b64u(b"a") == "YQ"
    monkeypatch.setattr(ghapp, "_openssl_sign", lambda *a, **k: bytes(range(64)))
    token = ghapp.app_jwt("Iv23li", b"pem")
    assert not set("=+/") & set(token)


def test_openssl_signature_is_deterministic_and_verifies(pem, tmp_path):
    key = pem.read_bytes()
    a = ghapp._openssl_sign(key, b"cafecito")
    b = ghapp._openssl_sign(key, b"cafecito")
    # PKCS#1 v1.5 is deterministic — identical across LibreSSL and OpenSSL 3
    assert a == b and len(a) == 256
    pub = tmp_path / "pub.pem"
    subprocess.run(["openssl", "rsa", "-in", str(pem), "-pubout", "-out",
                    str(pub)], check=True, capture_output=True)
    sig = tmp_path / "sig.bin"
    sig.write_bytes(a)
    r = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(pub),
                        "-signature", str(sig)], input=b"cafecito",
                       capture_output=True)
    assert r.returncode == 0 and b"Verified OK" in r.stdout


def test_encrypted_key_is_refused_before_openssl_runs(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("openssl would prompt on /dev/tty and hang")

    monkeypatch.setattr(ghapp.subprocess, "run", boom)
    p = tmp_path / "enc.pem"
    p.write_text("-----BEGIN ENCRYPTED PRIVATE KEY-----\nzz\n"
                 "-----END ENCRYPTED PRIVATE KEY-----\n")
    p.chmod(0o600)
    with pytest.raises(GhAppError) as ex:
        ghapp.load_private_key(str(p))
    assert "passphrase" in str(ex.value)


def test_world_readable_key_is_refused_and_the_mode_is_named(tmp_path):
    p = tmp_path / "app.pem"
    p.write_text("-----BEGIN RSA PRIVATE KEY-----\nzz\n"
                 "-----END RSA PRIVATE KEY-----\n")
    p.chmod(0o644)
    with pytest.raises(GhAppError) as ex:
        ghapp.load_private_key(str(p))
    assert "0644" in str(ex.value) and "chmod 600" in str(ex.value)


def test_an_oversized_key_is_refused_instead_of_hanging(tmp_path, monkeypatch):
    """The PEM is written into the pipe BEFORE openssl is spawned to read it,
    so a key/cert bundle larger than the pipe buffer blocks in os.write —
    and SIGN_TIMEOUT_S guards the subprocess, not the write. The symptom was
    a gateway that hung in preflight with no diagnostic at all."""
    p = tmp_path / "bundle.pem"
    p.write_bytes(b"-----BEGIN RSA PRIVATE KEY-----\n" + b"z" * (300 << 10)
                  + b"\n-----END RSA PRIVATE KEY-----\n")
    p.chmod(0o600)
    with pytest.raises(GhAppError) as ex:
        ghapp.load_private_key(str(p))
    assert "bundle" in str(ex.value) and str(p) in str(ex.value)

    def boom(*a, **k):
        raise AssertionError("openssl started with a key that cannot fit")

    monkeypatch.setattr(ghapp.subprocess, "run", boom)
    with pytest.raises(GhAppError):
        ghapp._openssl_sign(p.read_bytes(), b"x")


def test_the_key_never_reaches_argv(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        return types.SimpleNamespace(returncode=0, stdout=b"sig", stderr=b"")

    monkeypatch.setattr(ghapp.subprocess, "run", fake_run)
    ghapp._openssl_sign(b"-----BEGIN RSA PRIVATE KEY-----\nsecret\n", b"x")
    argv, kw = calls[0]
    assert not any("BEGIN" in a or "secret" in a for a in argv)
    assert argv[-1].startswith("/dev/fd/")     # `ps` is world-readable
    assert kw["timeout"] == ghapp.SIGN_TIMEOUT_S


def _post_seam(monkeypatch, expires_in=3600, status=201, extra=None):
    posts = []

    def fake_post(url, body, headers, timeout=15):
        posts.append({"url": url, "body": body, "headers": headers})
        exp = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(time.time() + expires_in))
        data = {"token": "ghs_" + "A" * 30, "expires_at": exp}
        return status, (extra if extra is not None else data)

    monkeypatch.setattr(ghapp, "_api_post", fake_post)
    monkeypatch.setattr(ghapp, "_openssl_sign", lambda *a, **k: b"sig")
    return posts


def test_token_is_cached_until_it_is_nearly_expired(monkeypatch):
    posts = _post_seam(monkeypatch)
    tc = ghapp.TokenCache("Iv23li", b"pem", INSTALL, "widget")
    assert tc.token().startswith("ghs_")
    assert tc.token().startswith("ghs_")
    assert len(posts) == 1


def test_a_token_inside_the_margin_is_re_minted(monkeypatch):
    posts = _post_seam(monkeypatch, expires_in=120)   # margin is 300s
    tc = ghapp.TokenCache("Iv23li", b"pem", INSTALL, "widget")
    tc.token()
    tc.token()
    assert len(posts) == 2


def test_an_unreadable_expiry_falls_back_to_an_hour(monkeypatch):
    """`expires_at` is a string GitHub sends; a missing or reshaped one must
    not turn into a token that is treated as immortal (or already dead)."""
    now = time.time()
    assert ghapp._expiry(None, now + 3600) == now + 3600
    assert ghapp._expiry("not-a-timestamp", now + 3600) == now + 3600
    assert ghapp._expiry(1700000000, now + 3600) == now + 3600
    assert ghapp._expiry("2023-11-14T22:13:20Z", 0.0) == 1_700_000_000.0
    _post_seam(monkeypatch, extra={"token": "ghs_" + "A" * 30})
    _, exp = ghapp.mint_installation_token("Iv23li", b"pem", INSTALL, "widget")
    assert now + 3000 < exp < now + 4200


def test_a_refused_exchange_names_the_status_and_nothing_else(monkeypatch):
    _post_seam(monkeypatch, status=401,
               extra={"message": "A JWT could not be decoded"})
    with pytest.raises(GhAppError) as ex:
        ghapp.mint_installation_token("Iv23li", b"secret-pem", INSTALL, "w")
    text = str(ex.value)
    assert "401" in text and "clock skew" in text
    assert "secret-pem" not in text and "ghs_" not in text


def test_tokens_are_redacted_and_never_in_a_repr(monkeypatch):
    tok = "ghs_" + "B" * 30
    assert ghapp.redact(f"failed with {tok} here") == "failed with *** here"
    assert ghapp.redact("password=hunter22", "hunter22") == "password=***"
    _post_seam(monkeypatch)
    tc = ghapp.TokenCache("Iv23li", b"pem", INSTALL, "widget")
    tc.token()
    assert "ghs_" not in repr(tc)


def test_the_exchange_is_scoped_to_one_repo_and_three_permissions(monkeypatch):
    posts = _post_seam(monkeypatch)
    ghapp.mint_installation_token("Iv23li", b"pem", INSTALL, "widget")
    p = posts[0]
    assert p["url"] == (f"{ghapp.API}/app/installations/{INSTALL}"
                        "/access_tokens")
    assert p["headers"]["Authorization"].startswith("Bearer ")
    assert p["headers"]["Accept"] == "application/vnd.github+json"
    assert p["headers"]["X-GitHub-Api-Version"] == ghapp.API_VERSION
    assert p["body"]["repositories"] == ["widget"]
    assert p["body"]["permissions"] == {"contents": "read", "issues": "write",
                                        "pull_requests": "write"}
