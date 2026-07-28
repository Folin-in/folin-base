"""Pytest bootstrap: hydrate env vars from the project .env files so tests can talk to
the live backend and MongoDB regardless of shell context."""
import os
from pathlib import Path

from dotenv import load_dotenv

# Backend env first (MONGO_URL, DB_NAME, JWT_SECRET, ...)
load_dotenv(Path("/app/backend/.env"), override=False)
# Frontend env (EXPO_PUBLIC_BACKEND_URL)
load_dotenv(Path("/app/frontend/.env"), override=False)

# Fallback for the public API base if the frontend .env didn't provide it.
if not os.environ.get("EXPO_PUBLIC_BACKEND_URL"):
    # Read the .env manually as a last resort (avoids failing collection).
    fenv = Path("/app/frontend/.env")
    if fenv.exists():
        for line in fenv.read_text().splitlines():
            if line.startswith("EXPO_PUBLIC_BACKEND_URL="):
                os.environ["EXPO_PUBLIC_BACKEND_URL"] = line.split("=", 1)[1].strip()
                break
