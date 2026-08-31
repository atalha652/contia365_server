"""First-class tax filing workflow routes."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from app.models.tax_filing import (
    TaxFilingActionRequest,
    TaxFilingCalculateRequest,
    TaxFilingCreate,
    TaxFilingResultRequest,
    TaxFilingSubmitRequest,
)
from app.routes.auth import get_current_user
from app.routes.spain_tax_dep import require_spanish_tax
from app.services.aeat_modelo_client import AeatModeloClientError
from app.services.spain_tax_access import ItalyTaxUnavailableError
from app.services.tax_filing_service import (
    FilingConflictError,
    FilingForbiddenError,
    TaxFilingService,
    serialize_filing,
)


router = APIRouter(
    prefix="/tax-filings",
    tags=["Tax Filings"],
    dependencies=[Depends(require_spanish_tax)],
)
service = TaxFilingService()


def _run(action):
    try:
        result = action()
        if isinstance(result, list):
            return [serialize_filing(item) for item in result]
        return serialize_filing(result)
    except ItalyTaxUnavailableError as exc:
        raise HTTPException(status_code=403, detail=exc.detail)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except FilingForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except FilingConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.as_detail())
    except AeatModeloClientError as exc:
        status = 400 if exc.code in {"CERT", "CONFIG"} else 502
        raise HTTPException(
            status_code=status,
            detail={
                "error": "AEAT_MODELO",
                "code": exc.code,
                "description": exc.description,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/", status_code=201)
async def create_tax_filing(
    body: TaxFilingCreate,
    current_user: dict = Depends(get_current_user),
):
    """Create one DRAFT filing for an applicable modelo and fiscal period."""
    return _run(
        lambda: service.create(
            current_user, body.modelo, body.year, body.quarter,
            month=body.month, period_key=body.period_key,
        )
    )


@router.get("/")
async def list_tax_filings(
    status: Optional[str] = Query(None),
    year: Optional[int] = Query(None),
    modelo: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
):
    return _run(
        lambda: service.list(current_user, status, year, modelo)
    )


@router.get("/{filing_id}")
async def get_tax_filing(
    filing_id: str,
    current_user: dict = Depends(get_current_user),
):
    return _run(lambda: service.get(filing_id, current_user))


@router.post("/{filing_id}/calculate")
async def calculate_tax_filing(
    filing_id: str,
    body: TaxFilingCalculateRequest,
    current_user: dict = Depends(get_current_user),
):
    """DRAFT/REJECTED -> CALCULATED using the existing tax engine."""
    return _run(
        lambda: service.calculate(
            filing_id, current_user, body.modelo_id, body.comment
        )
    )


@router.post("/{filing_id}/review")
async def review_tax_filing(
    filing_id: str,
    body: TaxFilingActionRequest,
    current_user: dict = Depends(get_current_user),
):
    """CALCULATED -> IN_REVIEW and store reviewer/timestamp/comment."""
    return _run(
        lambda: service.start_review(filing_id, current_user, body.comment)
    )


@router.post("/{filing_id}/approve")
async def approve_tax_filing(
    filing_id: str,
    body: TaxFilingActionRequest,
    current_user: dict = Depends(get_current_user),
):
    """IN_REVIEW -> APPROVED and store approver/timestamp/comment."""
    return _run(
        lambda: service.approve(filing_id, current_user, body.comment)
    )


@router.post("/{filing_id}/submit")
async def submit_tax_filing(
    filing_id: str,
    body: TaxFilingSubmitRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    APPROVED -> SUBMITTED (test_mode) or ACCEPTED/REJECTED (live).

    Live submit builds the official modelo file (T5) and posts it via
    aeat_modelo_client using Contia365's own certificate as authorized representative.
    It never uses the VeriFactu invoice endpoint.
    111 and 190 require percipient records.
    """
    return _run(
        lambda: service.submit(
            filing_id,
            current_user,
            body.comment,
            body.test_mode,
            cert_password=body.cert_password,
        )
    )



@router.get("/{filing_id}/justificante")
def download_tax_filing_justificante(
    filing_id: str,
    current_user: dict = Depends(get_current_user),
):
    """PDF receipt from stored AEAT fields (code, message, CSV, justificante)."""
    try:
        pdf_bytes, filename = service.justificante_pdf(filing_id, current_user)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{filing_id}/result")
async def record_tax_filing_result(
    filing_id: str,
    body: TaxFilingResultRequest,
    current_user: dict = Depends(get_current_user),
):
    """SUBMITTED -> ACCEPTED/REJECTED for test-mode filings of modelos without live AEAT."""
    result = body.model_dump(exclude={"accepted", "comment"})
    return _run(
        lambda: service.record_result(
            filing_id,
            current_user,
            body.accepted,
            result,
            body.comment,
        )
    )

