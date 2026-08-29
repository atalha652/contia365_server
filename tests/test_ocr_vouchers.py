"""OCR start rules: Expenses can run OCR on pending uploads; manuals never run."""

import unittest

from app.services.ocr_vouchers import (
    build_voucher_ocr_completion_update,
    ocr_ledger_match,
    select_runnable_ocr_vouchers,
    should_auto_approve_after_ocr,
    voucher_ocr_status,
)


class SelectRunnableOcrVouchersTests(unittest.TestCase):
    def test_pending_upload_is_runnable(self):
        split = select_runnable_ocr_vouchers(
            [{"_id": "1", "source": "upload", "OCR": "pending", "status": "pending"}]
        )
        self.assertEqual(len(split["runnable"]), 1)
        self.assertEqual(split["skipped_manual"], [])
        self.assertEqual(split["skipped_processing"], [])

    def test_failed_and_partial_can_rerun(self):
        split = select_runnable_ocr_vouchers(
            [
                {"OCR": "failed", "source": "upload"},
                {"OCR": "partial", "source": "upload"},
                {"OCR": "done", "source": "upload"},
            ]
        )
        self.assertEqual(len(split["runnable"]), 3)

    def test_manual_expense_is_skipped(self):
        split = select_runnable_ocr_vouchers(
            [{"source": "manual", "OCR": "not_applicable", "status": "approved"}]
        )
        self.assertEqual(split["runnable"], [])
        self.assertEqual(len(split["skipped_manual"]), 1)

    def test_processing_is_skipped(self):
        split = select_runnable_ocr_vouchers(
            [{"OCR": "processing", "source": "upload"}]
        )
        self.assertEqual(split["runnable"], [])
        self.assertEqual(len(split["skipped_processing"]), 1)

    def test_mixed_batch_skips_only_blocked(self):
        split = select_runnable_ocr_vouchers(
            [
                {"_id": "ok", "OCR": "pending"},
                {"_id": "typed", "source": "manual", "OCR": "not_applicable"},
                {"_id": "busy", "OCR": "processing"},
            ]
        )
        self.assertEqual([d["_id"] for d in split["runnable"]], ["ok"])
        self.assertEqual(len(split["skipped_manual"]), 1)
        self.assertEqual(len(split["skipped_processing"]), 1)


class OcrLedgerMatchTests(unittest.TestCase):
    def test_file_match_uses_s3_key(self):
        self.assertEqual(
            ocr_ledger_match("v1", "file", s3_key="user/v1/bill.pdf"),
            {"voucher_id": "v1", "data_type": "file", "s3_key": "user/v1/bill.pdf"},
        )

    def test_toon_match_uses_file_name(self):
        self.assertEqual(
            ocr_ledger_match("v1", "toon", file_name="email.toon"),
            {"voucher_id": "v1", "data_type": "toon", "file_name": "email.toon"},
        )


class VoucherOcrStatusTests(unittest.TestCase):
    def test_normalizes_blank(self):
        self.assertEqual(voucher_ocr_status({}), "")
        self.assertEqual(voucher_ocr_status({"OCR": " Processing "}), "processing")


class AutoApproveAfterOcrTests(unittest.TestCase):
    def test_done_and_partial_auto_approve(self):
        self.assertTrue(should_auto_approve_after_ocr("done"))
        self.assertTrue(should_auto_approve_after_ocr("partial"))

    def test_failed_does_not_auto_approve(self):
        self.assertFalse(should_auto_approve_after_ocr("failed"))

    def test_completion_update_auto_approves_pending(self):
        update = build_voucher_ocr_completion_update("user-1", "done", "pending")
        self.assertEqual(update["status"], "approved")
        self.assertEqual(update["approved_by"], "user-1")
        self.assertEqual(update["OCR"], "done")

    def test_completion_update_skips_already_approved(self):
        update = build_voucher_ocr_completion_update("user-1", "done", "approved")
        self.assertNotIn("status", update)

    def test_completion_update_failed_stays_pending(self):
        update = build_voucher_ocr_completion_update("user-1", "failed", "pending")
        self.assertNotIn("status", update)
        self.assertEqual(update["OCR"], "failed")


if __name__ == "__main__":
    unittest.main()
