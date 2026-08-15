"""The family invite lifecycle and its security boundaries, through the ASGI app.

    ./venv/bin/python -m pytest tests/test_family_routes.py -v
"""
import asyncio
import uuid

import httpx

from app.main import app
from core import rate_limit
from db import session as db

_LOOP = asyncio.get_event_loop()
_ready = False


def run(coro):
    async def w():
        global _ready
        if not _ready:
            await db.connect(); _ready = True
        rate_limit._WINDOWS.clear()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            return await coro(c)
    return _LOOP.run_until_complete(w())


async def user(c, plan="free", email=None):
    email = email or f"fam_{uuid.uuid4().hex[:8]}@example.com"
    r = await c.post("/api/auth/register", json={"email": email, "username": f"f{uuid.uuid4().hex[:6]}",
                                                 "password": "Str0ngPass1", "name": "Fam", "consent": True})
    assert r.status_code == 200, r.text
    uid = r.json()["user"]["user_id"]
    if plan == "pro":
        await db.execute("update users set preferences = preferences || '{\"plan\": \"pro\"}' where user_id = $1", uid)
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, uid, email


async def cleanup(*emails):
    for e in emails:
        await db.execute("delete from users where email = $1", e)


def test_full_invite_lifecycle_and_two_way_link():
    async def t(c):
        hp, pro_id, e1 = await user(c, plan="pro")
        hf, free_id, e2 = await user(c)
        r = await c.post("/api/family/invite", headers=hp, json={"email": e2, "relationship": "sibling"})
        assert r.json() == {"status": "invite_sent"}
        out = (await c.get("/api/family/invites", headers=hp)).json()
        assert len(out) == 1 and out[0]["email"] == e2 and out[0]["relationship"] == "sibling"

        inc = (await c.get("/api/family/incoming-invites", headers=hf)).json()
        assert len(inc) == 1 and inc[0]["inviter_email"] == e1
        acc = await c.post(f"/api/family/incoming-invites/{inc[0]['id']}/accept", headers=hf)
        assert acc.status_code == 200 and acc.json()["status"] == "accepted"
        assert acc.json()["member"]["user_id"] == pro_id
        assert "biomarkers" not in acc.json()["member"], "summary must never carry raw markers"

        for h, other in ((hp, free_id), (hf, pro_id)):
            m = (await c.get("/api/family/members", headers=h)).json()
            assert m["self"]["relationship"] == "you"
            assert [x["user_id"] for x in m["members"]] == [other], "link must be two-way"
        assert (await c.get("/api/family/invites", headers=hp)).json() == [], "accepted invite leaves pending list"

        assert (await c.delete(f"/api/family/members/{free_id}", headers=hp)).json() == {"removed": True}
        assert (await c.get("/api/family/members", headers=hf)).json()["members"] == [], "removal is two-way"
        await cleanup(e1, e2)
    run(t)


def test_invite_is_pro_gated_and_uniform_for_unknown_and_self():
    async def t(c):
        hf, _, e_free = await user(c)
        assert (await c.post("/api/family/invite", headers=hf, json={"email": "x@example.com"})).status_code == 403
        hp, _, e_pro = await user(c, plan="pro")
        assert (await c.post("/api/family/invite", headers=hp, json={"email": e_pro})).status_code == 400
        # unknown address and a real one must be indistinguishable from the response
        a = await c.post("/api/family/invite", headers=hp, json={"email": f"nobody_{uuid.uuid4().hex}@example.com"})
        b = await c.post("/api/family/invite", headers=hp, json={"email": e_free})
        assert a.status_code == b.status_code == 200 and a.json() == b.json() == {"status": "invite_sent"}
        # inviting the same address twice is also uniform, and creates no second row
        b2 = await c.post("/api/family/invite", headers=hp, json={"email": e_free})
        assert b2.json() == {"status": "invite_sent"}
        assert len((await c.get("/api/family/invites", headers=hp)).json()) == 2  # nobody + e_free, not 3
        await cleanup(e_free, e_pro)
    run(t)


def test_bound_invite_cannot_be_taken_by_another_account_with_the_same_email():
    """The takeover guard. Invitee exists at invite time -> invite is bound to their user_id.
    A different account that later takes over that email must not see or accept it."""
    async def t(c):
        hp, _, e_pro = await user(c, plan="pro")
        hv, victim_id, e_victim = await user(c)
        await c.post("/api/family/invite", headers=hp, json={"email": e_victim})
        inv_id = (await c.get("/api/family/incoming-invites", headers=hv)).json()[0]["id"]

        # victim moves their email away; attacker registers a new account then swaps in the freed address
        await db.execute("update users set email = $1 where user_id = $2", f"moved_{uuid.uuid4().hex[:6]}@example.com", victim_id)
        ha, attacker_id, e_att = await user(c)
        await db.execute("update users set email = $1 where user_id = $2", e_victim, attacker_id)

        assert (await c.get("/api/family/incoming-invites", headers=ha)).json() == [], "attacker can see a bound invite"
        assert (await c.post(f"/api/family/incoming-invites/{inv_id}/accept", headers=ha)).status_code == 404
        assert (await c.post(f"/api/family/incoming-invites/{inv_id}/decline", headers=ha)).status_code == 404
        # the real invitee (now on a different email) can still accept - bound by user_id, not email
        assert (await c.post(f"/api/family/incoming-invites/{inv_id}/accept", headers=hv)).status_code == 200
        await db.execute("delete from users where user_id = any($1::text[])", [victim_id, attacker_id])
        await cleanup(e_pro)
    run(t)


def test_cannot_accept_own_invite_or_cancel_someone_elses():
    async def t(c):
        hp, pro_id, e_pro = await user(c, plan="pro")
        hf, _, e_free = await user(c)
        await c.post("/api/family/invite", headers=hp, json={"email": e_free})
        inv_id = (await c.get("/api/family/invites", headers=hp)).json()[0]["id"]
        assert (await c.post(f"/api/family/incoming-invites/{inv_id}/accept", headers=hp)).status_code == 404  # not addressed to inviter
        assert (await c.delete(f"/api/family/invites/{inv_id}", headers=hf)).status_code == 404  # not the inviter
        assert (await c.delete(f"/api/family/invites/{inv_id}", headers=hp)).json() == {"deleted": True}
        assert (await c.get("/api/family/incoming-invites", headers=hf)).json() == []
        await cleanup(e_pro, e_free)
    run(t)


def test_decline_marks_and_hides():
    async def t(c):
        hp, _, e_pro = await user(c, plan="pro")
        hf, _, e_free = await user(c)
        await c.post("/api/family/invite", headers=hp, json={"email": e_free})
        inv_id = (await c.get("/api/family/incoming-invites", headers=hf)).json()[0]["id"]
        assert (await c.post(f"/api/family/incoming-invites/{inv_id}/decline", headers=hf)).json() == {"status": "declined"}
        assert (await c.get("/api/family/incoming-invites", headers=hf)).json() == []
        assert (await c.post(f"/api/family/incoming-invites/{inv_id}/accept", headers=hf)).status_code == 404
        await cleanup(e_pro, e_free)
    run(t)


def test_family_routes_require_auth():
    async def t(c):
        for m, p in (("get", "/api/family/members"), ("get", "/api/family/invites"), ("get", "/api/family/incoming-invites"),
                     ("post", "/api/family/invite"), ("delete", "/api/family/members/x")):
            r = await (c.post(p, json={}) if m == "post" else getattr(c, m)(p))
            assert r.status_code in (401, 403), f"{m} {p} -> {r.status_code}"
    run(t)
