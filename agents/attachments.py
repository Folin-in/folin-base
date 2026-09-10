"""Reuse immutable encodings within one upload; never cache patient files globally."""
import base64
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_encoded: ContextVar[dict | None] = ContextVar("analysis_attachments", default=None)


@contextmanager
def attachment_scope():
    token = _encoded.set({})
    try:
        yield
    finally:
        _encoded.reset(token)


def encode_attachment(path: str, mime: str) -> tuple[dict, dict]:
    cache = _encoded.get()
    key = (path, mime)
    if cache is not None and key in cache:
        return cache[key]
    raw = Path(path).read_bytes()
    value = ({"inline_data": {"mime_type": mime, "data": base64.b64encode(raw).decode()}},
             {"mime_type": mime, "bytes": len(raw)})
    if cache is not None:
        cache[key] = value
    return value
