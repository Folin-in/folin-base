"""Payment routes. Thin: parse, call PaymentService, respond."""
from fastapi import APIRouter, Depends

from app.models import RazorpayVerifyRequest
from core.security import get_current_user
from services.payments import payments

router = APIRouter(prefix="/payments", tags=["payments"])


@router.post("/razorpay/order")
async def create_order(current_user: dict = Depends(get_current_user)):
    return await payments.create_order(current_user)


@router.post("/razorpay/verify")
async def verify_payment(req: RazorpayVerifyRequest, current_user: dict = Depends(get_current_user)):
    return await payments.verify(req, current_user)
