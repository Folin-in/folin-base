"""Compatibility entrypoint. The app lives in app.main; prefer `uvicorn app.main:app`."""
from app.main import app  # noqa: F401
