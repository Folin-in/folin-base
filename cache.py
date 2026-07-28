"""In-process TTL cache for the per-request user lookup (the hottest DB path).

Every users-table write MUST call invalidate(user_id) - that keeps token revocation
(SEC-003) and profile updates instant, while repeat reads within the TTL skip the
remote Postgres round-trip.

# ponytail: single-process dict cache; swap for Redis if the backend ever scales past one instance
"""
from time import monotonic

_TTL_SECONDS = 30.0
_store: dict[str, tuple[float, dict]] = {}


def get_user(user_id: str) -> dict | None:
    entry = _store.get(user_id)
    if not entry:
        return None
    ts, user = entry
    if monotonic() - ts > _TTL_SECONDS:
        _store.pop(user_id, None)
        return None
    return user


def set_user(user_id: str, user: dict) -> None:
    _store[user_id] = (monotonic(), user)
    # Bound memory: drop oldest entries past 10k users.
    if len(_store) > 10_000:
        for k in list(_store)[:1_000]:
            _store.pop(k, None)


def invalidate(user_id: str) -> None:
    _store.pop(user_id, None)


# ---------- Reports (append-only: written by analyze, mutated only via rename /
# biomarker-edit / delete - each of those calls invalidate_reports) ----------
_REPORTS_TTL = 300.0  # generous: every mutation path invalidates explicitly
_reports: dict[str, tuple[float, list[dict]]] = {}


async def get_reports(user_id: str) -> list[dict]:
    """All report rows for a user (every member), read-through cached."""
    entry = _reports.get(user_id)
    if entry and monotonic() - entry[0] <= _REPORTS_TTL:
        return entry[1]
    import database as db
    rows = await db.fetch("select * from reports where user_id = $1 limit 1000", user_id)
    _reports[user_id] = (monotonic(), rows)
    if len(_reports) > 2_000:
        for k in list(_reports)[:200]:
            _reports.pop(k, None)
    return rows


def invalidate_reports(user_id: str) -> None:
    _reports.pop(user_id, None)
