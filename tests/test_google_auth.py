"""Google sign-in: token verification + account linking.

Runs in-process against the real DB with a locally generated RSA key standing in
for Google's JWKS, so every rejection branch and linking path is exercised without
needing a live Google token.

    ./venv/bin/python -m pytest tests/test_google_auth.py -v
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
os.environ.setdefault("GOOGLE_CLIENT_IDS", "test-client-id.apps.googleusercontent.com")

import httpx  # noqa: E402
import jwt as pyjwt  # noqa: E402

import config  # noqa: E402
import database as db  # noqa: E402
import rate_limit  # noqa: E402
from routers import auth as auth_router  # noqa: E402
from server import app  # noqa: E402

AUD = config.GOOGLE_CLIENT_IDS[0]
ISS = "https://accounts.google.com"

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIV = _key.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)
_OTHER_PRIV = _other_key.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)


class _FakeJWKS:
    """Stands in for Google's key server; always hands back our test public key."""

    def __init__(self, pub):
        self.key = pub

    def get_signing_key_from_jwt(self, token):
        return self


auth_router._GOOGLE_JWKS = _FakeJWKS(_key.public_key())


def id_token(sub, email, *, aud=AUD, iss=ISS, verified=True, exp_delta=3600,
             name=None, picture=None, sign_with=_PRIV, **extra):
    now = int(time.time())
    claims = {
        "iss": iss, "aud": aud, "sub": sub, "email": email,
        "email_verified": verified, "iat": now, "exp": now + exp_delta,
    }
    if name:
        claims["name"] = name
    if picture:
        claims["picture"] = picture
    claims.update(extra)
    return pyjwt.encode(claims, sign_with, algorithm="RS256")


# One loop for the whole module: the asyncpg pool is bound to the loop that created it,
# so a per-test asyncio.run() would close it out from under the next test.
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)
_pool_ready = False


def run(coro):
    async def _wrapped():
        global _pool_ready
        if not _pool_ready:
            await db.connect()
            # Rows left behind by an aborted run would collide on the fixed test subs.
            await db.execute("delete from users where google_id like 'tsub-%'")
            _pool_ready = True
        rate_limit._WINDOWS.clear()  # each test starts with a fresh throttle window
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await coro(c)

    return _LOOP.run_until_complete(_wrapped())


def uniq(prefix="g"):
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


async def cleanup(*emails):
    for e in emails:
        await db.execute("delete from users where email = $1", e.lower())


# ---------------------------------------------------------------- verification

def test_malformed_token_rejected():
    async def t(c):
        for bad in ["", "not.a.jwt", "a.b.c", "Bearer xyz", "." * 50]:
            r = await c.post("/api/auth/google", json={"id_token": bad})
            assert r.status_code == 401, f"{bad!r} -> {r.status_code}"
            assert "Invalid Google token" in r.json()["detail"]

    run(t)


def test_wrong_signing_key_rejected():
    async def t(c):
        tok = id_token("tsub-wrongkey", "wrongkey@example.com", sign_with=_OTHER_PRIV)
        r = await c.post("/api/auth/google", json={"id_token": tok})
        assert r.status_code == 401

    run(t)


def test_alg_none_rejected():
    """Classic JWT confusion: unsigned token claiming alg=none must not authenticate."""
    async def t(c):
        tok = pyjwt.encode(
            {"iss": ISS, "aud": AUD, "sub": "s", "email": "none@example.com",
             "email_verified": True, "exp": int(time.time()) + 600},
            key="", algorithm="none",
        )
        r = await c.post("/api/auth/google", json={"id_token": tok})
        assert r.status_code == 401

    run(t)


def test_hs256_key_confusion_rejected():
    """Token symmetrically signed with the public key must not verify as RS256."""
    async def t(c):
        # pyjwt refuses to *encode* this, so forge the wire format by hand.
        pub_pem = _key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=")  # noqa: E731
        signing_input = b".".join([
            b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()),
            b64(json.dumps({"iss": ISS, "aud": AUD, "sub": "s", "email": "hs@example.com",
                            "email_verified": True, "exp": int(time.time()) + 600}).encode()),
        ])
        sig = hmac.new(pub_pem, signing_input, hashlib.sha256).digest()
        tok = (signing_input + b"." + b64(sig)).decode()
        r = await c.post("/api/auth/google", json={"id_token": tok})
        assert r.status_code == 401

    run(t)


def test_wrong_audience_rejected():
    """A token minted for someone else's OAuth client must not log into Folin."""
    async def t(c):
        tok = id_token("tsub-aud", "aud@example.com", aud="attacker-client.apps.googleusercontent.com")
        r = await c.post("/api/auth/google", json={"id_token": tok})
        assert r.status_code == 401

    run(t)


def test_expired_token_rejected():
    async def t(c):
        tok = id_token("tsub-exp", "exp@example.com", exp_delta=-60)
        r = await c.post("/api/auth/google", json={"id_token": tok})
        assert r.status_code == 401

    run(t)


def test_wrong_issuer_rejected():
    async def t(c):
        tok = id_token("tsub-iss", "iss@example.com", iss="https://evil.example.com")
        r = await c.post("/api/auth/google", json={"id_token": tok})
        assert r.status_code == 401

    run(t)


def test_unverified_email_rejected():
    async def t(c):
        tok = id_token("tsub-unv", "unverified@example.com", verified=False)
        r = await c.post("/api/auth/google", json={"id_token": tok})
        assert r.status_code == 401
        assert "verified email" in r.json()["detail"]

    run(t)


def test_missing_email_rejected():
    async def t(c):
        tok = id_token("tsub-noemail", "")
        r = await c.post("/api/auth/google", json={"id_token": tok})
        assert r.status_code == 401

    run(t)


def test_not_configured_returns_503():
    async def t(c):
        saved = config.GOOGLE_CLIENT_IDS
        config.GOOGLE_CLIENT_IDS = []
        try:
            r = await c.post("/api/auth/google", json={"id_token": id_token("s", "x@example.com")})
            assert r.status_code == 503
        finally:
            config.GOOGLE_CLIENT_IDS = saved

    run(t)


def test_missing_body_field_is_422():
    async def t(c):
        r = await c.post("/api/auth/google", json={})
        assert r.status_code == 422

    run(t)


def test_rate_limited_after_20_attempts():
    async def t(c):
        bad = "not.a.jwt"
        codes = [(await c.post("/api/auth/google", json={"id_token": bad})).status_code
                 for _ in range(22)]
        assert codes[:20] == [401] * 20
        assert codes[20] == 429 and codes[21] == 429

    run(t)


# ------------------------------------------------------------------- new users

def test_new_user_created_and_is_passwordless():
    email = f"{uniq()}@example.com"

    async def t(c):
        tok = id_token("sub-new-1", email, name="Ada Lovelace", picture="https://pic/a.png")
        r = await c.post("/api/auth/google", json={"id_token": tok})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["access_token"] and body["refresh_token"]
        u = body["user"]
        assert u["email"] == email
        assert u["name"] == "Ada Lovelace"

        row = await db.fetchrow("select * from users where email = $1", email)
        assert row["auth_provider"] == "google"
        assert row["google_id"] == "sub-new-1"
        assert row["password_hash"] is None
        assert row["username"] == email.split("@")[0][:24]

        # token actually works
        me = await c.get("/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
        assert me.status_code == 200 and me.json()["email"] == email
        await cleanup(email)

    run(t)


def test_repeat_signin_reuses_same_account():
    email = f"{uniq()}@example.com"

    async def t(c):
        tok = id_token("tsub-repeat", email, name="Repeat User")
        first = (await c.post("/api/auth/google", json={"id_token": tok})).json()
        second = (await c.post("/api/auth/google", json={"id_token": tok})).json()
        assert first["user"]["user_id"] == second["user"]["user_id"]
        assert await db.fetchval("select count(*) from users where email = $1", email) == 1
        # fresh token pair each time
        assert first["refresh_token"] != second["refresh_token"]
        await cleanup(email)

    run(t)


def test_google_signin_survives_google_side_email_change():
    """Same Google sub, new Gmail address -> same Folin account, email updated or kept,
    never a silent duplicate account."""
    old = f"{uniq()}@example.com"
    new = f"{uniq()}@example.com"

    async def t(c):
        sub = "tsub-emailchange"
        a = (await c.post("/api/auth/google", json={"id_token": id_token(sub, old)})).json()
        b = (await c.post("/api/auth/google", json={"id_token": id_token(sub, new)})).json()
        assert a["user"]["user_id"] == b["user"]["user_id"], "sub change created a duplicate account"
        await cleanup(old, new)

    run(t)


def test_username_collision_gets_suffix():
    local = uniq("dup")
    e1, e2 = f"{local}@a.example.com", f"{local}@b.example.com"

    async def t(c):
        u1 = (await c.post("/api/auth/google", json={"id_token": id_token("tsub-c1", e1)})).json()["user"]
        u2 = (await c.post("/api/auth/google", json={"id_token": id_token("tsub-c2", e2)})).json()["user"]
        assert u1["username"] == local
        assert u2["username"] == f"{local}1"
        await cleanup(e1, e2)

    run(t)


def test_social_user_has_no_consent_until_accepted():
    """Google signup skips the register consent write, so the in-app consent prompt
    still gates them. Guards against silently treating social users as consented."""
    email = f"{uniq()}@example.com"

    async def t(c):
        r = (await c.post("/api/auth/google", json={"id_token": id_token("tsub-consent", email)})).json()
        assert not r["user"].get("consented_at")
        await cleanup(email)

    run(t)


# --------------------------------------------------------------------- linking

def test_links_to_existing_password_account_by_email():
    email = f"{uniq()}@example.com"
    pw = "Str0ng!Passw0rd"

    async def t(c):
        reg = await c.post("/api/auth/register", json={
            "email": email, "username": uniq("u"), "password": pw,
            "name": "Local User", "consent": True,
        })
        assert reg.status_code == 200, reg.text
        local_id = reg.json()["user"]["user_id"]

        g = await c.post("/api/auth/google", json={"id_token": id_token("tsub-link", email)})
        assert g.status_code == 200
        assert g.json()["user"]["user_id"] == local_id, "google login forked a second account"

        row = await db.fetchrow("select * from users where email = $1", email)
        assert row["google_id"] == "tsub-link"
        assert row["password_hash"] is not None, "linking wiped the password"

        # password login must still work after linking
        pl = await c.post("/api/auth/login", json={"identifier": email, "password": pw})
        assert pl.status_code == 200
        await cleanup(email)

    run(t)


def test_linking_does_not_overwrite_user_edited_name():
    email = f"{uniq()}@example.com"

    async def t(c):
        await c.post("/api/auth/register", json={
            "email": email, "username": uniq("u"), "password": "Str0ng!Passw0rd",
            "name": "My Chosen Name", "consent": True,
        })
        await c.post("/api/auth/google", json={"id_token": id_token("tsub-name", email, name="Google Name")})
        row = await db.fetchrow("select name from users where email = $1", email)
        assert row["name"] == "My Chosen Name"
        await cleanup(email)

    run(t)


def test_password_login_on_google_only_account_is_guided():
    email = f"{uniq()}@example.com"

    async def t(c):
        await c.post("/api/auth/google", json={"id_token": id_token("tsub-guide", email)})
        r = await c.post("/api/auth/login", json={"identifier": email, "password": "whatever123"})
        assert r.status_code == 400
        assert "Google" in r.json()["detail"]
        await cleanup(email)

    run(t)


def test_stale_email_row_cannot_hijack_a_google_account():
    """A Google user changes their Folin email; someone else later registers with the
    freed address. The next Google sign-in matches BOTH rows -- it must resolve to the
    account that owns the Google sub, never the stranger's."""
    gmail = f"{uniq()}@example.com"
    moved = f"{uniq()}@example.com"

    async def t(c):
        sub = "tsub-hijack"
        a = (await c.post("/api/auth/google", json={"id_token": id_token(sub, gmail)})).json()
        a_id = a["user"]["user_id"]
        # user moves their Folin email off the Gmail address
        await db.execute("update users set email = $1 where user_id = $2", moved, a_id)
        # stranger registers with the freed address
        s = await c.post("/api/auth/register", json={
            "email": gmail, "username": uniq("s"), "password": "Str0ng!Passw0rd",
            "name": "Stranger", "consent": True,
        })
        stranger_id = s.json()["user"]["user_id"]

        again = (await c.post("/api/auth/google", json={"id_token": id_token(sub, gmail)})).json()
        assert again["user"]["user_id"] != stranger_id, "Google sign-in logged into a stranger's account"
        assert again["user"]["user_id"] == a_id

        stranger = await db.fetchrow("select google_id from users where user_id = $1", stranger_id)
        assert stranger["google_id"] is None, "stranger's account got the victim's google_id"
        await cleanup(gmail, moved)

    run(t)


def test_second_google_account_cannot_steal_a_linked_row():
    """Different Google sub, same email as an already-linked account: must not silently
    repoint the existing row's google_id at the new sub."""
    email = f"{uniq()}@example.com"

    async def t(c):
        await c.post("/api/auth/google", json={"id_token": id_token("tsub-owner", email)})
        await c.post("/api/auth/google", json={"id_token": id_token("tsub-intruder", email)})
        row = await db.fetchrow("select google_id from users where email = $1", email)
        assert row["google_id"] == "tsub-owner", f"google_id repointed to {row['google_id']}"
        await cleanup(email)

    run(t)


# ------------------------------------------------------------------ session use

def test_refresh_rotation_works_for_google_session():
    email = f"{uniq()}@example.com"

    async def t(c):
        s = (await c.post("/api/auth/google", json={"id_token": id_token("tsub-refresh", email)})).json()
        r = await c.post("/api/auth/refresh", json={"refresh_token": s["refresh_token"]})
        assert r.status_code == 200
        # single-use: replaying the consumed token must fail
        again = await c.post("/api/auth/refresh", json={"refresh_token": s["refresh_token"]})
        assert again.status_code == 401
        await cleanup(email)

    run(t)


def test_logout_revokes_google_session_then_signin_works_again():
    email = f"{uniq()}@example.com"

    async def t(c):
        tok = id_token("tsub-logout", email)
        s = (await c.post("/api/auth/google", json={"id_token": tok})).json()
        h = {"Authorization": f"Bearer {s['access_token']}"}
        assert (await c.post("/api/auth/logout", headers=h)).status_code == 200
        assert (await c.get("/api/auth/me", headers=h)).status_code == 401
        # signing in again gives a fresh working session on the same account
        s2 = (await c.post("/api/auth/google", json={"id_token": tok})).json()
        assert s2["user"]["user_id"] == s["user"]["user_id"]
        h2 = {"Authorization": f"Bearer {s2['access_token']}"}
        assert (await c.get("/api/auth/me", headers=h2)).status_code == 200
        await cleanup(email)

    run(t)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
