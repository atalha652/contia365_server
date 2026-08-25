"""T11: Italian users cannot use Spanish tax / 303 APIs."""

import unittest

from app.models.onboarding import COUNTRY_CONFIGS, CountrySelection
from app.services.spain_tax_access import (
    ItalyTaxUnavailableError,
    assert_spanish_tax_allowed,
    is_italy_user,
)
from app.services.tax_filing_service import TaxFilingService
from tests.test_tax_filing_submit import _service, _user


class SpainTaxAccessTests(unittest.TestCase):
    def test_italy_is_coming_soon_in_country_config(self):
        italy = COUNTRY_CONFIGS[CountrySelection.ITALY]
        self.assertFalse(italy["tax_available"])
        self.assertEqual(italy["status"], "Coming soon")
        spain = COUNTRY_CONFIGS[CountrySelection.SPAIN]
        self.assertTrue(spain["tax_available"])

    def test_assert_blocks_italy(self):
        with self.assertRaises(ItalyTaxUnavailableError) as ctx:
            assert_spanish_tax_allowed({"country": "IT"})
        self.assertEqual(ctx.exception.detail["error"], "ITALY_TAX_UNAVAILABLE")

    def test_assert_allows_spain(self):
        assert_spanish_tax_allowed({"country": "ES"})
        assert_spanish_tax_allowed({"country": ""})
        self.assertFalse(is_italy_user({"country": "ES"}))

    def test_create_filing_blocked_for_italy(self):
        svc, _repo = _service({"_id": "x", "user_id": "user-1", "modelo": "303"})
        svc._profile = lambda _user: {
            "_id": "profile-1",
            "periodic_tax_obligations": [{"modelo": "303"}],
        }
        with self.assertRaises(ItalyTaxUnavailableError):
            svc.create(_user(country="IT"), "303", 2026, "Q2")


if __name__ == "__main__":
    unittest.main()
