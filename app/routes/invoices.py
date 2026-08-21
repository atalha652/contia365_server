"""
Invoice Routes
REST endpoints for the Invoice domain.

Endpoints:
  POST   /invoices/from-voucher/{voucher_id}  — create draft invoice from approved voucher
  POST   /invoices/{id}/issue                 — legal issuance (assigns number, locks, posts ledger)
  GET    /invoices/{id}                       — retrieve single invoice
  GET    /invoices                            — list invoices for org
  POST   /invoices/{id}/cancel               — cancel an issued invoice
  GET    /invoices/{id}/qr                   — VeriFactu QR legal string
  GET    /invoices/{id}/facturae             — Facturae 3.2.2 XML download
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from typing import List, Optional
from pydantic import BaseModel
import logging

from app.models.invoice import Invoice, InvoiceUpdate, InvoiceIssueResponse, InvoiceStatus
from app.services.invoice_service import InvoiceService
from app.services.facturae_service import FacturaeService, SellerInfo, VeriFactuService
from app.services.signature_service import SignatureService, encrypt_p12, decrypt_p12
from app.services.aeat_client import AeatClient, AeatSubmissionError
from app.routes.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/invoices", tags=["Invoices"])


# -------------------- Dependency --------------------

def get_invoice_service() -> InvoiceService:
    return InvoiceService()


def _org_id(current_user: dict) -> str:
    """
    Resolve the organization scope for the authenticated user.
    The voucher collection is scoped by user_id, so we use the user's _id
    as the org key when no explicit organization_id is set.
    """
    org = current_user.get("organization_info") or {}
    return org.get("organization_id") or str(current_user["_id"])


# -------------------- Request bodies --------------------

class CancelRequest(BaseModel):
    reason: str


class SubmitRequest(BaseModel):
    cert_password: str  # .p12 password — used in-memory only, never stored


# ==================== ENDPOINTS ====================

@router.get(
    "/verify-chain",
    summary="Verify the VeriFactu hash chain integrity for this organization",
)
def verify_chain(
    current_user: dict = Depends(get_current_user),
    service: InvoiceService = Depends(get_invoice_service),
):
    """
    Walks all issued invoices in chronological order and recomputes each
    SHA-256 fingerprint. Reports any broken links — indicating a deleted
    or tampered invoice. Returns { valid, total, errors, message }.
    """
    organization_id = _org_id(current_user)
    return service.verify_chain(organization_id)


@router.post(
    "/from-voucher/{voucher_id}",
    response_model=Invoice,
    status_code=201,
    summary="Create draft invoice from an approved voucher",
)
def create_from_voucher(
    voucher_id: str,
    current_user: dict = Depends(get_current_user),
    service: InvoiceService = Depends(get_invoice_service),
):
    """
    Generates a DRAFT invoice from an approved voucher.
    Extracts structured financial data (customer, lines, totals) from the voucher.
    Returns 409 if an invoice already exists for this voucher.
    """
    organization_id = _org_id(current_user)
    try:
        return service.create_from_voucher(organization_id, voucher_id)
    except ValueError as e:
        msg = str(e)
        status_code = 409 if "already exists" in msg else 400
        raise HTTPException(status_code=status_code, detail=msg)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")


@router.post(
    "/{invoice_id}/refresh-ocr",
    response_model=Invoice,
    summary="Re-apply OCR data to an existing draft invoice",
)
def refresh_ocr(
    invoice_id: str,
    current_user: dict = Depends(get_current_user),
    service: InvoiceService = Depends(get_invoice_service),
):
    """
    Re-runs the OCR lookup for the voucher linked to this draft and overwrites
    customer, lines, and totals with the latest extracted data.
    Use this when the invoice was created before OCR finished processing.
    Returns 400 if the invoice is not in DRAFT status or OCR data is not available.
    """
    organization_id = _org_id(current_user)
    try:
        return service.refresh_ocr(organization_id, invoice_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch(
    "/{invoice_id}",
    response_model=Invoice,
    summary="Update customer info and line items on a draft invoice",
)
def update_invoice(
    invoice_id: str,
    body: InvoiceUpdate,
    current_user: dict = Depends(get_current_user),
    service: InvoiceService = Depends(get_invoice_service),
):
    """
    Save edits to a DRAFT invoice before issuance.
    Accepts any combination of: series, customer, lines.
    Line totals are recalculated server-side — frontend computed values are ignored.
    Returns 400 if the invoice is not in DRAFT status.
    """
    organization_id = _org_id(current_user)
    try:
        return service.update(organization_id, invoice_id, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post(
    "/{invoice_id}/issue",
    response_model=InvoiceIssueResponse,
    summary="Issue an invoice (legal action — assigns number, locks, posts ledger)",
)
def issue_invoice(
    invoice_id: str,
    current_user: dict = Depends(get_current_user),
    service: InvoiceService = Depends(get_invoice_service),
):
    """
    Critical legal action:
    - Validates invoice is in DRAFT state
    - Recalculates all totals server-side (frontend values are ignored)
    - Assigns a sequential, gap-free invoice number (e.g. A-000001)
    - Creates double-entry ledger record: DR 430 / CR 700 + CR 477
    - Locks the invoice as ISSUED (immutable)
    """
    organization_id = _org_id(current_user)
    try:
        return service.issue(organization_id, invoice_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")


@router.get(
    "/{invoice_id}",
    response_model=Invoice,
    summary="Get a single invoice",
)
def get_invoice(
    invoice_id: str,
    current_user: dict = Depends(get_current_user),
    service: InvoiceService = Depends(get_invoice_service),
):
    organization_id = _org_id(current_user)
    try:
        return service.get(organization_id, invoice_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get(
    "",
    response_model=List[Invoice],
    summary="List invoices for the current organization",
)
def list_invoices(
    status: Optional[InvoiceStatus] = Query(None, description="Filter by status"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user),
    service: InvoiceService = Depends(get_invoice_service),
):
    organization_id = _org_id(current_user)
    return service.list(organization_id, status, limit, offset)


@router.post(
    "/{invoice_id}/cancel",
    response_model=Invoice,
    summary="Cancel an issued invoice",
)
def cancel_invoice(
    invoice_id: str,
    body: CancelRequest,
    current_user: dict = Depends(get_current_user),
    service: InvoiceService = Depends(get_invoice_service),
):
    """
    Cancels an ISSUED invoice. The record is never deleted — status is set to
    'cancelled' with a reason and timestamp for full audit trail.
    """
    organization_id = _org_id(current_user)
    try:
        return service.cancel(organization_id, invoice_id, body.reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ==================== COMPLIANCE ENDPOINTS ====================

def _seller_from_user(current_user: dict) -> SellerInfo:
    """Build SellerInfo from the authenticated user's profile."""
    org = current_user.get("organization_info") or {}
    census = current_user.get("census_data") or {}
    return SellerInfo(
        tax_id=current_user.get("tax_id") or census.get("nif") or "UNKNOWN",
        name=(
            org.get("company_name")
            or census.get("razon_social")
            or current_user.get("name")
            or "Unknown"
        ),
        address=org.get("address") or census.get("domicilio_fiscal") or "",
        postal_code=census.get("codigo_postal") or "",
        city=census.get("municipio") or "",
        province=census.get("provincia") or "",
    )


@router.get(
    "/{invoice_id}/qr",
    summary="Get the VeriFactu QR legal string for an issued invoice",
)
def get_qr_string(
    invoice_id: str,
    current_user: dict = Depends(get_current_user),
    service: InvoiceService = Depends(get_invoice_service),
):
    """
    Returns the AEAT VeriFactu verification URL that should be encoded into
    the QR code printed on the invoice PDF.

    Format: https://prewww2.aeat.es/wlpl/TIKE-CONT/ValidarQR?nif=...&numserie=...&fecha=...&importe=...

    Only available for ISSUED invoices.
    """
    organization_id = _org_id(current_user)
    try:
        invoice = service.get(organization_id, invoice_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    if invoice.status != InvoiceStatus.ISSUED:
        raise HTTPException(status_code=400, detail="QR is only available for ISSUED invoices")

    seller = _seller_from_user(current_user)
    facturae_svc = FacturaeService(seller)
    try:
        qr_url = facturae_svc.build_qr_string(invoice)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"invoice_id": invoice_id, "qr_url": qr_url}


@router.get(
    "/{invoice_id}/facturae",
    summary="Download Facturae 3.2.2 XML for an issued invoice",
)
def download_facturae_xml(
    invoice_id: str,
    current_user: dict = Depends(get_current_user),
    service: InvoiceService = Depends(get_invoice_service),
):
    """
    Generates and returns a Facturae 3.2.2 compliant XML file.
    Required for B2B invoicing with Spanish public administrations (FACe)
    and large corporates that mandate electronic invoicing.

    Returns the XML as a file download (application/xml).
    Only available for ISSUED invoices.
    """
    organization_id = _org_id(current_user)
    try:
        invoice = service.get(organization_id, invoice_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    if invoice.status != InvoiceStatus.ISSUED:
        raise HTTPException(status_code=400, detail="Facturae XML is only available for ISSUED invoices")

    seller = _seller_from_user(current_user)
    facturae_svc = FacturaeService(seller)
    try:
        xml_bytes = facturae_svc.build_facturae_xml(invoice)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    filename = f"facturae_{invoice.invoice_number}.xml"
    return Response(
        content=xml_bytes,
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/{invoice_id}/submit-debug",
    summary="Preview the SOAP XML that would be sent to AEAT (no actual submission)",
)
def submit_debug(
    invoice_id: str,
    current_user: dict = Depends(get_current_user),
    service: InvoiceService = Depends(get_invoice_service),
):
    """
    Returns both the raw VeriFactu XML and the full SOAP envelope
    that would be submitted to AEAT. No certificate or signing required.
    """
    from app.services.aeat_client import NS_SLR, NS_SF, _SOAP_TEMPLATE

    organization_id = _org_id(current_user)
    try:
        invoice = service.get(organization_id, invoice_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    seller = _seller_from_user(current_user)
    verifactu_svc = VeriFactuService(seller)
    try:
        xml_bytes = verifactu_svc.build_registro_xml(invoice)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Show what _clean_xml produces inside the SOAP envelope
    from app.services.aeat_client import AeatClient, NS_SLR, NS_SF, _SOAP_TEMPLATE
    client = AeatClient()
    cleaned = client._clean_xml(xml_bytes.decode("utf-8"))

    soap = _SOAP_TEMPLATE.format(
        ns_slr=NS_SLR,
        ns_sf=NS_SF,
        seller_name=seller.name,
        seller_nif=seller.tax_id,
        registro_children=cleaned,
    )

    return Response(
        content=soap.encode("utf-8"),
        media_type="application/xml",
        headers={"Content-Disposition": f'inline; filename="soap_debug_{invoice_id}.xml"'},
    )


@router.post(
    "/{invoice_id}/submit",
    summary="Sign and submit invoice to AEAT VeriFactu (mTLS + XAdES-EPES)",
)
def submit_to_aeat(
    invoice_id: str,
    body: SubmitRequest,
    current_user: dict = Depends(get_current_user),
    service: InvoiceService = Depends(get_invoice_service),
):
    """
    Full VeriFactu submission pipeline:

    A) Generate Facturae 3.2.2 XML from the issued invoice.
    B) Sign the XML with XAdES-EPES using the user's stored .p12 certificate.
    C) Submit to AEAT via SOAP + mTLS.
    D) On AEAT code 0: update invoice status → SUBMITTED, store the CSV.

    The cert_password is used in-memory only — it is never logged or stored.
    Prerequisites: invoice must be ISSUED, user must have uploaded their .p12.
    """
    organization_id = _org_id(current_user)

    # ── Fetch invoice ─────────────────────────────────────────────────────────
    try:
        invoice = service.get(organization_id, invoice_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    if invoice.status != InvoiceStatus.ISSUED:
        raise HTTPException(
            status_code=400,
            detail=f"Only ISSUED invoices can be submitted. Current status: {invoice.status}",
        )

    # ── Load and decrypt .p12 from user document ──────────────────────────────
    p12_encrypted = current_user.get("p12_encrypted")
    if not p12_encrypted:
        raise HTTPException(
            status_code=422,
            detail=(
                "No digital certificate found. "
                "Upload your .p12 via POST /api/auth/certificate first."
            ),
        )

    try:
        if isinstance(p12_encrypted, str):
            p12_encrypted = p12_encrypted.encode()
        p12_bytes = decrypt_p12(p12_encrypted)
    except Exception as e:
        logger.error("Failed to decrypt .p12 for user %s: %s", current_user.get("_id"), e)
        raise HTTPException(status_code=500, detail="Failed to decrypt certificate")

    # Password comes from the request body — never from env or DB
    p12_password = body.cert_password

    # ── Step A: Generate VeriFactu RegistroFacturacion XML ───────────────────
    seller = _seller_from_user(current_user)
    verifactu_svc = VeriFactuService(seller)
    facturae_svc = FacturaeService(seller)
    try:
        xml_bytes = verifactu_svc.build_registro_xml(invoice)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # ── Step B: Submit to AEAT via SOAP + mTLS ────────────────────────────────
    # VeriFactu authentication = mTLS (certificate presented at TLS handshake).
    # The Huella SHA-256 chain provides tamper-evidence.
    # XAdES signing is NOT required by the VeriFactu SOAP endpoint —
    # it is only needed for Facturae B2B (FACe). Signing the payload here
    # breaks the XML structure and causes AEAT schema validation errors.
    aeat = AeatClient()
    try:
        aeat_response = aeat.submit(
            signed_xml_bytes=xml_bytes,   # unsigned VeriFactu XML
            p12_bytes=p12_bytes,
            p12_password=p12_password,
            seller_nif=seller.tax_id,
            seller_name=seller.name,
        )
    except AeatSubmissionError as e:
        logger.error("AEAT rejected invoice %s — code=%s: %s\nRaw: %s", invoice_id, e.code, e.description, e.raw[:1000])
        raise HTTPException(
            status_code=502,
            detail={
                "error": "AEAT_REJECTION",
                "code": e.code,
                "description": e.description,
                "aeat_raw": e.raw[:1000],
            },
        )
    except Exception as e:
        logger.error("AEAT submission error for invoice %s: %s", invoice_id, e)
        raise HTTPException(status_code=502, detail=f"AEAT submission failed: {e}")

    # ── Step C: Update invoice → SUBMITTED + store CSV ────────────────────────
    try:
        service.mark_submitted(organization_id, invoice_id, aeat_response.csv)
    except Exception as e:
        logger.error("DB update failed after AEAT acceptance for %s: %s", invoice_id, e)

    try:
        qr_url = facturae_svc.build_qr_string(invoice)
    except Exception:
        qr_url = None

    return {
        "invoice_id": invoice_id,
        "status": "submitted",
        "aeat_code": aeat_response.code,
        "aeat_description": aeat_response.description,
        "csv": aeat_response.csv,
        "qr_url": qr_url,
        "message": "Invoice successfully submitted to AEAT. QR verification is now active.",
    }
