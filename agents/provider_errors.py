"""Bounded provider diagnostics, never a dump of request/response bodies."""
import json
import re
from collections.abc import Iterable

import httpx

MAX_ERROR_BODY = 64 * 1024
MAX_MESSAGE = 1000


def provider_error_details(response: httpx.Response, sensitive: Iterable[str] = ()) -> dict:
    """Keep Google's error code/status/message; discard arbitrary details and HTML.

    Provider messages are untrusted and can echo input. Redact known request values,
    quoted strings, credentials, URLs, emails and long encoded data before logging.
    Malformed or oversized responses must never interfere with model failover.
    """
    if len(response.content) > MAX_ERROR_BODY:
        return {"message": "[provider error body omitted: too large]"}
    try:
        body = response.json()
    except (ValueError, UnicodeError):
        return {"message": "[provider error body unavailable: non-JSON]"}
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return {"message": "[provider error object unavailable]"}
    out = {}
    code, status, message = error.get("code"), error.get("status"), error.get("message")
    if type(code) is int:
        out["code"] = code
    if isinstance(status, str) and re.fullmatch(r"[A-Z_]{1,64}", status):
        out["status"] = status
    if isinstance(message, str):
        # Redact before truncation so an echoed credential isn't partially exposed.
        for value in sensitive:
            if not value:
                continue
            for fragment in (value, *value.splitlines()):
                if fragment:
                    message = message.replace(fragment, "[redacted]")
                    message = message.replace(json.dumps(fragment)[1:-1], "[redacted]")
        message = re.sub(r'''"[^"\n]*"|'[^'\n]*'|`[^`\n]*`''', '[redacted]', message)
        message = re.sub(r"https?://\S+", "[url redacted]", message)
        message = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email redacted]", message)
        message = re.sub(r"(?i)(bearer\s+|(?:api[_-]?key|x-goog-api-key)\s*[:=]\s*)\S+", r"\1[redacted]", message)
        message = re.sub(r"[A-Za-z0-9_+/=-]{64,}", "[encoded data redacted]", message)
        message = " ".join(message.split())
        out["message"] = message[:MAX_MESSAGE] + ("…" if len(message) > MAX_MESSAGE else "")
    return out or {"message": "[provider error fields unavailable]"}
