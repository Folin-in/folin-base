"""asyncpg pool + the four query helpers everything uses. Schema is applied at connect
time from db/schema/*.sql in filename order - one file per table, 900_ for constraints -
each file idempotent, so a restart is always safe."""
import json
import logging
from pathlib import Path

import asyncpg

from app.config import settings

logger = logging.getLogger(__name__)

SCHEMA_DIR = Path(__file__).resolve().parent / "schema"
POOL_MIN, POOL_MAX = 1, 10
# Required behind Supabase's transaction pooler (pgbouncer): prepared statements do not
# survive across pooled connections.
STATEMENT_CACHE_SIZE = 0

pool: asyncpg.Pool | None = None


class NotConnected(RuntimeError):
    pass


def _require_pool() -> asyncpg.Pool:
    if pool is None:
        raise NotConnected("db.session.connect() has not been called")
    return pool


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


def schema_files() -> list[Path]:
    files = sorted(SCHEMA_DIR.glob("*.sql"))
    if not files:
        raise RuntimeError(f"no schema files in {SCHEMA_DIR}")
    return files


async def apply_schema(conn: asyncpg.Connection) -> int:
    for path in schema_files():
        await conn.execute(path.read_text())
    return len(schema_files())


async def connect() -> None:
    global pool
    pool = await asyncpg.create_pool(
        settings.supabase_db_url, init=_init_conn,
        statement_cache_size=STATEMENT_CACHE_SIZE, min_size=POOL_MIN, max_size=POOL_MAX,
    )
    async with pool.acquire() as conn:
        n = await apply_schema(conn)
    logger.info("Postgres connected, %d schema files applied", n)


async def close() -> None:
    global pool
    if pool:
        await pool.close()
        pool = None


async def fetch(sql: str, *args) -> list[dict]:
    return [dict(r) for r in await _require_pool().fetch(sql, *args)]


async def fetchrow(sql: str, *args) -> dict | None:
    r = await _require_pool().fetchrow(sql, *args)
    return dict(r) if r else None


async def fetchval(sql: str, *args):
    return await _require_pool().fetchval(sql, *args)


async def execute(sql: str, *args) -> str:
    return await _require_pool().execute(sql, *args)
