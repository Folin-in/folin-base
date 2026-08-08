"""Pytest bootstrap: hydrate env vars from the split repos' .env files so the
HTTP suites know where the backend lives regardless of shell context."""
import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND = Path(__file__).resolve().parent.parent          # ~/CODE/folin-base
FRONTEND = BACKEND.parent / "folin-handy"                 # sibling checkout

load_dotenv(BACKEND / ".env", override=False)             # SUPABASE_DB_URL, JWT_SECRET, ...
load_dotenv(FRONTEND / ".env", override=False)            # EXPO_PUBLIC_BACKEND_URL

# The suites index this key directly, so give it a working default rather than
# failing collection when the frontend checkout isn't present.
os.environ.setdefault("EXPO_PUBLIC_BACKEND_URL", "http://localhost:8000")
