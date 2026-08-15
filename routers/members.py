"""Sub-profile routes. Thin: parse, call MemberService, respond."""
from fastapi import APIRouter, Depends

from app.models import MemberUpsertRequest
from core.security import get_current_user
from services.members import members

router = APIRouter(prefix="/members", tags=["members"])


@router.get("")
async def list_members(current_user: dict = Depends(get_current_user)):
    return await members.list_for(current_user)


@router.post("")
async def create_member(req: MemberUpsertRequest, current_user: dict = Depends(get_current_user)):
    return await members.create(req, current_user)


@router.patch("/{member_id}")
async def update_member(member_id: str, req: MemberUpsertRequest, current_user: dict = Depends(get_current_user)):
    return await members.update(member_id, req, current_user)


@router.delete("/{member_id}")
async def delete_member(member_id: str, current_user: dict = Depends(get_current_user)):
    await members.delete(member_id, current_user["user_id"])
    return {"deleted": True}
