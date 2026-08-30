"""The one place a model is called. Every agent goes through `ModelClient`, which gives it:

- ordered failover across settings.gemini_models with a per-model cooldown after a
  provider fault (rate limit, 5xx, timeout) - never after a bad reply, which is the
  document's fault and would fail on every model anyway;
- a row in ai_trajectories for every call, success or failure, with the raw reply.
  That table is the audit trail when a report looks wrong;
- deterministic generation (temperature 0) so the same document yields the same result.
"""
import asyncio
import base64
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from agents.tools import TOOLS
from app.config import settings
from app.constants import (GEMINI_CALL_TIMEOUT_SECS, GEMINI_THINKING_LEVEL, MODEL_COOLDOWN_SECS,
                           MODEL_RETRY_BACKOFF_SECS, MODEL_RETRY_ROUNDS)
from app.enums import TrajectoryKind
from db import session as db

logger = logging.getLogger(__name__)

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_json_extract = TOOLS["json_extract"]


class ModelUnavailable(RuntimeError):
    """Every configured model refused or failed - a provider problem, not a document one."""


class BadModelReply(ValueError):
    """The model answered but not with usable JSON - almost always the document's fault."""


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

    async def call(self, *, system_prompt: str, user_text: str,
                   files: list[tuple[str, str]] = (), meta: CallMeta | None = None) -> dict:
        """One multimodal message (0-5 files + text) -> parsed JSON. Fails over across models."""
        parts, files_meta = [], []
        for path, mime in files:
            with open(path, "rb") as f:
                raw = f.read()
            parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(raw).decode()}})
            files_meta.append({"mime_type": mime, "bytes": len(raw)})
        parts.append({"text": user_text})
        body = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"response_mime_type": "application/json", "temperature": 0,
                                 "thinkingConfig": {"thinkingLevel": GEMINI_THINKING_LEVEL}},
        }
        request_log = {"user_text": user_text, "files": files_meta, "attempts": []}
        started = time.monotonic()
        last_err: Exception | None = None

        # A 503 "high demand" is transient by Google's own wording, so sweep the models
        # more than once before declaring the provider unavailable.
        for round_no in range(MODEL_RETRY_ROUNDS):
            if round_no:
                await asyncio.sleep(MODEL_RETRY_BACKOFF_SECS * round_no)
            for model in model_order(self._models, self._failed_at, time.monotonic()):
                try:
                    async with httpx.AsyncClient(timeout=GEMINI_CALL_TIMEOUT_SECS) as client:
                        resp = await client.post(GEMINI_ENDPOINT.format(model=model), json=body,
                                                 headers={"x-goog-api-key": self._api_key})
                    resp.raise_for_status()
                    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                except Exception as e:
                    # Provider fault: cool this model down and try the next one.
                    self._failed_at[model] = time.monotonic()
                    last_err = e
                    request_log["attempts"].append({"model": model, "error": f"{type(e).__name__}: {e}"[:300]})
                    logger.warning("model %s failed (%s: %s); trying next", model, type(e).__name__, e)
                    continue
                self._failed_at.pop(model, None)
                try:
                    parsed = _json_extract(text)
                except ValueError as e:
                    # The model answered; the reply was unusable. Not a reason to blame the
                    # model or to retry elsewhere - the document is what it is.
                    await self._log(meta, request_log, model, text, "bad_reply", str(e), started)
                    raise BadModelReply(str(e)) from e
                await self._log(meta, request_log, model, text, "ok", None, started)
                return parsed

        await self._log(meta, request_log, None, None, "error",
                        str(last_err)[:500] if last_err else "no models configured", started)
        raise ModelUnavailable(str(last_err) if last_err else "no models configured")

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
