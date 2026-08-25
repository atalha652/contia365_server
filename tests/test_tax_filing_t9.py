"""T9: one filing per period (409) and lock totals after APPROVED."""

import unittest

from app.models.tax_filing import TaxFilingStatus
from app.services.tax_filing_service import FilingConflictError
from tests.test_tax_filing_submit import _approved_filing, _service, _user


class TaxFilingGuardTests(unittest.TestCase):
    def test_duplicate_create_is_conflict(self):
        existing = _approved_filing()
        svc, _repo = _service(existing)
        svc._profile = lambda _user: {
            "_id": "profile-1",
            "periodic_tax_obligations": [{"modelo": "303"}],
        }
        with self.assertRaises(FilingConflictError) as ctx:
            svc.create(_user(), "303", 2026, "Q2")
        detail = ctx.exception.as_detail()
        self.assertEqual(detail["error"], "FILING_EXISTS")
        self.assertEqual(detail["filing_id"], str(existing["_id"]))
        self.assertEqual(detail["period_key"], "Q2")

    def test_second_submit_is_conflict_with_reference(self):
        filing = _approved_filing(
            status=TaxFilingStatus.SUBMITTED.value,
            submission={"mode": "test", "reference": "TEST-ABC123"},
        )
        svc, repo = _service(filing)
        with self.assertRaises(FilingConflictError) as ctx:
            svc.submit("507f1f77bcf86cd799439011", _user(), None, True)
        self.assertIn("TEST-ABC123", ctx.exception.as_detail()["reference"])
        self.assertEqual(repo.filing["status"], TaxFilingStatus.SUBMITTED.value)

    def test_accepted_live_submit_is_conflict(self):
        filing = _approved_filing(
            status=TaxFilingStatus.ACCEPTED.value,
            submission={"mode": "live", "reference": "CSVTEST123"},
            aeat_result={"code": "0", "csv": "CSVTEST123", "justificante": "JUS-1"},
        )
        svc, _repo = _service(filing)
        with self.assertRaises(FilingConflictError) as ctx:
            svc.submit(
                "507f1f77bcf86cd799439011", _user(), None, False, "secret"
            )
        self.assertEqual(ctx.exception.as_detail()["reference"], "CSVTEST123")

    def test_calculate_refused_when_approved(self):
        svc, repo = _service(_approved_filing(totals_locked=True))
        with self.assertRaises(ValueError) as ctx:
            svc.calculate("507f1f77bcf86cd799439011", _user(), None, None)
        self.assertIn("DRAFT or REJECTED", str(ctx.exception))
        self.assertEqual(repo.filing["calculation"]["totals"]["vat_payable"], 210.0)

    def test_approve_locks_totals(self):
        filing = _approved_filing(status=TaxFilingStatus.IN_REVIEW.value)
        svc, _repo = _service(filing)
        updated = svc.approve("507f1f77bcf86cd799439011", _user(), None)
        self.assertTrue(updated["totals_locked"])
        self.assertEqual(updated["status"], TaxFilingStatus.APPROVED.value)


if __name__ == "__main__":
    unittest.main()
