"""Helpers for starting voucher OCR from the Expenses page."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from app.services.manual_voucher import is_manual_voucher


def voucher_ocr_status(doc: Optional[Dict[str, Any]]) -> str:
    if not doc:
        return ""
    return str(doc.get("OCR") or "").strip().lower()


def should_auto_approve_after_ocr(ocr_status: str) -> bool:
    """Scans auto-approve when OCR extracted usable data (done or partial)."""
    return str(ocr_status or "").lower() in ("done", "partial")


def build_voucher_ocr_completion_update(
    user_id: str,
    ocr_status: str,
    current_status: Optional[str],
) -> dict:
    """Mongo $set payload after OCR finishes — includes auto-approve when eligible."""
    now = datetime.utcnow()
    update: Dict[str, Any] = {
        "OCR": ocr_status,
        "ocr_completed_at": now,
        "updated_at": now,
    }
    if should_auto_approve_after_ocr(ocr_status) and str(current_status or "").lower() != "approved":
        update.update(
            {
                "status": "approved",
                "approved_by": user_id,
                "approved_at": now,
                "approval_notes": "Auto-approved after OCR",
            }
        )
    return update


def select_runnable_ocr_vouchers(docs: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Split a voucher list into items that can start OCR vs items to skip.
    Manual / typed expenses never run OCR. A voucher already in
    ``processing`` is skipped so a second click does not double-scan.
    Approval status is not required — pending uploads on Expenses are valid.
    """
    runnable: List[Dict[str, Any]] = []
    skipped_manual: List[Dict[str, Any]] = []
    skipped_processing: List[Dict[str, Any]] = []
    for doc in docs or []:
        if is_manual_voucher(doc):
            skipped_manual.append(doc)
        elif voucher_ocr_status(doc) == "processing":
            skipped_processing.append(doc)
        else:
            runnable.append(doc)
    return {
        "runnable": runnable,
        "skipped_manual": skipped_manual,
        "skipped_processing": skipped_processing,
    }


def ocr_ledger_match(
    voucher_id: str,
    data_type: str,
    s3_key: Optional[str] = None,
    file_name: Optional[str] = None,
) -> Dict[str, str]:
    """Find the existing OCR ledger row for the same voucher file."""
    match: Dict[str, str] = {"voucher_id": str(voucher_id), "data_type": data_type}
    if data_type == "file" and s3_key:
        match["s3_key"] = s3_key
    elif data_type == "toon" and file_name:
        match["file_name"] = file_name
    return match
