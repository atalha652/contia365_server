"""T10: only the filing owner may review, approve, or submit."""

import unittest

from app.models.tax_filing import TaxFilingStatus
from app.services.tax_filing_service import FilingForbiddenError
from tests.test_tax_filing_submit import _approved_filing, _service, _user


class TaxFilingOwnerTests(unittest.TestCase):
    def test_owner_can_approve(self):
        svc, _repo = _service(_approved_filing(status=TaxFilingStatus.IN_REVIEW.value))
        updated = svc.approve("507f1f77bcf86cd799439011", _user(), None)
        self.assertEqual(updated["status"], TaxFilingStatus.APPROVED.value)

    def test_non_owner_approve_is_forbidden(self):
        svc, repo = _service(_approved_filing(status=TaxFilingStatus.IN_REVIEW.value))
        with self.assertRaises(FilingForbiddenError):
            svc.approve("507f1f77bcf86cd799439011", _user(_id="other-user"), None)
        self.assertEqual(repo.filing["status"], TaxFilingStatus.IN_REVIEW.value)

    def test_non_owner_submit_is_forbidden(self):
        svc, repo = _service(_approved_filing())
        with self.assertRaises(FilingForbiddenError):
            svc.submit(
                "507f1f77bcf86cd799439011",
                _user(_id="other-user"),
                None,
                True,
            )
        self.assertEqual(repo.filing["status"], TaxFilingStatus.APPROVED.value)

    def test_non_owner_review_is_forbidden(self):
        svc, repo = _service(_approved_filing(status=TaxFilingStatus.CALCULATED.value))
        with self.assertRaises(FilingForbiddenError):
            svc.start_review(
                "507f1f77bcf86cd799439011", _user(_id="other-user"), None
            )
        self.assertEqual(repo.filing["status"], TaxFilingStatus.CALCULATED.value)

    def test_non_owner_get_is_not_found(self):
        svc, _repo = _service(_approved_filing())
        with self.assertRaises(LookupError):
            svc.get("507f1f77bcf86cd799439011", _user(_id="other-user"))


if __name__ == "__main__":
    unittest.main()
