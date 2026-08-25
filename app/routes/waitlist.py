"""Waitlist for White Label and Italy (T12 / T11)."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from pymongo import MongoClient
import os
import certifi
from dotenv import load_dotenv

from app.routes.auth import get_current_user
from app.services.waitlist_service import (
    WaitlistError,
    WaitlistService,
    can_view_sales_waitlist,
)

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI"), tlsCAFile=certifi.where())
db = client[os.getenv("DB_NAME")]

router = APIRouter(prefix="/waitlist", tags=["Waitlist"])
service = WaitlistService(db["waitlist"])


class WaitlistJoinRequest(BaseModel):
    interest: str = Field(..., examples=["white_label", "italy"])
    source: Optional[str] = Field("onboarding", examples=["onboarding"])


@router.post("/", status_code=201)
def join_waitlist(
    body: WaitlistJoinRequest,
    current_user: dict = Depends(get_current_user),
):
    """Store White Label or Italy interest. Idempotent per user + interest."""
    try:
        return service.join(current_user, body.interest, body.source)
    except WaitlistError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/me")
def my_waitlist(current_user: dict = Depends(get_current_user)):
    return service.list_for_user(str(current_user["_id"]))


@router.get("/")
def list_waitlist(current_user: dict = Depends(get_current_user)):
    """Sales view: admin or advisor accounts."""
    if not can_view_sales_waitlist(current_user):
        raise HTTPException(
            status_code=403,
            detail="Only admin or advisor accounts can list waitlist interest.",
        )
    return service.list_all()
