"""Helpers for the Ledgers list: bank posts stay, issued-invoice posts do not."""

from datetime import datetime
from typing import Any, Dict


def is_invoice_issue_ledger_entry(entry: Dict[str, Any]) -> bool:
    """Issued invoices post to ledger_entries with invoice_id; those are not Ledgers rows."""
    return bool(entry.get("invoice_id"))


def format_bank_ledger_for_display(entry: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """Map a bank posting from ledger_entries onto the OCR ledger shape used by the UI."""
    transaction_date = entry.get("transaction_date")
    created_at = entry.get("created_at")
    amount = entry.get("amount", 0)
    return {
        "_id": str(entry["_id"]),
        "user_id": user_id,
        "voucher_id": entry.get("journal_entry_id", ""),
        "file_name": f"Bank Transaction - {entry.get('reference', 'N/A')}",
        "data_type": "bank_transaction",
        "ocr_text": entry.get("description", ""),
        "invoice_data": {
            "transaction_type": "income" if str(entry.get("entry_type", "")).lower() in ("debit", "income") else "expense",
            "account": {
                "account_code": entry.get("account_code", ""),
                "account_name": entry.get("account_name", "")
            },
            "invoice": {
                "invoice_number": entry.get("reference", ""),
                "invoice_date": transaction_date.strftime("%Y-%m-%d") if isinstance(transaction_date, datetime) else str(transaction_date or ""),
                "due_date": "",
                "amount_in_words": ""
            },
            "items": [
                {
                    "description": entry.get("description", ""),
                    "qty": 1,
                    "unit_price": amount,
                    "subtotal": amount
                }
            ],
            "totals": {
                "total": amount,
                "running_balance": entry.get("running_balance", 0)
            }
        },
        "llm_error": None,
        "processing_status": "success",
        "created_at": created_at.strftime("%Y-%m-%d %H:%M:%S") if isinstance(created_at, datetime) else str(created_at or "")
    }
