"""cafecito/model.py — the provider seam.

Every text-in/text-out model call in cafecito goes through `call()`. Two backends:

  api  — HTTPS to the Anthropic Messages API, standard library only
  cli  — shell out to the `claude` CLI, which is what cafecito did before this

`auto` (the default) prefers the API when a credential is present and falls back
to the CLI, so an install that has neither config nor key keeps behaving exactly
as it did. The reconciler in particular used to die with "reconciler unavailable:
`claude` CLI not on PATH" on any machine without Anthropic's CLI — that is our
headline differentiator gated on an undeclared dependency, and an API key now
clears it.

Why raw HTTP rather than the `anthropic` SDK: `cafecito/` is stdlib-only by
project rule. Zero runtime dependencies is a product feature and it is what lets
the whole package be vendored into an editor plugin with no install step.

NOT covered here: the swarm's *worker* agents. A worker edits files, so it needs
an agentic tool loop rather than a single completion. Workers stay on the CLI
until that loop exists; see `swarm._run_worker`.

Environment:
  ANTHROPIC_API_KEY         preferred credential
  ANTHROPIC_AUTH_TOKEN      OAuth access token (used only if no API key)
  ANTHROPIC_BASE_URL        override the API host
  CAFECITO_MODEL_BACKEND    auto (default) | api | cli
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from typing import NamedTuple

API_VERSION = "2023-06-01"
DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_MAX_TOKENS = 16000
DEFAULT_TIMEOUT_S = 300
MAX_ATTEMPTS = 3
RETRY_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})
MAX_BACKOFF_S = 30.0

# Short names the `claude` CLI accepts, mapped to Messages API model ids. A name
# that isn't a key here passes through unchanged, so a full id ("claude-opus-5")
# works in config today and a model released tomorrow works without a release of
# ours. `reconciler_model` has defaulted to "sonnet" since v0.1 — that is why
# these aliases exist at all.
ALIASES = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
    "fable": "claude-fable-5",
}


class ModelError(RuntimeError):
    """Any failure to obtain a completion.

    Deliberately a RuntimeError: every existing call site already catches
    RuntimeError around the CLI, so routing through this module does not widen
    anyone's exception handling.
    """


class Result(NamedTuple):
    """A completion plus the accounting the caller needs to make it cheaper.

    Token counts are what model routing will be built on — record them at the
    call site rather than re-deriving spend later.
    """

    text: str
    model: str
    backend: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None
    seconds: float


def resolve(name: str) -> str:
    """Alias -> Messages API model id. Unknown names pass through unchanged."""
    return ALIASES.get(name, name)


def credential() -> dict | None:
    """Auth headers, or None when no credential is present.

    Resolution order matches the Anthropic SDKs: API key first, then an OAuth
    access token — which rides on `Authorization` rather than `x-api-key` and
    needs its own beta header.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return {"x-api-key": key}
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if token:
        return {"authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20"}
    return None


def choose_backend() -> str:
    want = os.environ.get("CAFECITO_MODEL_BACKEND", "auto")
    if want == "cli":
        return "cli"
    if want == "api":
        if not credential():
            raise ModelError("CAFECITO_MODEL_BACKEND=api but neither "
                             "ANTHROPIC_API_KEY nor ANTHROPIC_AUTH_TOKEN is set")
        return "api"
    if want != "auto":
        raise ModelError(f"unknown CAFECITO_MODEL_BACKEND {want!r} "
                         "(expected auto, api, or cli)")
    return "api" if credential() else "cli"


def base_url() -> str:
    return os.environ.get("ANTHROPIC_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def call(prompt: str, *, model: str, system: str | None = None,
         max_tokens: int = DEFAULT_MAX_TOKENS, timeout: int = DEFAULT_TIMEOUT_S,
         effort: str | None = None) -> Result:
    """One completion. Raises ModelError on any failure to produce text.

    FileNotFoundError and subprocess.TimeoutExpired from the CLI backend are
    deliberately NOT wrapped: callers distinguish "no CLI installed" from "the
    model failed", and that distinction is asserted in the regen tests.
    """
    backend = choose_backend()
    t0 = time.time()
    if backend == "api":
        text, usage, stop = _call_api(prompt, model, system, max_tokens,
                                      timeout, effort)
    else:
        text, usage, stop = _call_cli(prompt, model, system, timeout)
    return Result(text=text, model=model, backend=backend,
                  input_tokens=int(usage.get("input_tokens", 0)),
                  output_tokens=int(usage.get("output_tokens", 0)),
                  stop_reason=stop, seconds=round(time.time() - t0, 2))


def _call_api(prompt, model, system, max_tokens, timeout, effort):
    headers = {"content-type": "application/json",
               "anthropic-version": API_VERSION}
    headers.update(credential() or {})
    body = {"model": resolve(model),
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}]}
    if system:
        body["system"] = system
    if effort:
        body["output_config"] = {"effort": effort}
    data = json.dumps(body).encode()
    url = base_url() + "/v1/messages"

    delay, payload = 1.0, None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.loads(r.read().decode())
            break
        except urllib.error.HTTPError as e:  # a subclass of OSError — keep first
            if e.code in RETRY_STATUS and attempt < MAX_ATTEMPTS:
                time.sleep(_retry_after(e, delay))
                delay = min(delay * 2, MAX_BACKOFF_S)
                continue
            raise ModelError(
                f"messages API {e.code}: {_http_detail(e)}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < MAX_ATTEMPTS:
                time.sleep(delay)
                delay = min(delay * 2, MAX_BACKOFF_S)
                continue
            raise ModelError(
                f"messages API unreachable: {str(e)[:160]}") from None
        except ValueError as e:  # malformed JSON body
            raise ModelError(f"messages API returned non-JSON: {e}") from None

    stop = payload.get("stop_reason")
    if stop == "refusal":
        detail = (payload.get("stop_details") or {}).get("category") or "unspecified"
        raise ModelError(f"model declined the request (refusal: {detail})")
    # Thinking blocks come back with empty text unless display is requested, so
    # selecting text blocks is enough on every current model.
    text = "".join(b.get("text", "") for b in payload.get("content", [])
                   if b.get("type") == "text")
    if not text.strip():
        raise ModelError(f"messages API returned no text (stop_reason={stop})")
    return text, payload.get("usage") or {}, stop


def _call_cli(prompt, model, system, timeout):
    # Prompt goes on stdin, never argv: a reconciler prompt runs to MAX_PROMPT
    # (80k) and a planner's repo listing is unbounded, both well past ARG_MAX.
    if system:
        prompt = f"{system}\n\n{prompt}"
    r = subprocess.run(["claude", "-p", "--model", model], input=prompt,
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise ModelError(f"claude CLI failed: {r.stderr.strip()[:200]}")
    return r.stdout, {}, None


def _retry_after(err, fallback: float) -> float:
    try:
        return min(max(float(err.headers.get("retry-after", "")), 0.0),
                   MAX_BACKOFF_S)
    except (AttributeError, TypeError, ValueError):
        return fallback


def _http_detail(err) -> str:
    try:
        body = json.loads(err.read().decode())
        return str((body.get("error") or {}).get("message") or body)[:160]
    except (AttributeError, ValueError, OSError):
        return (err.reason or "")[:160] if hasattr(err, "reason") else ""
