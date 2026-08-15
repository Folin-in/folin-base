"""Razorpay checkout for the Pro pass.

Flow: app asks for an order -> Razorpay Checkout collects payment -> app sends the
(order_id, payment_id, signature) triple back -> we verify the HMAC with the key secret
and only then activate Pro.

The client never gets a say in the price, and never gets to declare a payment successful:
the amount comes from config at order time and is re-read from our own row at verification.
"""
import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import cache
import config
from db import session as db
from security import get_current_user

router = APIRouter(prefix="/payments", tags=["payments"])

RAZORPAY_API = "https://api.razorpay.com/v1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _auth_header() -> dict:
    raw = f"{config.RAZORPAY_KEY_ID}:{config.RAZORPAY_KEY_SECRET}".encode()
    return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}


def expected_signature(order_id: str, payment_id: str, secret: str) -> str:
    """Razorpay signs `order_id|payment_id` with the key secret (HMAC-SHA256, hex)."""
    return hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


class VerifyRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


@router.post("/razorpay/order")
async def create_order(current_user: dict = Depends(get_current_user)):
    """Create a Razorpay order for the Pro pass. Amount is server-decided."""
    plan = (current_user.get("preferences") or {}).get("plan") or "free"
    if plan == "pro":
        raise HTTPException(status_code=400, detail="You're already on Pro.")

    amount_inr = config.PRO_PRICE_INR
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{RAZORPAY_API}/orders",
                headers=_auth_header(),
                json={
                    "amount": amount_inr * 100,  # Razorpay works in paise
                    "currency": "INR",
                    "receipt": f"pro_{current_user['user_id']}_{int(_now().timestamp())}",
                    "notes": {"user_id": current_user["user_id"], "plan": "pro"},
                },
            )
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail="Couldn't reach the payment provider. Please try again.")
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail="Couldn't start the payment. Please try again.")

    order = r.json()
    await db.execute(
        """insert into payments (order_id, user_id, amount_inr, currency, status, created_at)
           values ($1, $2, $3, $4, 'created', $5)""",
        order["id"], current_user["user_id"], amount_inr, "INR", _now().isoformat(),
    )
    # key_id is public - the app needs it to open Checkout. The secret never leaves here.
    return {
        "order_id": order["id"],
        "amount": order["amount"],
        "currency": order["currency"],
        "key_id": config.RAZORPAY_KEY_ID,
        "term_days": config.PRO_TERM_DAYS,
        "amount_inr": amount_inr,
    }


@router.post("/razorpay/verify")
async def verify_payment(req: VerifyRequest, current_user: dict = Depends(get_current_user)):
    """Verify the signature and activate Pro. Idempotent: re-verifying the same payment
    returns the existing state instead of extending the term again."""
    row = await db.fetchrow("select * from payments where order_id = $1", req.razorpay_order_id)
    if not row:
        raise HTTPException(status_code=404, detail="Unknown order.")
    if row["user_id"] != current_user["user_id"]:
        # Someone else's order - do not confirm or deny which, just refuse.
        raise HTTPException(status_code=404, detail="Unknown order.")

    expected = expected_signature(req.razorpay_order_id, req.razorpay_payment_id, config.RAZORPAY_KEY_SECRET)
    if not hmac.compare_digest(expected, req.razorpay_signature):
        await db.execute("update payments set status = 'failed' where order_id = $1", req.razorpay_order_id)
        raise HTTPException(status_code=400, detail="Payment could not be verified. You have not been charged.")

    if row["status"] == "paid":
        return {"ok": True, "already_applied": True, "preferences": current_user.get("preferences") or {}}

    now = _now()
    expires_at = (now + timedelta(days=config.PRO_TERM_DAYS)).isoformat()
    # The unique index on razorpay_payment_id is what actually stops a replay across orders:
    # reusing one captured payment on a second order trips the constraint here.
    try:
        marked = await db.fetchval(
            """update payments set status = 'paid', razorpay_payment_id = $2, paid_at = $3
               where order_id = $1 and status = 'created' returning 1""",
            req.razorpay_order_id, req.razorpay_payment_id, now.isoformat(),
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="This payment was already processed.")
    if not marked:
        raise HTTPException(status_code=409, detail="This payment was already processed.")

    merged = {
        **(current_user.get("preferences") or {}),
        "plan": "pro",
        "plan_changed_at": now.isoformat(),
        "plan_expires_at": expires_at,
    }
    await db.execute("update users set preferences = $1 where user_id = $2", merged, current_user["user_id"])
    await db.execute(
        """insert into plan_history (user_id, action, plan, amount_inr, starts_at, expires_at, created_at)
           values ($1, 'upgraded', 'pro', $2, $3, $4, $3)""",
        current_user["user_id"], row["amount_inr"], now.isoformat(), expires_at,
    )
    cache.invalidate(current_user["user_id"])
    return {"ok": True, "already_applied": False, "preferences": merged}
