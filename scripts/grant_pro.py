#!/usr/bin/env python
"""Grant or extend Pro access for any user by email or username.

Usage:
    ./venv/bin/python scripts/grant_pro.py <email_or_username> [days]

Examples:
    ./venv/bin/python scripts/grant_pro.py friend@example.com
    ./venv/bin/python scripts/grant_pro.py ketanraaz1 365
"""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")


async def grant_pro(identifier: str, days: int = 365) -> int:
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("SUPABASE_DB_URL not found in environment or .env", file=sys.stderr)
        return 1

    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        user = await conn.fetchrow(
            """
            select user_id, email, username, name, preferences
            from users
            where lower(email) = lower($1) or lower(username) = lower($1) or user_id = $1
            """,
            identifier.strip(),
        )

        if not user:
            print(f"Error: User '{identifier}' not found in database.", file=sys.stderr)
            return 1

        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        expires_at = (now + timedelta(days=days)).isoformat()

        prefs = json.loads(user["preferences"]) if isinstance(user["preferences"], str) else dict(user["preferences"] or {})
        prefs.update({
            "plan": "pro",
            "plan_changed_at": now_iso,
            "plan_expires_at": expires_at,
        })

        await conn.execute(
            "update users set preferences = $1 where user_id = $2",
            json.dumps(prefs),
            user["user_id"],
        )

        await conn.execute(
            """
            insert into plan_history (user_id, action, plan, amount_inr, starts_at, expires_at, created_at)
            values ($1, 'upgraded', 'pro', null, $2, $3, $2)
            """,
            user["user_id"],
            now_iso,
            expires_at,
        )

        print(f"✅ Successfully granted Pro to {user['name'] or user['username'] or user['email']}!")
        print(f"   User ID    : {user['user_id']}")
        print(f"   Email      : {user['email']}")
        print(f"   Username   : @{user['username'] or 'n/a'}")
        print(f"   Plan       : PRO")
        print(f"   Valid Until: {expires_at[:10]} ({days} days)")
        return 0

    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description="Grant Pro access to a Folin user.")
    parser.add_argument("identifier", help="User email, username, or user_id")
    parser.add_argument("days", nargs="?", type=int, default=365, help="Number of days of Pro access (default: 365)")
    args = parser.parse_args()

    return asyncio.run(grant_pro(args.identifier, args.days))


if __name__ == "__main__":
    raise SystemExit(main())
