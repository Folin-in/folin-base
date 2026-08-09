"""Pytest bootstrap: hydrate env vars from the split repos' .env files so the suites
know where the backend lives regardless of shell context.

Also owns THE event loop. asyncpg binds its pool to the loop that created it, and
`database.pool` is a module global - so when two in-process suites each made their own
loop, whichever ran second drove a pool belonging to the first one's loop and failed
depending on collection order. One shared loop removes that class of flake.
"""
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND = Path(__file__).resolve().parent.parent          # ~/CODE/folin-base
FRONTEND = BACKEND.parent / "folin-handy"                 # sibling checkout

load_dotenv(BACKEND / ".env", override=False)             # SUPABASE_DB_URL, JWT_SECRET, ...
load_dotenv(FRONTEND / ".env", override=False)            # EXPO_PUBLIC_BACKEND_URL

# The HTTP suites index this key directly, so give it a working default rather than
# failing collection when the frontend checkout isn't present.
os.environ.setdefault("EXPO_PUBLIC_BACKEND_URL", "http://localhost:8000")

# Created once, before any test module imports asyncio - see the module docstring.
LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)
