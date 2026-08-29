"""Manual voucher path: approved on save, never an OCR/Execution item."""

import unittest

from app.services.manual_voucher import (
    ManualLineItem,
    ManualParty,
    ManualVoucherCreate,
    build_manual_invoice_data,
    execution_list_query,
    expenses_list_query,
    is_manual_voucher,
)


class ManualVoucherPathTests(unittest.TestCase):
    def test_typed_voucher_is_manual(self):
        self.assertTrue(is_manual_voucher({"source": "manual", "OCR": "not_applicable"}))
        self.assertTrue(is_manual_voucher({"OCR": "not_applicable"}))
        self.assertFalse(is_manual_voucher({"source": "upload", "OCR": "pending"}))
        self.assertFalse(is_manual_voucher({"OCR": "done"}))

    def test_execution_query_excludes_manuals(self):
        query = execution_list_query("user-1")
        self.assertEqual(query["status"], "approved")
        self.assertEqual(query["source"], {"$ne": "manual"})
        self.assertEqual(query["OCR"], {"$ne": "not_applicable"})

    def test_expenses_query_keeps_approved_manuals(self):
        query = expenses_list_query("user-1")
        self.assertIn({"source": "manual", "status": "approved"}, query["$or"])

    def test_expenses_query_includes_awaiting_approval_scans(self):
        query = expenses_list_query("user-1")
        scan_open = next(
            o for o in query["$or"]
            if o.get("status") == {"$in": ["pending", "rejected", "awaiting_approval"]}
        )
        self.assertEqual(scan_open["source"], {"$ne": "manual"})
        self.assertEqual(scan_open["OCR"], {"$ne": "not_applicable"})

    def test_expenses_query_keeps_approved_scans_until_invoiced(self):
        query = expenses_list_query("user-1")
        approved_scans = next(o for o in query["$or"] if o.get("status") == "approved" and "source" in o)
        self.assertEqual(approved_scans["source"], {"$ne": "manual"})
        self.assertIn({"invoice_id": {"$exists": False}}, approved_scans["$or"])
        self.assertIn({"invoice_id": None}, approved_scans["$or"])

    def test_typed_payload_builds_invoice_data(self):
        body = ManualVoucherCreate(
            user_id="u1",
            period="2026-T2",
            transaction_type="debit",
            supplier=ManualParty(name="Acme", nif="B12345678"),
            items=[ManualLineItem(description="Paper", qty=1, unit_price=10, vat_percent=21)],
        )
        data = build_manual_invoice_data(body)
        self.assertEqual(data["transaction_type"], "expense")
        self.assertEqual(data["totals"]["base"], 10)
        self.assertEqual(data["totals"]["VAT_amount"], 2.1)


if __name__ == "__main__":
    unittest.main()
