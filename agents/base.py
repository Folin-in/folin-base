"""The one place a model is called. Every agent goes through `ModelClient`, which gives it:

- ordered failover across settings.gemini_models with a per-model cooldown after a
  provider fault (rate limit, 5xx, timeout). Unusable replies are not retried within
  this latency budget; they can reflect model output failures, not just bad files;
- a row in ai_trajectories for every call, success or failure, with the raw reply.
  That table is the audit trail when a report looks wrong;
- low-variance generation (temperature 0); provider output is not guaranteed deterministic.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from agents.tools import TOOLS
from agents.attachments import encode_attachment
from agents.performance import DEFAULT_POLICY, POLICIES, CallPolicy, thinking_config
from app.config import settings
from app.constants import MODEL_COOLDOWN_SECS
from app.enums import TrajectoryKind
from db import session as db

logger = logging.getLogger(__name__)

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_json_extract = TOOLS["json_extract"]


class ModelUnavailable(RuntimeError):
    """Every configured model refused or failed - a provider problem, not a document one."""


class BadModelReply(ValueError):
    """The model answered but not with a usable, completed JSON response."""


@dataclass
class CallMeta:
    """Attribution for the trajectory row. Filled in by the client after the call."""
    kind: TrajectoryKind
    user_id: str | None = None
    report_id: str | None = None
    trajectory_id: int | None = field(default=None, init=False)
    model: str | None = field(default=None, init=False)


def model_order(models: list[str], failed_at: dict[str, float], now: float,
                cooldown: float = MODEL_COOLDOWN_SECS) -> list[str]:
    """Priority order with recently-failed models moved to the back - never dropped, so if
    everything is cooling down we still try them all rather than fail instantly."""
    healthy = [m for m in models if now - failed_at.get(m, -cooldown) >= cooldown]
    return healthy + [m for m in models if m not in healthy]


class ModelClient:
    def __init__(self, models: list[str] | None = None, api_key: str | None = None):
        self._models = models or settings.gemini_models
        self._api_key = api_key or settings.gemini_api_key
        self._failed_at: dict[str, float] = {}
        self._http: httpx.AsyncClient | None = None

    def policy_for(self, meta: CallMeta | None) -> CallPolicy:
        return POLICIES.get(str(meta.kind), DEFAULT_POLICY) if meta else DEFAULT_POLICY

    def _connection(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10,
                                   keepalive_expiry=120))
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def call(self, *, system_prompt: str, user_text: str,
                   files: list[tuple[str, str]] = (), meta: CallMeta | None = None) -> dict:
        """One multimodal message (0-5 files + text) -> parsed JSON. Fails over across models."""
        parts, files_meta = [], []
        for path, mime in files:
            part, info = encode_attachment(path, mime)
            parts.append(part)
            files_meta.append(info)
        parts.append({"text": user_text})
        body = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"response_mime_type": "application/json", "temperature": 0},
        }
        policy = self.policy_for(meta)
        request_log = {"user_text": user_text, "files": files_meta, "attempts": [],
                       "thinking_level": policy.thinking, "stage_budget_seconds": policy.total_seconds}
        started = time.monotonic()
        deadline = started + policy.total_seconds
        last_err: Exception | None = None
        permanent_failures: set[str] = set()

        # A 503 "high demand" is transient by Google's own wording, so sweep the models
        # more than once before declaring the provider unavailable.
        for round_no in range(policy.retry_rounds):
            if all(model in permanent_failures for model in self._models):
                break
            if round_no:
                delay = policy.backoff_seconds * round_no
                if deadline - time.monotonic() <= delay:
                    break
                await asyncio.sleep(delay)
            for model in model_order(self._models, self._failed_at, time.monotonic()):
                if model in permanent_failures:
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                generation = dict(body["generationConfig"])
                thinking = thinking_config(model, policy.thinking)
                if thinking:
                    generation["thinkingConfig"] = thinking
                attempt_started = time.monotonic()
                try:
                    # Wall-clock timeout also bounds streaming/trickling responses;
                    # httpx's read timeout alone only limits gaps between chunks.
                    async with asyncio.timeout(min(policy.attempt_seconds, remaining)):
                        resp = await self._connection().post(
                            GEMINI_ENDPOINT.format(model=model),
                            json={**body, "generationConfig": generation},
                            headers={"x-goog-api-key": self._api_key},
                            timeout=httpx.Timeout(min(policy.attempt_seconds, remaining), connect=10, pool=5))
                    resp.raise_for_status()
                except (httpx.HTTPError, TimeoutError) as e:
                    # Provider fault: cool this model down and try the next one.
                    self._failed_at[model] = time.monotonic()
                    last_err = e
                    code = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
                    request_log["attempts"].append({"model": model, "error": type(e).__name__,
                                                     "http_status": code,
                                                     "latency_ms": int((time.monotonic() - attempt_started) * 1000)})
                    logger.warning("model %s failed (%s: %s); trying next", model, type(e).__name__, e)
                    if code in (401, 403):
                        await self._log(meta, request_log, None, None, "error", "Provider authorization failed", started)
                        raise ModelUnavailable("Provider authorization failed") from e
                    if code is not None and code not in (408, 429) and code < 500:
                        permanent_failures.add(model)
                    continue
                self._failed_at.pop(model, None)
                text = ""
                try:
                    response = resp.json()
                    candidate = response["candidates"][0]
                    text = "".join(p.get("text", "") for p in candidate.get("content", {}).get("parts", [])
                                   if not p.get("thought"))
                    request_log["usage"] = response.get("usageMetadata", {})
                    request_log["effective_thinking_config"] = thinking
                    if candidate.get("finishReason") not in (None, "STOP"):
                        raise ValueError("Model response did not complete")
                    parsed = _json_extract(text)
                except (ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
                    # Do not accept truncated output or spend another full call budget
                    # on a malformed answer. The caller decides whether this stage is optional.
                    await self._log(meta, request_log, model, text, "bad_reply", str(e), started)
                    raise BadModelReply(str(e)) from e
                await self._log(meta, request_log, model, text, "ok", None, started)
                return parsed

        reason = (str(last_err) or type(last_err).__name__) if last_err else "Model request budget exhausted"
        await self._log(meta, request_log, None, None, "error", reason[:500], started)
        raise ModelUnavailable(reason)

    @staticmethod
    async def _log(meta: CallMeta | None, request: dict, model: str | None, response_raw: str | None,
                   status: str, error: str | None, started: float) -> None:
        """Best-effort trajectory row. Never breaks the caller."""
        if meta is None:
            return
        try:
            meta.trajectory_id = await db.fetchval(
                """insert into ai_trajectories (user_id, kind, model, report_id, request,
                     response_raw, status, error, latency_ms, created_at)
                   values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) returning id""",
                meta.user_id, str(meta.kind), model, meta.report_id, request, response_raw,
                status, error, int((time.monotonic() - started) * 1000),
                datetime.now(timezone.utc).isoformat(),
            )
            meta.model = model
        except Exception as e:
            logger.warning("trajectory log failed (continuing): %s", e)


client = ModelClient()
