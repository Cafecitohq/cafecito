"""The provider seam. No test here opens a socket — the API backend is
exercised by monkeypatching urlopen, which also keeps these green inside our
own sandboxed gate (it denies network*, see HANDBOOK 11b)."""

import io
import json
import urllib.error

import pytest

from cafecito import model, regen, swarm

ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
       "CAFECITO_MODEL_BACKEND")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """A developer's real key must never decide what these assert."""
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def ok_payload(text="hello", stop="end_turn", extra_blocks=()):
    return {"content": [*extra_blocks, {"type": "text", "text": text}],
            "stop_reason": stop,
            "usage": {"input_tokens": 11, "output_tokens": 7}}


def http_error(code, body=None, headers=None):
    fp = io.BytesIO(json.dumps(body or {}).encode())
    return urllib.error.HTTPError("https://api.anthropic.com/v1/messages",
                                  code, "err", headers or {}, fp)


def capture_urlopen(monkeypatch, *responses):
    """Queue responses; raise them if they're exceptions. Records requests."""
    seen = []
    queue = list(responses)

    def fake(req, timeout=None):
        seen.append((req, timeout))
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(model.urllib.request, "urlopen", fake)
    monkeypatch.setattr(model.time, "sleep", lambda _s: None)
    return seen


# ------------------------------------------------------------------ aliases ---

def test_resolve_maps_cli_shortnames():
    assert model.resolve("sonnet") == "claude-sonnet-5"
    assert model.resolve("opus") == "claude-opus-5"


def test_resolve_passes_through_full_ids():
    """A model released after us must work without a release of ours."""
    assert model.resolve("claude-opus-5") == "claude-opus-5"
    assert model.resolve("something-new") == "something-new"


# -------------------------------------------------------------- credentials ---

def test_api_key_wins_over_oauth_token(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "oauth-test")
    assert model.credential() == {"x-api-key": "sk-test"}


def test_oauth_token_uses_bearer_and_beta_header(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "oauth-test")
    cred = model.credential()
    assert cred["authorization"] == "Bearer oauth-test"
    assert cred["anthropic-beta"] == "oauth-2025-04-20"
    assert "x-api-key" not in cred


def test_no_credential_is_none():
    assert model.credential() is None


# ------------------------------------------------------------------ backend ---

def test_auto_prefers_api_when_key_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert model.choose_backend() == "api"


def test_auto_falls_back_to_cli_without_key():
    """The no-config install must keep behaving exactly as it did."""
    assert model.choose_backend() == "cli"


def test_explicit_api_without_credential_is_an_error(monkeypatch):
    monkeypatch.setenv("CAFECITO_MODEL_BACKEND", "api")
    with pytest.raises(model.ModelError, match="ANTHROPIC_API_KEY"):
        model.choose_backend()


def test_explicit_cli_ignores_a_present_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("CAFECITO_MODEL_BACKEND", "cli")
    assert model.choose_backend() == "cli"


def test_unknown_backend_is_an_error(monkeypatch):
    monkeypatch.setenv("CAFECITO_MODEL_BACKEND", "bedrock")
    with pytest.raises(model.ModelError, match="expected auto, api, or cli"):
        model.choose_backend()


# -------------------------------------------------------------- api backend ---

def test_api_returns_text_and_token_counts(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    seen = capture_urlopen(monkeypatch, FakeResponse(ok_payload("regenerated")))

    r = model.call("do it", model="sonnet", timeout=42)

    assert r.text == "regenerated"
    assert r.backend == "api"
    assert (r.input_tokens, r.output_tokens) == (11, 7)
    assert r.stop_reason == "end_turn"

    req, timeout = seen[0]
    assert timeout == 42
    body = json.loads(req.data)
    assert body["model"] == "claude-sonnet-5"       # alias resolved for the API
    assert body["messages"] == [{"role": "user", "content": "do it"}]
    assert "thinking" not in body                    # 400s on current models
    assert req.headers["X-api-key"] == "sk-test"
    assert req.headers["Anthropic-version"] == model.API_VERSION


def test_api_sends_system_and_effort_only_when_given(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    seen = capture_urlopen(monkeypatch, FakeResponse(ok_payload()),
                           FakeResponse(ok_payload()))

    model.call("p", model="opus")
    bare = json.loads(seen[0][0].data)
    assert "system" not in bare and "output_config" not in bare

    model.call("p", model="opus", system="be terse", effort="low")
    full = json.loads(seen[1][0].data)
    assert full["system"] == "be terse"
    assert full["output_config"] == {"effort": "low"}


def test_api_ignores_thinking_blocks(monkeypatch):
    """Thinking is on by default and returns empty text — selecting text
    blocks must not concatenate it in or trip the empty-response guard."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    payload = ok_payload("answer", extra_blocks=[{"type": "thinking",
                                                  "thinking": ""}])
    capture_urlopen(monkeypatch, FakeResponse(payload))
    assert model.call("p", model="opus").text == "answer"


def test_api_retries_a_429_then_succeeds(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    seen = capture_urlopen(monkeypatch,
                           http_error(429, headers={"retry-after": "0"}),
                           FakeResponse(ok_payload("second try")))
    assert model.call("p", model="opus").text == "second try"
    assert len(seen) == 2


def test_api_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    seen = capture_urlopen(monkeypatch, *[http_error(503)
                                          for _ in range(model.MAX_ATTEMPTS)])
    with pytest.raises(model.ModelError, match="503"):
        model.call("p", model="opus")
    assert len(seen) == model.MAX_ATTEMPTS


def test_api_does_not_retry_a_400(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    seen = capture_urlopen(monkeypatch,
                           http_error(400, {"error": {"message": "bad model"}}))
    with pytest.raises(model.ModelError, match="bad model"):
        model.call("p", model="opus")
    assert len(seen) == 1


def test_api_refusal_raises_rather_than_returning_empty(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    capture_urlopen(monkeypatch, FakeResponse(
        {"content": [], "stop_reason": "refusal",
         "stop_details": {"category": "cyber"}, "usage": {}}))
    with pytest.raises(model.ModelError, match="refusal: cyber"):
        model.call("p", model="opus")


def test_api_empty_text_raises(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    capture_urlopen(monkeypatch, FakeResponse(
        {"content": [], "stop_reason": "max_tokens", "usage": {}}))
    with pytest.raises(model.ModelError, match="no text"):
        model.call("p", model="opus")


def test_api_base_url_is_overridable(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.internal/")
    seen = capture_urlopen(monkeypatch, FakeResponse(ok_payload()))
    model.call("p", model="opus")
    assert seen[0][0].full_url == "https://proxy.internal/v1/messages"


# -------------------------------------------------------------- cli backend ---

def test_cli_puts_the_prompt_on_stdin_not_argv(monkeypatch):
    """Reconciler prompts run to 80k and planner listings are unbounded —
    both are past ARG_MAX."""
    calls = {}

    def fake_run(argv, **kw):
        calls["argv"], calls["input"] = argv, kw.get("input")
        return type("R", (), {"returncode": 0, "stdout": "out", "stderr": ""})()

    monkeypatch.setattr(model.subprocess, "run", fake_run)
    r = model.call("a" * 5000, model="sonnet")

    assert r.backend == "cli"
    assert calls["argv"] == ["claude", "-p", "--model", "sonnet"]
    assert calls["input"] == "a" * 5000


def test_cli_passes_the_unresolved_alias(monkeypatch):
    """`claude --model` wants the short name; only the API needs the full id."""
    calls = {}

    def fake_run(argv, **kw):
        calls["argv"] = argv
        return type("R", (), {"returncode": 0, "stdout": "x", "stderr": ""})()

    monkeypatch.setattr(model.subprocess, "run", fake_run)
    model.call("p", model="sonnet")
    assert "claude-sonnet-5" not in calls["argv"]


def test_cli_failure_becomes_model_error(monkeypatch):
    def fake_run(argv, **kw):
        return type("R", (), {"returncode": 1, "stdout": "",
                              "stderr": "boom"})()

    monkeypatch.setattr(model.subprocess, "run", fake_run)
    with pytest.raises(model.ModelError, match="boom"):
        model.call("p", model="sonnet")


def test_missing_cli_still_raises_filenotfound(monkeypatch):
    """Not wrapped on purpose: live_regen distinguishes 'no CLI installed'
    from 'the model failed', and reports the former differently."""
    def fake_run(argv, **kw):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(model.subprocess, "run", fake_run)
    with pytest.raises(FileNotFoundError):
        model.call("p", model="sonnet")


# ------------------------------------------------------------- call sites ----

def test_reconciler_goes_through_the_seam(monkeypatch):
    seen = {}

    def fake_call(prompt, **kw):
        seen.update(prompt=prompt, **kw)
        return model.Result("regions", "sonnet", "api", 1, 2, "end_turn", 0.1)

    monkeypatch.setattr(regen, "model_call", fake_call)
    assert regen.run_reconciler("prompt text", "sonnet", timeout=99) == "regions"
    assert seen["model"] == "sonnet" and seen["timeout"] == 99


def test_planner_goes_through_the_seam(monkeypatch):
    seen = {}

    def fake_call(prompt, **kw):
        seen.update(prompt=prompt, **kw)
        return model.Result("[]", "opus", "api", 1, 2, "end_turn", 0.1)

    monkeypatch.setattr(swarm, "model_call", fake_call)
    assert swarm._claude_plan("goal", "opus") == "[]"
    assert seen["model"] == "opus"
