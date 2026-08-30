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

import pytest
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


# These files exercise a separately running server and, in several cases, intentionally
# inspect seeded account history. They are valuable deployment smoke tests, but they are
# not safe defaults for a local unit/integration run: they create accounts, consume AI
# quota, and depend on external state. Run them explicitly with `pytest --run-live`.
LIVE_HTTP_FILES = frozenset({
    "backend_test.py",
    "test_it18_spotcheck.py",
    "test_new_features.py",
    "test_new_features_it17.py",
    "test_profile_fields.py",
    "test_profile_hp_preserve.py",
    "test_security_fixes.py",
    "test_security_it19.py",
    "test_universal_docs_it16.py",
    "test_username_edit_it13.py",
})


def pytest_addoption(parser):
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="run tests that call EXPO_PUBLIC_BACKEND_URL and depend on live seeded data",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "live: calls a separately running Folin backend")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live"):
        return
    skip = pytest.mark.skip(reason="live backend test; pass --run-live to enable")
    for item in items:
        if Path(str(item.path)).name in LIVE_HTTP_FILES:
            item.add_marker(skip)
