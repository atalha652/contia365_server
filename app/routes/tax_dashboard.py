"""
Tax Dashboard Routes
Returns deadline dates for all periodic tax obligations from a user's census record.
"""

import os
import certifi
from datetime import date
from typing import Optional

from bson import ObjectId
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Depends, Query
from pymongo import MongoClient

from app.models.tax_dashboard import TaxDeadlineItem, TaxDeadlinesResponse
from app.routes.auth import get_current_user

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")

client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = client[DB_NAME]

router = APIRouter(prefix="/tax-dashboard", tags=["Tax Dashboard"])

# ─────────────────────────── deadline calculator ────────────────────────────

# Spanish AEAT filing windows (days after period end)
# TRIMESTRAL quarters end: Q1=Mar31, Q2=Jun30, Q3=Sep30, Q4=Dec31
# Filing deadline is typically the 20th of the month following quarter end
# (Q4 is 30th Jan for most modelos)
QUARTERLY_DEADLINES = {
    1: date(date.today().year, 4, 20),   # Q1 → 1–20 April
    2: date(date.today().year, 7, 20),   # Q2 → 1–20 July
    3: date(date.today().year, 10, 20),  # Q3 → 1–20 October
    4: date(date.today().year + 1, 1, 30),  # Q4 → 1–30 January next year
}

QUARTER_LABELS = {1: "Q1", 2: "Q2", 3: "Q3", 4: "Q4"}

# Modelo-specific overrides (some have different deadlines)
MODELO_DEADLINE_OVERRIDES: dict[str, dict] = {
    # Modelo 303 (IVA) Q4 → until 30 Jan
    "303": {4: date(date.today().year + 1, 1, 30)},
    # Modelo 390 (IVA annual summary) → 30 Jan
    "390": {0: date(date.today().year + 1, 1, 30)},
    # Modelo 190 (IRPF annual summary) → 31 Jan
    "190": {0: date(date.today().year + 1, 1, 31)},
    # Modelo 347 (annual operations) → last day of Feb
    "347": {0: date(date.today().year + 1, 2, 28)},
}


def _current_quarter(today: date) -> int:
    return (today.month - 1) // 3 + 1


def _quarter_label(q: int, year: int) -> str:
    return f"{QUARTER_LABELS[q]} {year}"


def _compute_deadline(modelo: str, periodicity: str, today: date) -> tuple[str, str, date]:
    """
    Returns (period_label, deadline_date, deadline_date_obj).
    """
    periodicity_upper = periodicity.upper()

    if periodicity_upper == "TRIMESTRAL":
        q = _current_quarter(today)
        year = today.year

        # Use override if exists, else default quarterly deadline
        overrides = MODELO_DEADLINE_OVERRIDES.get(modelo, {})
        deadline = overrides.get(q) or QUARTERLY_DEADLINES.get(q)

        # Rebuild with correct year in case QUARTERLY_DEADLINES was built at import time
        if deadline:
            if q == 4:
                deadline = date(year + 1, deadline.month, deadline.day)
            else:
                deadline = date(year, deadline.month, deadline.day)

        period_label = _quarter_label(q, year)
        return period_label, deadline

    elif periodicity_upper == "MENSUAL":
        # Due on the 20th of the following month
        if today.month == 12:
            deadline = date(today.year + 1, 1, 20)
        else:
            deadline = date(today.year, today.month + 1, 20)
        period_label = today.strftime("%B %Y")
        return period_label, deadline

    elif periodicity_upper == "ANUAL":
        overrides = MODELO_DEADLINE_OVERRIDES.get(modelo, {})
        deadline = overrides.get(0) or date(today.year + 1, 6, 30)
        period_label = str(today.year)
        return period_label, deadline

    else:
        # Unknown periodicity — return end of current year
        deadline = date(today.year, 12, 31)
        return str(today.year), deadline


def _status(days_remaining: int) -> str:
    if days_remaining < 0:
        return "overdue"
    if days_remaining <= 7:
        return "due_soon"
    return "upcoming"


# ─────────────────────────── routes ─────────────────────────────────────────

@router.get("/deadlines", response_model=TaxDeadlinesResponse)
async def get_my_tax_deadlines(
    current_user: dict = Depends(get_current_user),
    census_id: Optional[str] = Query(None, description="Specific census record ID (uses latest if omitted)"),
):
    """
    Returns deadline dates for ALL periodic tax obligations found in the
    user's census record (periodic_tax_obligations array).

    Uses the latest census record by default, or a specific one via ?census_id=
    """
    user_id = str(current_user["_id"])

    # Fetch census record
    if census_id:
        if not ObjectId.is_valid(census_id):
            raise HTTPException(status_code=400, detail="Invalid census_id.")
        record = db["census_data"].find_one(
            {"_id": ObjectId(census_id), "user_id": user_id}
        )
    else:
        record = db["census_data"].find_one(
            {"user_id": user_id},
            sort=[("created_at", -1)]
        )

    if not record:
        raise HTTPException(
            status_code=404,
            detail="No census record found for this user.",
        )

    obligations = record.get("periodic_tax_obligations") or []
    if not obligations:
        raise HTTPException(
            status_code=404,
            detail="No periodic tax obligations found in this census record.",
        )

    today = date.today()
    deadlines = []

    for ob in obligations:
        modelo_no = ob.get("modelo") or "?"
        description = ob.get("description") or ""
        periodicity = ob.get("periodicity") or "TRIMESTRAL"

        period_label, deadline_date = _compute_deadline(modelo_no, periodicity, today)
        days_remaining = (deadline_date - today).days

        deadlines.append(TaxDeadlineItem(
            modelo=modelo_no,
            description=description,
            periodicity=periodicity,
            current_period=period_label,
            deadline_date=deadline_date.isoformat(),
            days_remaining=days_remaining,
            status=_status(days_remaining),
        ))

    # Sort by deadline date ascending
    deadlines.sort(key=lambda x: x.deadline_date)

    taxpayer = record.get("taxpayer_identity") or {}

    return TaxDeadlinesResponse(
        user_id=user_id,
        census_record_id=str(record["_id"]),
        nif_nie=taxpayer.get("nif_nie"),
        full_name=taxpayer.get("full_name"),
        deadlines=deadlines,
        total=len(deadlines),
    )


@router.get("/{user_id}")
async def get_tax_deadlines_by_user_id(user_id: str):
    """
    Legacy endpoint for frontend compatibility.
    Returns deadline dates for a specific user without authentication.
    """
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=400, detail="Invalid user_id.")

    # Fetch latest census record for user
    record = db["census_data"].find_one(
        {"user_id": user_id},
        sort=[("created_at", -1)]
    )

    if not record:
        # Return empty response instead of 404
        return TaxDeadlinesResponse(
            user_id=user_id,
            census_record_id=None,
            nif_nie=None,
            full_name=None,
            deadlines=[],
            total=0,
        )

    obligations = record.get("periodic_tax_obligations") or []
    if not obligations:
        # Return empty response instead of 404
        taxpayer = record.get("taxpayer_identity") or {}
        return TaxDeadlinesResponse(
            user_id=user_id,
            census_record_id=str(record["_id"]),
            nif_nie=taxpayer.get("nif_nie"),
            full_name=taxpayer.get("full_name"),
            deadlines=[],
            total=0,
        )

    today = date.today()
    deadlines = []

    for ob in obligations:
        modelo_no = ob.get("modelo") or "?"
        description = ob.get("description") or ""
        periodicity = ob.get("periodicity") or "TRIMESTRAL"

        period_label, deadline_date = _compute_deadline(modelo_no, periodicity, today)
        days_remaining = (deadline_date - today).days

        deadlines.append(TaxDeadlineItem(
            modelo=modelo_no,
            description=description,
            periodicity=periodicity,
            current_period=period_label,
            deadline_date=deadline_date.isoformat(),
            days_remaining=days_remaining,
            status=_status(days_remaining),
        ))

    # Sort by deadline date ascending
    deadlines.sort(key=lambda x: x.deadline_date)

    taxpayer = record.get("taxpayer_identity") or {}

    return TaxDeadlinesResponse(
        user_id=user_id,
        census_record_id=str(record["_id"]),
        nif_nie=taxpayer.get("nif_nie"),
        full_name=taxpayer.get("full_name"),
        deadlines=deadlines,
        total=len(deadlines),
    )
