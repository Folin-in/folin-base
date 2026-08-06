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

import httpx

import config
from ai_prompts import REFRESH_ADVICE_PROMPT, VERIFY_SYSTEM_PROMPT, build_system_prompt
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


async def _gemini_document_call(files: list[tuple[str, str]], system_prompt: str, user_text: str) -> dict:
    """Send a document (0-5 files; text-only when files is empty) + prompt to Gemini
    as ONE multimodal message and return the parsed JSON. Fails over across GEMINI_MODELS."""
    parts = []
    for file_path, mime_type in files:
        with open(file_path, "rb") as f:
            file_b64 = base64.b64encode(f.read()).decode()
        parts.append({"inline_data": {"mime_type": mime_type, "data": file_b64}})
    parts.append({"text": user_text})

    body = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": parts}],
        # temperature 0: deterministic extraction - the same document should always
        # produce the same markers, statuses and score.
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0},
    }

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
            return parsed
        except Exception as e:
            # Covers HTTP errors, timeouts, missing candidates and JSON parse failures -
            # all are reasons to try the next model. A genuinely bad document fails on
            # every model and surfaces the last error, same as before.
            _model_failed_at[model] = time.monotonic()
            last_err = e
            logger.warning(f"model {model} failed ({type(e).__name__}: {e}); trying next model")
    raise last_err if last_err else RuntimeError("GEMINI_MODELS is empty")


async def run_ai_analysis(files: list[tuple[str, str]], profile_context: str, language: str = "english") -> dict:
    """files: list of (file_path, mime_type). Multiple files are pages of ONE document
    (images only - enforced upstream in the router)."""
    page_note = (
        f" The document is provided as {len(files)} photos - treat them as consecutive pages of "
        "ONE single report and merge all findings into one result."
        if len(files) > 1 else ""
    )
    text = f"Analyze this medical document and return JSON matching the correct report_type.{page_note} {profile_context}"
    return await _gemini_document_call(files, build_system_prompt(language), text)


async def verify_biomarkers(files: list[tuple[str, str]], biomarkers: list[dict]) -> list[dict]:
    """PRO self-check: second AI pass that cross-checks the extraction against the document.
    Returns a list of {name, issue, expected} mismatches (may be empty)."""
    compact = [
        {"name": b.get("name"), "value": b.get("value"), "unit": b.get("unit"),
         "ref_range": b.get("ref_range"), "status": b.get("status")}
        for b in biomarkers
    ]
    text = "Verify this extraction against the attached document:\n" + json.dumps(compact, ensure_ascii=False)
    parsed = await _gemini_document_call(files, VERIFY_SYSTEM_PROMPT, text)
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
                                linked_suggestions: list[dict], language: str = "english") -> dict:
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
    return await _gemini_document_call([], REFRESH_ADVICE_PROMPT, text)
