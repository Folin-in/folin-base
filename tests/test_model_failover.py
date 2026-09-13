"""ModelClient failover and retry. Runs offline - the HTTP post is stubbed, no model, no DB.

A 503 "high demand" is transient by Google's own wording, so one sweep of the models is
not enough: the client must re-sweep before declaring the provider unavailable.

    ./venv/bin/python -m pytest tests/test_model_failover.py -v
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from agents.base import BadModelReply, CallMeta, ModelClient, ModelUnavailable, model_order
from agents.performance import DEFAULT_POLICY, POLICIES, CallPolicy, thinking_config
from app.enums import TrajectoryKind

_LOOP = asyncio.get_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


MODELS = ["model-a", "model-b"]
REPLY = {"report_type": "lab", "biomarkers": []}


def _reply(payload: dict) -> httpx.Response:
    body = {"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]}
    return httpx.Response(200, json=body, request=httpx.Request("POST", "https://x"))


def _fault(status: int) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": status}},
                          request=httpx.Request("POST", "https://x"))


class _Transport:
    """Serves a scripted response per call and records which model each call targeted."""

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[str] = []
        self.bodies = []
        self.clients = []

    async def post(self, url, **kw):
        self.calls.append(url.rsplit("/", 1)[-1].split(":")[0])
        self.bodies.append(kw["json"])
        step = self._script.pop(0) if self._script else _reply(REPLY)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture
def patch_http(monkeypatch):
    def apply(script):
        t = _Transport(script)

        class _Client:
            def __init__(self, *a, **kw):
                self.is_closed = False
                t.clients.append(self)
            async def post(self, *a, **kw): return await t.post(*a, **kw)
            async def aclose(self): self.is_closed = True

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        # the retry backoff is real seconds in production; the test asserts order, not waiting
        monkeypatch.setattr("agents.base.asyncio.sleep", lambda *_: _noop())
        monkeypatch.setattr("agents.base.ModelClient._log", staticmethod(
            lambda *a, **kw: _noop()))
        return t
    return apply


async def _noop():
    return None


def _client():
    return ModelClient(models=list(MODELS), api_key="test-key")


async def _call(c):
    return await c.call(system_prompt="sys", user_text="hi")


# ------------------------------------------------------------------ failover within a round

def test_falls_over_to_second_model_on_503(patch_http):
    t = patch_http([_fault(503), _reply(REPLY)])
    assert run(_call(_client())) == REPLY
    assert t.calls == ["model-a", "model-b"]


def test_provider_message_saved_and_logged_even_when_fallback_succeeds(patch_http, monkeypatch, caplog):
    error = httpx.Response(503, json={"error": {
        "code": 503, "status": "UNAVAILABLE", "message": "The model is overloaded. Please try again later.",
    }}, request=httpx.Request("POST", "https://x"))
    t = patch_http([error, _reply(REPLY)])
    log = AsyncMock()
    monkeypatch.setattr(ModelClient, "_log", staticmethod(log))
    assert run(_call(_client())) == REPLY
    attempt = log.call_args.args[1]["attempts"][0]
    assert attempt["provider_error"]["status"] == "UNAVAILABLE"
    assert "overloaded" in attempt["provider_error"]["message"]
    assert "overloaded" in caplog.text
    assert t.calls == MODELS


def test_provider_secret_not_exposed_in_logs_or_final_exception(patch_http, monkeypatch, caplog):
    error = httpx.Response(503, json={"error": {
        "status": "UNAVAILABLE", "message": "Service unavailable for test-key",
    }}, request=httpx.Request("POST", "https://x?key=test-key"))
    patch_http([error] * 10)
    log = AsyncMock()
    monkeypatch.setattr(ModelClient, "_log", staticmethod(log))
    with pytest.raises(ModelUnavailable) as exc:
        run(_call(_client()))
    assert "test-key" not in str(exc.value)
    assert "test-key" not in caplog.text
    assert "test-key" not in json.dumps(log.call_args.args[1]["attempts"])


def test_timeout_logging_does_not_invent_provider_response(patch_http, monkeypatch, caplog):
    patch_http([httpx.ReadTimeout("secret URL or payload"), TimeoutError()])
    log = AsyncMock()
    monkeypatch.setattr(ModelClient, "_log", staticmethod(log))
    with pytest.raises(ModelUnavailable):
        run(_call(_client()))
    attempts = log.call_args.args[1]["attempts"]
    assert all("provider_error" not in a and a["http_status"] is None for a in attempts)
    assert "secret URL or payload" not in caplog.text


# ------------------------------------------------------------------ the retry rounds

def test_retries_after_every_model_503s(patch_http):
    """Round 1 exhausts both models; the client must sweep again rather than give up."""
    t = patch_http([_fault(503), _fault(503), _reply(REPLY)])
    assert run(_call(_client())) == REPLY
    assert t.calls == ["model-a", "model-b", "model-a"]


def test_read_timeout_does_not_repeat_the_same_model(patch_http):
    t = patch_http([httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow"), _reply(REPLY)])
    with pytest.raises(ModelUnavailable):
        run(_call(_client()))
    assert t.calls == MODELS


def test_gives_up_after_the_round_budget(patch_http):
    """Bounded: a provider that is genuinely down must not retry forever."""
    t = patch_http([_fault(503)] * 50)
    with pytest.raises(ModelUnavailable):
        run(_call(_client()))
    assert len(t.calls) == DEFAULT_POLICY.retry_rounds * len(MODELS)


def test_success_on_first_model_makes_exactly_one_call(patch_http):
    """A successful request must not trigger any retry."""
    t = patch_http([_reply(REPLY)])
    assert run(_call(_client())) == REPLY
    assert t.calls == ["model-a"]


@pytest.mark.parametrize("code", [401, 403])
def test_auth_failure_does_not_retry(patch_http, code):
    t = patch_http([_fault(code)])
    with pytest.raises(ModelUnavailable):
        run(_call(_client()))
    assert len(t.calls) == 1


def test_permanent_failure_only_tried_once(patch_http):
    t = patch_http([_fault(400), _fault(503), _reply(REPLY)])
    assert run(_call(_client())) == REPLY
    assert t.calls == ["model-a", "model-b", "model-b"]


def test_rate_limit_fails_over(patch_http):
    t = patch_http([_fault(429), _reply(REPLY)])
    assert run(_call(_client())) == REPLY
    assert len(t.calls) == 2


def test_incomplete_reply_not_accepted_or_retried(patch_http):
    response = _reply(REPLY).json()
    response["candidates"][0]["finishReason"] = "MAX_TOKENS"
    t = patch_http([httpx.Response(200, json=response, request=httpx.Request("POST", "https://x"))])
    with pytest.raises(BadModelReply):
        run(_call(_client()))
    assert len(t.calls) == 1


def test_thought_parts_excluded(patch_http):
    response = _reply(REPLY).json()
    response["candidates"][0]["content"]["parts"].insert(0, {"text": "thinking", "thought": True})
    patch_http([httpx.Response(200, json=response, request=httpx.Request("POST", "https://x"))])
    assert run(_call(_client())) == REPLY


def test_connection_reused_and_closed(patch_http):
    t = patch_http([])
    c = _client()
    run(_call(c))
    run(_call(c))
    assert len(t.clients) == 1
    run(c.close())
    assert t.clients[0].is_closed
    run(_call(c))
    assert len(t.clients) == 2
    run(c.close())


@pytest.mark.parametrize("model", ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash-lite"])
def test_stage_specific_effort(patch_http, model):
    t = patch_http([])
    c = ModelClient(models=[model], api_key="test")
    for kind in (TrajectoryKind.SCREEN, TrajectoryKind.TRANSCRIBE, TrajectoryKind.ANALYZE, TrajectoryKind.VERIFY):
        run(c.call(system_prompt="s", user_text="t", meta=CallMeta(kind=kind)))
    assert [b["generationConfig"]["thinkingConfig"]["thinkingLevel"] for b in t.bodies] == ["low", "low", "high", "medium"]


def test_model_compatible_thinking_config():
    assert thinking_config("gemini-3-pro-preview", "medium") == {"thinkingLevel": "high"}
    assert thinking_config("gemini-2.5-pro", "low") == {"thinkingBudget": 1024}
    assert thinking_config("gemini-flash-latest", "low") == {}


def test_stage_budget_cancels_request(monkeypatch):
    class HangingClient:
        is_closed = False
        cancelled = False
        async def post(self, *a, **kw):
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True
    c = _client()
    c._http = transport = HangingClient()
    monkeypatch.setattr(c, "policy_for", lambda _: CallPolicy(total_seconds=.02, attempt_seconds=1))
    with pytest.raises(ModelUnavailable):
        run(_call(c))
    assert transport.cancelled


def test_cooldown_skips_recent_failures_when_healthy_models_exist():
    assert model_order(MODELS, {"model-a": 99}, 100, cooldown=30) == ["model-b"]
    assert model_order(MODELS, {"model-a": 99, "model-b": 95}, 100, cooldown=30) == ["model-b", "model-a"]
    assert model_order(MODELS, {"model-a": 50}, 100, cooldown=30) == MODELS


def test_short_passes_allow_three_models_without_repeating_timeouts():
    for kind in ("screen", "transcribe", "verify"):
        policy = POLICIES[kind]
        assert policy.attempt_seconds * 3 <= policy.total_seconds
        assert policy.retry_rounds == 1
    assert POLICIES["analyze"].attempt_seconds == 60  # preserve space for full interpretation


def test_ai_stage_caps_remain_bounded_without_an_overall_job_deadline():
    foreground = sum(POLICIES[kind].total_seconds for kind in ("screen", "transcribe", "analyze"))
    assert POLICIES["analyze"].total_seconds == 120
    assert foreground == 181


@pytest.mark.parametrize("budget", [85, 120])
def test_two_full_timeouts_leave_third_model_a_fair_attempt(monkeypatch, budget):
    """Reproduce the failed upload without waiting minutes or calling the provider."""
    now = [0.0]
    calls, limits = [], []
    models = ["model-a", "model-b", "model-c"]

    class Transport:
        is_closed = False
        async def post(self, url, **kwargs):
            model = url.rsplit("/", 1)[-1].split(":")[0]
            calls.append(model)
            allowance = kwargs["timeout"].read
            limits.append(allowance)
            if model != "model-c":
                now[0] += allowance
                raise TimeoutError()
            now[0] += 1
            return _reply(REPLY)

    monkeypatch.setattr("agents.base.time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(ModelClient, "_log", staticmethod(lambda *a, **kw: _noop()))
    c = ModelClient(models=models, api_key="test")
    c._http = Transport()
    monkeypatch.setattr(c, "policy_for", lambda _: CallPolicy(total_seconds=budget, attempt_seconds=60))
    assert run(_call(c)) == REPLY
    assert calls == models
    assert limits == pytest.approx([budget / 3] * 3)
    assert now[0] < budget


def test_fast_failure_leaves_unused_budget_for_remaining_models(monkeypatch):
    now = [0.0]
    limits = []

    class Transport:
        is_closed = False
        async def post(self, url, **kwargs):
            limits.append(kwargs["timeout"].read)
            if len(limits) == 1:
                now[0] += 2
                return _fault(503)
            if len(limits) == 2:
                now[0] += limits[-1]
                raise TimeoutError()
            return _reply(REPLY)

    monkeypatch.setattr("agents.base.time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(ModelClient, "_log", staticmethod(lambda *a, **kw: _noop()))
    c = ModelClient(models=["a", "b", "c"], api_key="test")
    c._http = Transport()
    assert run(c.call(system_prompt="s", user_text="t", meta=CallMeta(kind=TrajectoryKind.ANALYZE))) == REPLY
    assert limits == pytest.approx([40, 59, 59])


@pytest.mark.parametrize("last_succeeds", [True, False])
def test_real_wall_clock_cancellation_reaches_all_fallbacks(monkeypatch, last_succeeds):
    calls, cancelled = [], []

    class Transport:
        is_closed = False
        async def post(self, url, **kwargs):
            calls.append(url.rsplit("/", 1)[-1].split(":")[0])
            if len(calls) == 3 and last_succeeds:
                return _reply(REPLY)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(calls[-1])

    c = ModelClient(models=["a", "b", "c"], api_key="test")
    c._http = Transport()
    monkeypatch.setattr(c, "policy_for", lambda _: CallPolicy(total_seconds=.3, attempt_seconds=.6))
    monkeypatch.setattr(ModelClient, "_log", staticmethod(lambda *a, **kw: _noop()))
    if last_succeeds:
        assert run(_call(c)) == REPLY
    else:
        with pytest.raises(ModelUnavailable):
            run(_call(c))
    assert calls == ["a", "b", "c"]
    assert cancelled == (["a", "b"] if last_succeeds else calls)


def test_single_healthy_model_keeps_full_per_attempt_allowance(monkeypatch):
    limits = []

    class Transport:
        is_closed = False
        async def post(self, url, **kwargs):
            limits.append(kwargs["timeout"].read)
            return _reply(REPLY)

    c = ModelClient(models=["a", "b", "c"], api_key="test")
    c._http = Transport()
    c._failed_at = {"a": 99, "b": 99}
    monkeypatch.setattr("agents.base.time", SimpleNamespace(monotonic=lambda: 100))
    monkeypatch.setattr(ModelClient, "_log", staticmethod(lambda *a, **kw: _noop()))
    assert run(c.call(system_prompt="s", user_text="t", meta=CallMeta(kind=TrajectoryKind.ANALYZE))) == REPLY
    assert limits == [60]
