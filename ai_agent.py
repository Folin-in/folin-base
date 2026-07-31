"""The AI agent: sends medical documents to Gemini and returns parsed analysis.
Logic only - prompts live in ai_prompts.py, helpers in ai_tools.py.

Model is configurable via env GEMINI_MODEL (see config.py).
"""
import base64
import json
import logging

import httpx

import config
from ai_prompts import VERIFY_SYSTEM_PROMPT, build_system_prompt
from ai_tools import extract_json

logger = logging.getLogger(__name__)


async def _gemini_document_call(files: list[tuple[str, str]], system_prompt: str, user_text: str) -> dict:
    """Send a document (1 file of any supported type, or 2-5 image pages) + prompt to Gemini
    as ONE multimodal message and return the parsed JSON."""
    parts = []
    for file_path, mime_type in files:
        with open(file_path, "rb") as f:
            file_b64 = base64.b64encode(f.read()).decode()
        parts.append({"inline_data": {"mime_type": mime_type, "data": file_b64}})
    parts.append({"text": user_text})

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent"
    body = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": parts}],
        # temperature 0: deterministic extraction - the same document should always
        # produce the same markers, statuses and score.
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0},
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, json=body, headers={"x-goog-api-key": config.GEMINI_API_KEY})
    resp.raise_for_status()
    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    return extract_json(text)


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
