"""Family routes. Thin: parse, call FamilyService, respond."""
from fastapi import APIRouter, Depends

from app.models import FamilyInviteRequest
from core.security import get_current_user
from services.family import family

router = APIRouter(prefix="/family", tags=["family"])


@router.post("/invite")
async def invite_family(req: FamilyInviteRequest, current_user: dict = Depends(get_current_user)):
    return await family.invite(req, current_user)


@router.get("/invites")
async def list_pending_invites(current_user: dict = Depends(get_current_user)):
    return await family.outgoing(current_user["user_id"])


@router.delete("/invites/{invite_id}")
async def cancel_invite(invite_id: str, current_user: dict = Depends(get_current_user)):
    await family.cancel(invite_id, current_user["user_id"])
    return {"deleted": True}


@router.get("/incoming-invites")
async def list_incoming_invites(current_user: dict = Depends(get_current_user)):
    return await family.incoming(current_user)


@router.post("/incoming-invites/{invite_id}/accept")
async def accept_incoming_invite(invite_id: str, current_user: dict = Depends(get_current_user)):
    return await family.accept(invite_id, current_user)


@router.post("/incoming-invites/{invite_id}/decline")
async def decline_incoming_invite(invite_id: str, current_user: dict = Depends(get_current_user)):
    return await family.decline(invite_id, current_user)


@router.get("/members")
async def list_family_members(current_user: dict = Depends(get_current_user)):
    return await family.members(current_user)


@router.delete("/members/{user_id}")
async def remove_member(user_id: str, current_user: dict = Depends(get_current_user)):
    await family.remove_member(current_user["user_id"], user_id)
    return {"removed": True}
