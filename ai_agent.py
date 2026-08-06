"""The AI agent: sends medical documents to Gemini and returns parsed analysis.
Logic only - prompts live in ai_prompts.py, helpers in ai_tools.py.

Models come from env GEMINI_MODELS (comma-separated, priority order - see config.py).
The agent tries each model in order and fails over automatically: any HTTP error,
timeout, or unparseable response moves to the next model. A model that just failed
is skipped for MODEL_COOLDOWN_SECS so a dead primary doesn't add its timeout to
every request. Adding a model = appending it to the env var; no code change.
"""
import base64
import json
import logging
import time
from datetime import datetime, timezone

import httpx

import config
import database as db
from ai_prompts import RECONCILE_TIPS_PROMPT, REFRESH_ADVICE_PROMPT, VERIFY_SYSTEM_PROMPT, build_system_prompt
from ai_tools import extract_json

logger = logging.getLogger(__name__)

# ponytail: in-process cooldown map; move to Redis if we ever run multiple workers
MODEL_COOLDOWN_SECS = 120
_model_failed_at: dict[str, float] = {}


def _model_order(models: list[str], failed_at: dict[str, float], now: float,
                 cooldown: float = MODEL_COOLDOWN_SECS) -> list[str]:
    """Priority order with recently-failed models moved to the back (never dropped -
    if every model is cooling down we still try them all rather than fail instantly)."""
    healthy = [m for m in models if now - failed_at.get(m, -cooldown) >= cooldown]
    cooling = [m for m in models if m not in healthy]
    return healthy + cooling


async def _log_trajectory(meta: dict | None, request: dict, model: str | None,
                          response_raw: str | None, status: str, error: str | None,
                          latency_ms: int) -> None:
    """Append the agent call to ai_trajectories. Best-effort - never breaks the caller
    (e.g. self-check scripts run without a DB pool)."""
    meta = meta or {}
    try:
        traj_id = await db.fetchval(
            """insert into ai_trajectories (user_id, kind, model, report_id, request,
                 response_raw, status, error, latency_ms, created_at)
               values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) returning id""",
            meta.get("user_id"), meta.get("kind", "unknown"), model, meta.get("report_id"),
            request, response_raw, status, error, latency_ms,
            datetime.now(timezone.utc).isoformat(),
        )
        meta["trajectory_id"] = traj_id
        meta["model"] = model
    except Exception as e:
        logger.warning(f"trajectory log failed (continuing): {e}")


async def _gemini_document_call(files: list[tuple[str, str]], system_prompt: str,
                                user_text: str, meta: dict | None = None) -> dict:
    """Send a document (0-5 files; text-only when files is empty) + prompt to Gemini
    as ONE multimodal message and return the parsed JSON. Fails over across GEMINI_MODELS.
    Every call is recorded in ai_trajectories with its raw response (pass `meta` with
    kind/user_id/report_id to attribute it; meta gets trajectory_id + model back)."""
    parts = []
    files_meta = []
    for file_path, mime_type in files:
        with open(file_path, "rb") as f:
            raw = f.read()
        parts.append({"inline_data": {"mime_type": mime_type, "data": base64.b64encode(raw).decode()}})
        files_meta.append({"mime_type": mime_type, "bytes": len(raw)})
    parts.append({"text": user_text})

    body = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": parts}],
        # temperature 0: deterministic extraction - the same document should always
        # produce the same markers, statuses and score.
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0},
    }
    request_log = {"user_text": user_text, "files": files_meta, "attempts": []}
    started = time.monotonic()

    last_err: Exception | None = None
    for model in _model_order(config.GEMINI_MODELS, _model_failed_at, time.monotonic()):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(url, json=body, headers={"x-goog-api-key": config.GEMINI_API_KEY})
            resp.raise_for_status()
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            parsed = extract_json(text)
            _model_failed_at.pop(model, None)
            await _log_trajectory(meta, request_log, model, text, "ok", None,
                                  int((time.monotonic() - started) * 1000))
            return parsed
        except Exception as e:
            # Covers HTTP errors, timeouts, missing candidates and JSON parse failures -
            # all are reasons to try the next model. A genuinely bad document fails on
            # every model and surfaces the last error, same as before.
            _model_failed_at[model] = time.monotonic()
            last_err = e
            request_log["attempts"].append({"model": model, "error": f"{type(e).__name__}: {e}"[:300]})
            logger.warning(f"model {model} failed ({type(e).__name__}: {e}); trying next model")
    await _log_trajectory(meta, request_log, None, None, "error",
                          str(last_err)[:500] if last_err else "GEMINI_MODELS is empty",
                          int((time.monotonic() - started) * 1000))
    raise last_err if last_err else RuntimeError("GEMINI_MODELS is empty")


async def run_ai_analysis(files: list[tuple[str, str]], profile_context: str,
                          language: str = "english", meta: dict | None = None) -> dict:
    """files: list of (file_path, mime_type). Multiple files are pages of ONE document
    (images only - enforced upstream in the router)."""
    page_note = (
        f" The document is provided as {len(files)} photos - treat them as consecutive pages of "
        "ONE single report and merge all findings into one result."
        if len(files) > 1 else ""
    )
    text = f"Analyze this medical document and return JSON matching the correct report_type.{page_note} {profile_context}"
    return await _gemini_document_call(files, build_system_prompt(language), text, meta=meta)


async def verify_biomarkers(files: list[tuple[str, str]], biomarkers: list[dict],
                            meta: dict | None = None) -> list[dict]:
    """PRO self-check: second AI pass that cross-checks the extraction against the document.
    Returns a list of {name, issue, expected} mismatches (may be empty)."""
    compact = [
        {"name": b.get("name"), "value": b.get("value"), "unit": b.get("unit"),
         "ref_range": b.get("ref_range"), "status": b.get("status")}
        for b in biomarkers
    ]
    text = "Verify this extraction against the attached document:\n" + json.dumps(compact, ensure_ascii=False)
    parsed = await _gemini_document_call(files, VERIFY_SYSTEM_PROMPT, text, meta=meta)
    out = parsed.get("mismatches") or []
    return [m for m in out if isinstance(m, dict) and m.get("name")]


if __name__ == "__main__":
    # Self-check: run `python ai_agent.py`
    # Fresh state: priority order preserved.
    assert _model_order(["a", "b", "c"], {}, now=1000.0) == ["a", "b", "c"]
    # Primary failed recently: moved to the back, not dropped.
    assert _model_order(["a", "b", "c"], {"a": 999.0}, now=1000.0) == ["b", "c", "a"]
    # Cooldown expired: primary restored to the front.
    assert _model_order(["a", "b", "c"], {"a": 800.0}, now=1000.0) == ["a", "b", "c"]
    # Everything cooling down: all models still tried, in priority order.
    assert _model_order(["a", "b"], {"a": 999.0, "b": 999.0}, now=1000.0) == ["a", "b"]
    print("ai_agent self-check passed")


async def refresh_marker_advice(profile_context: str, marker: dict,
                                linked_suggestions: list[dict], language: str = "english",
                                meta: dict | None = None) -> dict:
    """After a user corrects a biomarker value, rewrite that marker's explanation/tip
    and any report suggestions linked to it so the advice matches the NEW value.
    Text-only Gemini call (no document needed). Raises on failure - caller decides
    whether stale advice is acceptable."""
    # Always explicit - without this the model drifts into Hinglish when the
    # profile context mentions Indian foods.
    lang_note = f" Write ALL narrative text strictly in this language: {language}."
    payload = {
        "marker": {"name": marker.get("name"), "value": marker.get("value"), "unit": marker.get("unit"),
                   "ref_range": marker.get("ref_range"), "status": marker.get("status")},
        "linked_suggestions": [{"title": s.get("title"), "body": s.get("body")} for s in linked_suggestions],
    }
    text = f"{profile_context}{lang_note}\nCorrected marker and its current suggestions:\n" + json.dumps(payload, ensure_ascii=False)
    return await _gemini_document_call([], REFRESH_ADVICE_PROMPT, text, meta=meta)


async def reconcile_tips(profile_context: str, tips: list[dict], meta: dict | None = None) -> list[int]:
    """Cross-report conflict check on the aggregated tips feed. Returns indices to
    drop (older side of each genuine contradiction). Raises on failure - caller
    fails open and serves the unfiltered list."""
    numbered = [
        {"i": i, "title": t.get("title"), "body": t.get("body"),
         "linked_marker": t.get("linked_marker"), "from": t.get("report_date")}
        for i, t in enumerate(tips)
    ]
    text = f"{profile_context}\nTips:\n" + json.dumps(numbered, ensure_ascii=False)
    parsed = await _gemini_document_call([], RECONCILE_TIPS_PROMPT, text, meta=meta)
    return [i for i in (parsed.get("drop") or []) if isinstance(i, int) and 0 <= i < len(tips)]
