"""
Tax Engine Routes
=================
POST /tax-engine/303/calculate   → Modelo 303 (IVA)   — requires modelo_id
POST /tax-engine/130/calculate   → Modelo 130 (IRPF)  — requires modelo_id
GET  /tax-engine/reports         → List saved reports
PATCH /tax-engine/reports/{id}/status → Finalize or mark as filed

Classification Layer Routes:
POST /tax-engine/classify/{ledger_id}  → Classify a single ledger entry
POST /tax-engine/backfill              → Classify all unclassified entries for user
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Optional
from pydantic import BaseModel

from app.models.tax_engine import (
    TaxCalculationRequest, Modelo303Response, Modelo130Response,
    TaxReportUpdateStatus, GenericCalcRequest,
    Modelo115Response, Modelo111Response,
    Modelo390Response, Modelo190Response,
)
from app.services.tax_engine_service import TaxEngineService
from app.services.tax_classification_service import TaxClassificationService
from app.routes.auth import get_current_user

router = APIRouter(prefix="/tax-engine", tags=["Tax Engine"])

_engine     = TaxEngineService()
_classifier = TaxClassificationService()


# ── Request models ────────────────────────────────────────────────────────────

class TaxCalcRequest(BaseModel):
    year: int
    quarter: str                    # "Q1" | "Q2" | "Q3" | "Q4"
    modelo_id: Optional[str] = None # MongoDB _id — if omitted, all classified entries are used


class AnnualCalcRequest(BaseModel):
    year: int
    modelo_id: Optional[str] = None  # if omitted, all classified entries for the year are used


# ── Calculation endpoints ─────────────────────────────────────────────────────

@router.post("/303/calculate", response_model=Modelo303Response)
async def calculate_modelo_303(
    body: TaxCalcRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Calculate Modelo 303 (IVA) for the given year, quarter, and modelo_id.
    Only processes entries pre-classified as belonging to this modelo_id.
    """
    from app.models.tax_engine import Quarter
    user_id = str(current_user["_id"])
    org_id  = str(current_user.get("organization_id", user_id))
    try:
        return _engine.calculate_modelo_303(
            user_id, org_id, body.year, Quarter(body.quarter), body.modelo_id
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/130/calculate", response_model=Modelo130Response)
async def calculate_modelo_130(
    body: TaxCalcRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Calculate Modelo 130 (IRPF) for the given year, quarter, and modelo_id.
    Only processes entries pre-classified as belonging to this modelo_id.
    """
    from app.models.tax_engine import Quarter
    user_id = str(current_user["_id"])
    org_id  = str(current_user.get("organization_id", user_id))
    try:
        return _engine.calculate_modelo_130(
            user_id, org_id, body.year, Quarter(body.quarter), body.modelo_id
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Report management ─────────────────────────────────────────────────────────

@router.get("/reports")
async def list_tax_reports(
    modelo: Optional[str] = Query(None, description="Filter by modelo number e.g. '303'"),
    current_user: dict = Depends(get_current_user),
):
    """List all saved tax reports for the current user."""
    return _engine.list_reports(str(current_user["_id"]), modelo)


@router.patch("/reports/{report_id}/status")
async def update_report_status(
    report_id: str,
    body: TaxReportUpdateStatus,
    current_user: dict = Depends(get_current_user),
):
    """Advance a report status: draft → finalized → filed."""
    updated = _engine.update_status(report_id, body.status)
    if not updated:
        raise HTTPException(status_code=404, detail="Report not found or no change.")
    return {"message": f"Status updated to '{body.status}'."}


# ── Additional quarterly modelos ─────────────────────────────────────────────

@router.post("/115/calculate", response_model=Modelo115Response)
async def calculate_modelo_115(
    body: TaxCalcRequest,
    current_user: dict = Depends(get_current_user),
):
    """Modelo 115 — Rent IRPF withholding (retenciones alquileres)."""
    from app.models.tax_engine import Quarter
    user_id = str(current_user["_id"])
    org_id  = str(current_user.get("organization_id", user_id))
    try:
        return _engine.calculate_modelo_115(
            user_id, org_id, body.year, Quarter(body.quarter), body.modelo_id
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/111/calculate", response_model=Modelo111Response)
async def calculate_modelo_111(
    body: TaxCalcRequest,
    current_user: dict = Depends(get_current_user),
):
    """Modelo 111 — Employee / professional IRPF withholding."""
    from app.models.tax_engine import Quarter
    user_id = str(current_user["_id"])
    org_id  = str(current_user.get("organization_id", user_id))
    try:
        return _engine.calculate_modelo_111(
            user_id, org_id, body.year, Quarter(body.quarter), body.modelo_id
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Annual modelos ────────────────────────────────────────────────────────────

@router.post("/390/calculate", response_model=Modelo390Response)
async def calculate_modelo_390(
    body: AnnualCalcRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Modelo 390 — Annual VAT summary.
    Aggregates all 4 quarters and deducts quarterly 303 payments already made.
    """
    user_id = str(current_user["_id"])
    org_id  = str(current_user.get("organization_id", user_id))
    try:
        return _engine.calculate_modelo_390(user_id, org_id, body.year, body.modelo_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/190/calculate", response_model=Modelo190Response)
async def calculate_modelo_190(
    body: AnnualCalcRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Modelo 190 — Annual IRPF summary.
    Aggregates all 4 quarters and deducts quarterly 130 payments + withheld retentions.
    """
    user_id = str(current_user["_id"])
    org_id  = str(current_user.get("organization_id", user_id))
    try:
        return _engine.calculate_modelo_190(user_id, org_id, body.year, body.modelo_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Classification Layer endpoints ────────────────────────────────────────────

@router.post("/classify/{ledger_id}")
async def classify_ledger_entry(
    ledger_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Manually trigger classification for a single ledger entry.
    Useful for re-classifying after census data is updated.
    """
    user_id = str(current_user["_id"])
    try:
        result = _classifier.classify_ledger_entry(ledger_id, user_id)
        return {
            "ledger_id":       ledger_id,
            "modelo_ids":      result["modelo_ids"],
            "matched_modelos": result["matched_modelos"],
            "signals":         result["signals"],
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/backfill")
async def backfill_classification(
    current_user: dict = Depends(get_current_user),
):
    """
    Classify all unclassified ledger entries for the current user.
    Run this once after deploying the classification layer to enrich
    existing ledger documents.
    """
    user_id = str(current_user["_id"])
    try:
        stats = _classifier.backfill_user(user_id)
        return {"message": "Backfill complete", "stats": stats}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
