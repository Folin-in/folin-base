#!/usr/bin/env python
"""Print an access token for an admin account, for signing in to /api/admin.

Works for Google accounts too, which have no password to log in with. The email must
already be listed in ADMIN_EMAILS or the dashboard will still refuse it.

    ./venv/bin/python scripts/admin_token.py 21ketanraaz@gmail.com
"""
import asyncio
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from app.config import settings                                    # noqa: E402  (needs .env first)
from core.security import create_access_token                      # noqa: E402


async def main(email: str) -> int:
    if email.lower() not in settings.admin_emails:
        print(f"{email} is not in ADMIN_EMAILS - add it to .env and restart the server.", file=sys.stderr)
        return 1
    conn = await asyncpg.connect(os.environ["SUPABASE_DB_URL"], statement_cache_size=0)
    try:
        row = await conn.fetchrow(
            "select user_id, token_version from users where lower(email) = lower($1)", email)
    finally:
        await conn.close()
    if not row:
        print(f"No account for {email}. Sign in through the app once first.", file=sys.stderr)
        return 1
    print(create_access_token(row["user_id"], row["token_version"] or 0))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(sys.argv[1])))
