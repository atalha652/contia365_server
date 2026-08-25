"""Employee / professional percipient records for Modelo 111 and 190."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.models.tax_percipient import PercipientCreate, PercipientUpdate
from app.routes.auth import get_current_user
from app.routes.spain_tax_dep import require_spanish_tax
from app.services.tax_percipient_service import TaxPercipientService


router = APIRouter(
    prefix="/tax-percipients",
    tags=["Tax Percipients"],
    dependencies=[Depends(require_spanish_tax)],
)
service = TaxPercipientService()


@router.post("/", status_code=201)
def create_percipient(
    body: PercipientCreate,
    current_user: dict = Depends(get_current_user),
):
    try:
        return service.create(current_user, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/")
def list_percipients(
    year: Optional[int] = Query(None),
    quarter: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
):
    try:
        return service.list(current_user, year, quarter)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/{row_id}")
def update_percipient(
    row_id: str,
    body: PercipientUpdate,
    current_user: dict = Depends(get_current_user),
):
    try:
        return service.update(row_id, current_user, body)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{row_id}", status_code=204)
def delete_percipient(
    row_id: str,
    current_user: dict = Depends(get_current_user),
):
    try:
        service.delete(row_id, current_user)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
