"""Supabase Auth: token verification, account linking and provisioning.

Uses a locally generated EC key in place of Supabase's JWKS, so every branch runs
offline against the real DB.

    ./venv/bin/python -m pytest tests/test_supabase_auth.py -v
"""
import asyncio
import time
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

import httpx  # noqa: E402
import jwt as pyjwt  # noqa: E402

import config  # noqa: E402
from db import session as db  # noqa: E402
import rate_limit  # noqa: E402
import security  # noqa: E402
from server import app  # noqa: E402

ISS = f"{config.SUPABASE_URL}/auth/v1"

_key = ec.generate_private_key(ec.SECP256R1())
_PRIV = _key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption())
_other = ec.generate_private_key(ec.SECP256R1())
_OTHER_PRIV = _other.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption())


class _FakeJWKS:
    def __init__(self, pub):
        self.key = pub

    def get_signing_key_from_jwt(self, token):
        return self


security._SUPABASE_JWKS = _FakeJWKS(_key.public_key())

# One loop for the whole run, owned by conftest - see its docstring.
_LOOP = asyncio.get_event_loop()
_ready = False


def token(sub, email, *, verified=True, aud="authenticated", iss=ISS, exp_delta=3600,
          sign_with=_PRIV, meta=None):
    now = int(time.time())
    claims = {"iss": iss, "aud": aud, "sub": sub, "email": email, "iat": now,
              "exp": now + exp_delta, "user_metadata": {"email_verified": verified, **(meta or {})}}
    return pyjwt.encode(claims, sign_with, algorithm="ES256")


def run(coro):
    async def _wrapped():
        global _ready
        if not _ready:
            await db.connect()
            await db.execute("delete from users where email like 'sb\\_%@example.com'")
            _ready = True
        rate_limit._WINDOWS.clear()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            return await coro(c)
    return _LOOP.run_until_complete(_wrapped())


def sub(name: str) -> str:
    """Stable UUID per test label - Supabase subs are UUIDs, not free strings."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"folin-test-{name}"))


def uniq(p="sb"):
    return f"{p}_{uuid.uuid4().hex[:10]}"


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


async def cleanup(*emails):
    for e in emails:
        await db.execute("delete from users where email = $1", e.lower())


# ------------------------------------------------------------------ verification

def test_rejects_bad_signature_wrong_aud_wrong_issuer_and_expired():
    async def t(c):
        email = f"{uniq()}@example.com"
        cases = {
            "wrong key": token(sub("s1"), email, sign_with=_OTHER_PRIV),
            "wrong aud": token(sub("s1"), email, aud="anon"),
            "wrong issuer": token(sub("s1"), email, iss="https://evil.example.com/auth/v1"),
            "expired": token(sub("s1"), email, exp_delta=-60),
            "malformed": "not.a.jwt",
        }
        for label, tok in cases.items():
            r = await c.post("/api/auth/supabase/sync", headers=H(tok), json={"consent": True})
            assert r.status_code == 401, f"{label} -> {r.status_code}"

    run(t)


def test_no_token_is_401():
    async def t(c):
        assert (await c.post("/api/auth/supabase/sync", json={"consent": True})).status_code == 401

    run(t)


# ------------------------------------------------------------------ provisioning

def test_new_identity_requires_consent_then_creates_account():
    email = f"{uniq()}@example.com"

    async def t(c):
        tok = token(sub("sb-new-1"), email, meta={"full_name": "Supa User"})
        r = await c.post("/api/auth/supabase/sync", headers=H(tok), json={"consent": False})
        assert r.status_code == 400 and "consent" in r.json()["detail"].lower()
        assert await db.fetchval("select count(*) from users where email = $1", email) == 0

        r = await c.post("/api/auth/supabase/sync", headers=H(tok), json={"consent": True})
        assert r.status_code == 200, r.text
        row = await db.fetchrow("select * from users where email = $1", email)
        assert str(row["supabase_uid"]) and row["auth_provider"] == "supabase"
        assert row["password_hash"] is None, "Supabase account must not hold a password"
        assert row["name"] == "Supa User" and row["consented_at"]
        await cleanup(email)

    run(t)


def test_sync_is_idempotent():
    email = f"{uniq()}@example.com"

    async def t(c):
        tok = token(sub("sb-idem"), email)
        a = await c.post("/api/auth/supabase/sync", headers=H(tok), json={"consent": True})
        b = await c.post("/api/auth/supabase/sync", headers=H(tok), json={"consent": True})
        assert a.json()["user"]["user_id"] == b.json()["user"]["user_id"]
        assert await db.fetchval("select count(*) from users where email = $1", email) == 1
        await cleanup(email)

    run(t)


def test_supabase_token_authenticates_other_endpoints():
    email = f"{uniq()}@example.com"

    async def t(c):
        tok = token(sub("sb-authn"), email)
        await c.post("/api/auth/supabase/sync", headers=H(tok), json={"consent": True})
        me = await c.get("/api/auth/me", headers=H(tok))
        assert me.status_code == 200 and me.json()["email"] == email
        assert (await c.get("/api/reports", headers=H(tok))).status_code == 200
        await cleanup(email)

    run(t)


# ---------------------------------------------------------------------- linking

def test_links_to_existing_account_and_keeps_its_data():
    """The whole point of the migration: an existing user signing in via Supabase must
    keep their reports, plan and family - not silently start a fresh account."""
    email = f"{uniq()}@example.com"

    async def t(c):
        reg = await c.post("/api/auth/register", json={
            "email": email, "username": uniq("u"), "password": "Str0ng!Passw0rd",
            "name": "Existing User", "consent": True})
        old_id = reg.json()["user"]["user_id"]

        r = await c.post("/api/auth/supabase/sync", headers=H(token(sub("sb-link"), email)),
                         json={"consent": True})
        assert r.status_code == 200, r.text
        assert r.json()["user"]["user_id"] == old_id, "linking forked a second account"
        assert await db.fetchval("select count(*) from users where email = $1", email) == 1
        row = await db.fetchrow("select * from users where user_id = $1", old_id)
        assert str(row["supabase_uid"]) == sub("sb-link")
        assert row["password_hash"] is not None, "linking wiped the existing password"
        await cleanup(email)

    run(t)


def test_unverified_email_cannot_claim_an_existing_account():
    """Without this, anyone could sign up at Supabase using someone else's address and
    inherit their health data on first sync."""
    email = f"{uniq()}@example.com"

    async def t(c):
        await c.post("/api/auth/register", json={
            "email": email, "username": uniq("u"), "password": "Str0ng!Passw0rd",
            "name": "Victim", "consent": True})
        r = await c.post("/api/auth/supabase/sync",
                         headers=H(token(sub("sb-attacker"), email, verified=False)),
                         json={"consent": True})
        assert r.status_code == 403, f"unverified email claimed the account ({r.status_code})"
        row = await db.fetchrow("select supabase_uid from users where email = $1", email)
        assert row["supabase_uid"] is None
        await cleanup(email)

    run(t)


def test_second_identity_cannot_steal_a_linked_account():
    email = f"{uniq()}@example.com"

    async def t(c):
        await c.post("/api/auth/supabase/sync", headers=H(token(sub("sb-owner"), email)),
                     json={"consent": True})
        r = await c.post("/api/auth/supabase/sync", headers=H(token(sub("sb-intruder"), email)),
                         json={"consent": True})
        assert r.status_code == 409, f"second identity claimed the row ({r.status_code})"
        await cleanup(email)

    run(t)


def test_folin_tokens_still_work_alongside_supabase():
    """Both issuers must be accepted during the migration."""
    email = f"{uniq()}@example.com"

    async def t(c):
        reg = await c.post("/api/auth/register", json={
            "email": email, "username": uniq("u"), "password": "Str0ng!Passw0rd",
            "name": "Dual", "consent": True})
        folin_tok = reg.json()["access_token"]
        assert (await c.get("/api/auth/me", headers=H(folin_tok))).status_code == 200
        await c.post("/api/auth/supabase/sync", headers=H(token(sub("sb-dual"), email)), json={"consent": True})
        assert (await c.get("/api/auth/me", headers=H(folin_tok))).status_code == 200, \
            "linking broke the existing Folin session"
        await cleanup(email)

    run(t)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
