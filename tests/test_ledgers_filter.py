"""Ledgers list excludes issued-invoice postings from ledger_entries."""

import unittest

from app.utils.ledger_display import format_bank_ledger_for_display, is_invoice_issue_ledger_entry


class LedgersFilterTests(unittest.TestCase):
    def test_invoice_issue_row_is_excluded(self):
        self.assertTrue(
            is_invoice_issue_ledger_entry({
                "invoice_id": "inv-1",
                "invoice_number": "A-000017",
                "description": "Invoice A-000017 — Muhammad Furqan",
            })
        )

    def test_bank_posting_is_kept(self):
        self.assertFalse(
            is_invoice_issue_ledger_entry({
                "journal_code": "BANK",
                "reference": "TRX-1",
                "amount": 50,
                "description": "ATM withdrawal",
            })
        )

    def test_bank_formatter_keeps_reference_and_amount(self):
        formatted = format_bank_ledger_for_display(
            {
                "_id": "abc123",
                "journal_entry_id": "je-1",
                "reference": "TRX-1",
                "description": "ATM withdrawal",
                "entry_type": "DEBIT",
                "account_code": "5720",
                "account_name": "Banks",
                "amount": 50,
                "running_balance": 100,
                "transaction_date": "2026-08-29",
                "created_at": "2026-08-29 07:02:41",
            },
            "user-1",
        )
        self.assertEqual(formatted["data_type"], "bank_transaction")
        self.assertEqual(formatted["invoice_data"]["invoice"]["invoice_number"], "TRX-1")
        self.assertEqual(formatted["invoice_data"]["totals"]["total"], 50)
