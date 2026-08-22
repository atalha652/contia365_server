"""
Tax Dashboard Routes
Fiscal calendar of obligations and periods from the canonical fiscal profile.
"""

import os
from datetime import date
from typing import Optional

import certifi
from bson import ObjectId
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Query
from pymongo import MongoClient

from app.models.tax_dashboard import TaxDeadlinesResponse
from app.routes.auth import get_current_user
from app.services.fiscal_profile_service import get_canonical_fiscal_profile
from app.services.tax_obligations_service import build_fiscal_calendar

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")

client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = client[DB_NAME]
_tax_reports = db["tax_reports"]

router = APIRouter(prefix="/tax-dashboard", tags=["Tax Dashboard"])


def _counts(items) -> dict:
    counts = {"upcoming": 0, "due": 0, "overdue": 0, "completed": 0}
    for item in items:
        if item.status in counts:
            counts[item.status] += 1
    return counts


def _calendar_response(user_id: str, record: dict, year: int) -> TaxDeadlinesResponse:
    taxpayer = (record or {}).get("taxpayer_identity") or {}
    deadlines = build_fiscal_calendar(
        record or {},
        user_id,
        _tax_reports,
        tax_filings=db["tax_filings"],
        year=year,
    )
    return TaxDeadlinesResponse(
        user_id=user_id,
        census_record_id=str(record["_id"]) if record else None,
        nif_nie=taxpayer.get("nif_nie"),
        full_name=taxpayer.get("full_name"),
        year=year,
        deadlines=deadlines,
        total=len(deadlines),
        counts=_counts(deadlines),
    )


@router.get("/deadlines", response_model=TaxDeadlinesResponse)
async def get_my_tax_deadlines(
    current_user: dict = Depends(get_current_user),
    year: Optional[int] = Query(None, description="Calendar year (defaults to current year)"),
):
    """
    Fiscal calendar: one row per modelo × period from the canonical profile.
    Statuses: upcoming | due | overdue | completed (accepted filing;
    legacy filed reports are still recognized).
    """
    record = get_canonical_fiscal_profile(
        db["users"], db["census_data"], current_user["_id"]
    )
    if not record:
        raise HTTPException(status_code=404, detail="No fiscal profile found for this user.")
    if not (record.get("periodic_tax_obligations") or []):
        raise HTTPException(
            status_code=404,
            detail="No periodic tax obligations found on this fiscal profile.",
        )
    return _calendar_response(str(current_user["_id"]), record, year or date.today().year)


@router.get("/{user_id}", response_model=TaxDeadlinesResponse)
async def get_tax_deadlines_by_user_id(
    user_id: str,
    year: Optional[int] = Query(None),
):
    """Legacy unauthenticated calendar for a user id."""
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=400, detail="Invalid user_id.")

    record = get_canonical_fiscal_profile(db["users"], db["census_data"], user_id)
    calendar_year = year or date.today().year
    if not record:
        return TaxDeadlinesResponse(
            user_id=user_id,
            census_record_id=None,
            deadlines=[],
            total=0,
            year=calendar_year,
            counts=_counts([]),
        )
    return _calendar_response(user_id, record, calendar_year)
