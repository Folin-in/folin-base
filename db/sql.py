"""Helpers for the few places SQL is assembled dynamically. Values ALWAYS travel as $n
parameters; the only thing ever interpolated is a column name, and it must be one we
allow-listed here. A key that is not in the allow-list raises - so a future change that
starts building `updates` from request data cannot turn into injection."""

USERS_UPDATABLE = frozenset({
    "name", "username", "email", "picture", "health_profile", "preferences", "password_hash",
    "google_id", "apple_id", "supabase_uid", "consented_at", "consent_version",
})
MEMBERS_UPDATABLE = frozenset({"name", "relation", "age", "gender", "allergies"})
SOCIAL_ID_COLUMNS = frozenset({"google_id", "apple_id"})


class UnsafeColumn(ValueError):
    pass


def assert_columns(cols, allowed: frozenset[str], table: str) -> None:
    bad = [c for c in cols if c not in allowed]
    if bad:
        raise UnsafeColumn(f"refusing to interpolate column(s) {bad} into {table} SQL - not in the allow-list")


def set_clause(updates: dict, allowed: frozenset[str], table: str, first_param: int = 2) -> str:
    """'a = $2, b = $3' for the keys of `updates`, after checking every key is allow-listed.
    Values are the caller's responsibility to pass positionally, in dict order."""
    assert_columns(updates.keys(), allowed, table)
    return ", ".join(f"{col} = ${i + first_param}" for i, col in enumerate(updates))
