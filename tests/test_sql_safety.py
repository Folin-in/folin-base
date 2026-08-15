"""The dynamic-SQL allow-lists. Values always travel as parameters; only allow-listed
column names may ever be interpolated. These tests are the proof that a future change
building `updates` from request keys cannot become injection."""
import pytest

from db.sql import MEMBERS_UPDATABLE, USERS_UPDATABLE, UnsafeColumn, assert_columns, set_clause


def test_set_clause_numbers_params_from_two_by_default():
    assert set_clause({"name": "x", "picture": "y"}, USERS_UPDATABLE, "users") == "name = $2, picture = $3"


def test_unknown_column_is_refused():
    with pytest.raises(UnsafeColumn):
        set_clause({"name": "x", "is_admin": True}, USERS_UPDATABLE, "users")


@pytest.mark.parametrize("attack", [
    "name = 'x'; drop table users; --",
    "name) values ('x'); --",
    "email = $2, token_version = 0, name",
    "password_hash = 'known' where 1=1 --",
    "1",
    "",
])
def test_injection_shaped_keys_are_refused(attack):
    """A request-controlled key would arrive here as a dict key. None of these may pass."""
    with pytest.raises(UnsafeColumn):
        set_clause({attack: "v"}, USERS_UPDATABLE, "users")


def test_assert_columns_lists_every_offender():
    with pytest.raises(UnsafeColumn) as e:
        assert_columns(["name", "evil1", "evil2"], MEMBERS_UPDATABLE, "members")
    assert "evil1" in str(e.value) and "evil2" in str(e.value)


def test_allow_lists_do_not_include_privileged_columns():
    """These must never be settable through a generic update path."""
    for col in ("user_id", "token_version", "auth_provider", "created_at"):
        assert col not in USERS_UPDATABLE, f"{col} must not be in the generic update allow-list"
