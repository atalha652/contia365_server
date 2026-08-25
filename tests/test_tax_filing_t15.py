"""T15: monthly 303 (REDEME) — profile periodicity, engine month window, filing."""

import unittest
from datetime import datetime
from unittest.mock import MagicMock

from app.models.tax_engine import Modelo303Response, TaxReportStatus
from app.models.tax_filing import TaxFilingStatus
from app.services.fiscal_profile_service import derive_periodic_tax_obligations
from app.services.tax_engine_service import TaxEngineService
from app.services.tax_period import resolve_tax_period
from tests.test_tax_filing_submit import (
    _approved_filing,
    _service,
    _user,
    _totals,
)


class TaxPeriodTests(unittest.TestCase):
    def test_monthly_key_and_march_range(self):
        period = resolve_tax_period(
            year=2026, periodicity="MENSUAL", month=3, modelo="303"
        )
        self.assertEqual(period.period_key, "2026-03")
        self.assertEqual(period.aeat_period, "03")
        self.assertEqual(period.report_quarter, "M03")
        self.assertTrue(period.is_redeme)
        start, end = period.date_range()
        self.assertEqual(start, datetime(2026, 3, 1))
        self.assertEqual(end, datetime(2026, 3, 31, 23, 59, 59))

    def test_monthly_rejects_quarter(self):
        with self.assertRaises(ValueError) as ctx:
            resolve_tax_period(
                year=2026, periodicity="MENSUAL", quarter="Q1", modelo="303"
            )
        self.assertIn("month", str(ctx.exception).lower())

    def test_quarterly_rejects_month(self):
        with self.assertRaises(ValueError) as ctx:
            resolve_tax_period(
                year=2026, periodicity="TRIMESTRAL", month=3, modelo="303"
            )
        self.assertIn("monthly", str(ctx.exception).lower())

    def test_period_key_2026_03(self):
        period = resolve_tax_period(
            year=2026, periodicity="MENSUAL", period_key="2026-03", modelo="303"
        )
        self.assertEqual(period.month, 3)
        self.assertEqual(period.aeat_period, "03")


class FiscalProfileRedemeTests(unittest.TestCase):
    def test_census_mensual_303_is_kept_and_marked_redeme(self):
        profile = {"professional_registration": {"vat_regime": "general"}}
        obligations = derive_periodic_tax_obligations(
            profile,
            [{"modelo": "303", "description": "IVA", "periodicity": "MENSUAL"}],
        )
        by_modelo = {item["modelo"]: item for item in obligations}
        self.assertEqual(by_modelo["303"]["periodicity"], "MENSUAL")
        self.assertTrue(by_modelo["303"]["redeme"])
        self.assertNotIn("390", by_modelo)

    def test_derived_303_stays_quarterly_and_adds_390(self):
        profile = {"professional_registration": {"vat_regime": "general"}}
        obligations = derive_periodic_tax_obligations(profile, [])
        by_modelo = {item["modelo"]: item for item in obligations}
        self.assertEqual(by_modelo["303"]["periodicity"], "TRIMESTRAL")
        self.assertFalse(by_modelo["303"].get("redeme"))
        self.assertEqual(by_modelo["390"]["periodicity"], "ANUAL")


class Monthly303EngineTests(unittest.TestCase):
    def test_calculate_uses_march_window(self):
        captured = {}

        class Repo:
            def get_applicable_modelos(self, _user_id):
                return {"303"}

            def get_modelo_periodicity(self, _user_id, _modelo):
                return "MENSUAL"

            def upsert_tax_report(self, report):
                captured["report"] = report
                return "rep-1"

        engine = TaxEngineService(repo=Repo(), percipient_repo=MagicMock())
        engine._get_entries_for_modelo = (
            lambda *_args: captured.update(
                start=_args[3], end=_args[4]
            ) or []
        )
        result = engine.calculate_modelo_303(
            "user-1", "org-1", 2026, month=3
        )
        self.assertIsInstance(result, Modelo303Response)
        self.assertEqual(result.month, 3)
        self.assertEqual(result.period_key, "2026-03")
        self.assertTrue(result.redeme)
        self.assertIsNone(result.quarter)
        self.assertEqual(captured["start"], datetime(2026, 3, 1))
        self.assertEqual(captured["end"], datetime(2026, 3, 31, 23, 59, 59))
        self.assertEqual(captured["report"].period_key, "2026-03")
        self.assertEqual(captured["report"].quarter, "M03")

    def test_monthly_profile_requires_month(self):
        class Repo:
            def get_applicable_modelos(self, _user_id):
                return {"303"}

            def get_modelo_periodicity(self, _user_id, _modelo):
                return "MENSUAL"

        engine = TaxEngineService(repo=Repo(), percipient_repo=MagicMock())
        with self.assertRaises(ValueError):
            engine.calculate_modelo_303("user-1", "org-1", 2026, quarter="Q1")


class Monthly303FilingTests(unittest.TestCase):
    def _monthly_profile(self):
        return {
            "_id": "profile-1",
            "periodic_tax_obligations": [{
                "modelo": "303",
                "periodicity": "MENSUAL",
                "redeme": True,
            }],
            "taxpayer_identity": {
                "nif_nie": "55238025Y",
                "full_name": "BROWN FERNANDEZ ROBERT GLASCO",
            },
        }

    def test_create_monthly_filing(self):
        svc, repo = _service(_approved_filing(period_key="Q2", quarter="Q2"))
        svc._profile = lambda _user: self._monthly_profile()
        created = svc.create(_user(), "303", 2026, None, month=3)
        self.assertEqual(created["period_key"], "2026-03")
        self.assertEqual(created["month"], 3)
        self.assertIsNone(created["quarter"])
        self.assertTrue(created["redeme"])
        self.assertEqual(created["aeat_period"], "03")
        self.assertEqual(created["periodicity"], "MENSUAL")
        self.assertEqual(created["status"], TaxFilingStatus.DRAFT.value)
        self.assertEqual(repo.filing["period_key"], "2026-03")

    def test_create_monthly_from_period_key(self):
        svc, _repo = _service(_approved_filing(period_key="Q2"))
        svc._profile = lambda _user: self._monthly_profile()
        created = svc.create(
            _user(), "303", 2026, None, period_key="2026-11"
        )
        self.assertEqual(created["month"], 11)
        self.assertEqual(created["period_key"], "2026-11")

    def test_monthly_create_rejects_quarter(self):
        svc, _repo = _service(_approved_filing())
        svc._profile = lambda _user: self._monthly_profile()
        with self.assertRaises(ValueError) as ctx:
            svc.create(_user(), "303", 2026, "Q1")
        self.assertIn("month", str(ctx.exception).lower())

    def test_quarterly_create_still_uses_q2(self):
        svc, repo = _service(_approved_filing(year=2025, period_key="Q1"))
        svc._profile = lambda _user: {
            "_id": "profile-1",
            "periodic_tax_obligations": [{"modelo": "303", "periodicity": "TRIMESTRAL"}],
        }
        created = svc.create(_user(), "303", 2026, "Q2")
        self.assertEqual(created["period_key"], "Q2")
        self.assertEqual(created["quarter"], "Q2")
        self.assertFalse(created["redeme"])
        self.assertEqual(repo.filing["period_key"], "Q2")

    def test_calculate_passes_month_to_engine(self):
        filing = _approved_filing(
            status=TaxFilingStatus.DRAFT.value,
            quarter=None,
            month=3,
            period_key="2026-03",
            redeme=True,
            calculation=None,
        )
        svc, repo = _service(filing)
        response = Modelo303Response(
            period="03 2026",
            year=2026,
            quarter=None,
            month=3,
            period_key="2026-03",
            redeme=True,
            totals=_totals(),
            status=TaxReportStatus.DRAFT,
            transactions_count=0,
            calculated_at="2026-03-01T00:00:00",
        )
        svc.engine.calculate_modelo_303.return_value = response
        updated = svc.calculate("507f1f77bcf86cd799439011", _user(), None, None)
        self.assertEqual(updated["status"], TaxFilingStatus.CALCULATED.value)
        kwargs = svc.engine.calculate_modelo_303.call_args
        self.assertEqual(kwargs.kwargs.get("month"), 3)
        self.assertEqual(kwargs.kwargs.get("period_key"), "2026-03")
        self.assertEqual(repo.filing["calculation"]["month"], 3)

    def test_live_submit_writes_monthly_redeme_file(self):
        from unittest.mock import patch

        from tests.test_tax_filing_submit import FakeAeat, _accept_response

        filing = _approved_filing(
            quarter=None,
            month=3,
            period_key="2026-03",
            aeat_period="03",
            redeme=True,
        )
        aeat = FakeAeat(_accept_response())
        svc, _repo = _service(filing, aeat=aeat)
        with patch(
            "app.services.tax_filing_service.decrypt_p12", return_value=b"p12-bytes"
        ):
            svc.submit(
                "507f1f77bcf86cd799439011",
                _user(),
                None,
                False,
                "secret",
            )
        blob = aeat.calls[0]["declaration_bytes"]
        self.assertTrue(blob.startswith(b"<T30302026030000>"))
        page_start = blob.index(b"<T30301000>")
        page = blob[page_start:page_start + 1581].decode("latin-1")
        self.assertEqual(page[106:108], "03")
        self.assertEqual(page[109], "1")


if __name__ == "__main__":
    unittest.main()
