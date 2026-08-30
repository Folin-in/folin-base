"""ModelClient failover and retry. Runs offline - the HTTP post is stubbed, no model, no DB.

A 503 "high demand" is transient by Google's own wording, so one sweep of the models is
not enough: the client must re-sweep before declaring the provider unavailable.

    ./venv/bin/python -m pytest tests/test_model_failover.py -v
"""
import asyncio
import json

import httpx
import pytest

from agents.base import ModelClient, ModelUnavailable
from app.constants import MODEL_RETRY_ROUNDS

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

    async def post(self, url, **kw):
        self.calls.append(url.rsplit("/", 1)[-1].split(":")[0])
        step = self._script.pop(0) if self._script else _reply(REPLY)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture
def patch_http(monkeypatch):
    def apply(script):
        t = _Transport(script)

        class _Client:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return t
            async def __aexit__(self, *a): return False

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
    assert len(t.calls) == MODEL_RETRY_ROUNDS * len(MODELS)


def test_success_on_first_model_makes_exactly_one_call(patch_http):
    """The retry must not cost extra quota on the happy path - each call is 1/20 of a free day."""
    t = patch_http([_reply(REPLY)])
    assert run(_call(_client())) == REPLY
    assert t.calls == ["model-a"]
