"""ModelClient failover and retry. Runs offline - the HTTP post is stubbed, no model, no DB.

A 503 "high demand" is transient by Google's own wording, so one sweep of the models is
not enough: the client must re-sweep before declaring the provider unavailable.

    ./venv/bin/python -m pytest tests/test_model_failover.py -v
"""
import asyncio
import json

import httpx
import pytest

from agents.base import BadModelReply, CallMeta, ModelClient, ModelUnavailable
from agents.performance import DEFAULT_POLICY, CallPolicy, thinking_config
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


# ------------------------------------------------------------------ the retry rounds

def test_retries_after_every_model_503s(patch_http):
    """Round 1 exhausts both models; the client must sweep again rather than give up."""
    t = patch_http([_fault(503), _fault(503), _reply(REPLY)])
    assert run(_call(_client())) == REPLY
    assert t.calls == ["model-a", "model-b", "model-a"]


def test_read_timeout_is_retried_too(patch_http):
    t = patch_http([httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow"), _reply(REPLY)])
    assert run(_call(_client())) == REPLY
    assert len(t.calls) == 3


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


def test_stage_specific_effort(patch_http):
    t = patch_http([])
    c = ModelClient(models=["gemini-3-flash-preview"], api_key="test")
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
