"""Razorpay checkout for the Pro pass. Order -> Checkout collects payment -> the app sends
(order_id, payment_id, signature) back -> we verify the HMAC with the key secret and only
then activate Pro.

The client is never trusted with the price (config at order time, re-read from our own
row at verification) or with whether a payment succeeded (only a verifying HMAC flips
the plan). Replay is stopped by the unique index on razorpay_payment_id: reusing one
captured payment on a second order trips the constraint, surfaced as a clean 409.
"""
import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx
from fastapi import HTTPException

from app.config import settings
from app.constants import RAZORPAY_API, RAZORPAY_CURRENCY, RAZORPAY_TIMEOUT_SECS
from app.enums import PaymentStatus, Plan, PlanAction
from app.models import RazorpayVerifyRequest
from core import cache
from db import session as db

PAISE_PER_RUPEE = 100

MSG_ALREADY_PRO = "You're already on Pro."
MSG_PROVIDER_DOWN = "Couldn't reach the payment provider. Please try again."
MSG_ORDER_FAILED = "Couldn't start the payment. Please try again."
MSG_UNKNOWN_ORDER = "Unknown order."
MSG_NOT_VERIFIED = "Payment could not be verified. You have not been charged."
MSG_ALREADY_PROCESSED = "This payment was already processed."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def expected_signature(order_id: str, payment_id: str, secret: str) -> str:
    """Razorpay signs `order_id|payment_id` with the key secret (HMAC-SHA256, hex)."""
    return hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


class PaymentService:
    @staticmethod
    def _auth_header() -> dict:
        raw = f"{settings.razorpay_key_id}:{settings.razorpay_key_secret}".encode()
        return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}

    async def create_order(self, user: dict) -> dict:
        if ((user.get("preferences") or {}).get("plan") or Plan.FREE) == Plan.PRO:
            raise HTTPException(status_code=400, detail=MSG_ALREADY_PRO)
        amount_inr = settings.pro_price_inr
        try:
            async with httpx.AsyncClient(timeout=RAZORPAY_TIMEOUT_SECS) as client:
                r = await client.post(f"{RAZORPAY_API}/orders", headers=self._auth_header(), json={
                    "amount": amount_inr * PAISE_PER_RUPEE, "currency": RAZORPAY_CURRENCY,
                    "receipt": f"pro_{user['user_id']}_{int(_now().timestamp())}",
                    "notes": {"user_id": user["user_id"], "plan": str(Plan.PRO)}})
        except httpx.HTTPError:
            raise HTTPException(status_code=503, detail=MSG_PROVIDER_DOWN)
        if r.status_code >= 400:
            raise HTTPException(status_code=502, detail=MSG_ORDER_FAILED)
        order = r.json()
        await db.execute(
            """insert into payments (order_id, user_id, amount_inr, currency, status, created_at)
               values ($1, $2, $3, $4, $5, $6)""",
            order["id"], user["user_id"], amount_inr, RAZORPAY_CURRENCY, str(PaymentStatus.CREATED), _now().isoformat())
        return {"order_id": order["id"], "amount": order["amount"], "currency": order["currency"],
                "key_id": settings.razorpay_key_id, "term_days": settings.pro_term_days, "amount_inr": amount_inr}

    @staticmethod
    async def verify(req: RazorpayVerifyRequest, user: dict) -> dict:
        row = await db.fetchrow("select * from payments where order_id = $1", req.razorpay_order_id)
        if not row or row["user_id"] != user["user_id"]:
            raise HTTPException(status_code=404, detail=MSG_UNKNOWN_ORDER)
        expected = expected_signature(req.razorpay_order_id, req.razorpay_payment_id, settings.razorpay_key_secret)
        if not hmac.compare_digest(expected, req.razorpay_signature):
            await db.execute("update payments set status = $1 where order_id = $2", str(PaymentStatus.FAILED), req.razorpay_order_id)
            raise HTTPException(status_code=400, detail=MSG_NOT_VERIFIED)
        if row["status"] == PaymentStatus.PAID:
            return {"ok": True, "already_applied": True, "preferences": user.get("preferences") or {}}
        now = _now()
        expires_at = (now + timedelta(days=settings.pro_term_days)).isoformat()
        try:
            marked = await db.fetchval(
                """update payments set status = $1, razorpay_payment_id = $3, paid_at = $4
                   where order_id = $2 and status = $5 returning 1""",
                str(PaymentStatus.PAID), req.razorpay_order_id, req.razorpay_payment_id, now.isoformat(),
                str(PaymentStatus.CREATED))
        except asyncpg.UniqueViolationError:
            raise HTTPException(status_code=409, detail=MSG_ALREADY_PROCESSED)
        if not marked:
            raise HTTPException(status_code=409, detail=MSG_ALREADY_PROCESSED)
        merged = {**(user.get("preferences") or {}), "plan": str(Plan.PRO), "plan_changed_at": now.isoformat(),
                  "plan_expires_at": expires_at}
        await db.execute("update users set preferences = $1 where user_id = $2", merged, user["user_id"])
        await db.execute(
            """insert into plan_history (user_id, action, plan, amount_inr, starts_at, expires_at, created_at)
               values ($1, $2, $3, $4, $5, $6, $5)""",
            user["user_id"], str(PlanAction.UPGRADED), str(Plan.PRO), row["amount_inr"], now.isoformat(), expires_at)
        cache.invalidate(user["user_id"])
        return {"ok": True, "already_applied": False, "preferences": merged}


payments = PaymentService()
