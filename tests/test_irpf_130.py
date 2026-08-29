"""Modelo 130 régimen rate and payable (prior payments)."""

import unittest

from app.models.tax_engine import Modelo130Results
from app.services.irpf_130 import (
    DEFAULT_130_RATE,
    STARTER_130_RATE,
    modelo_130_payable,
    resolve_modelo_130_rate,
)
from app.services.modelo_130_file_service import build_modelo_130_file, slice_page_field


class Resolve130RateTests(unittest.TestCase):
    def test_default_estimacion_directa(self):
        profile = {"professional_registration": {"irpf_method": "Estimación directa simplificada"}}
        self.assertEqual(resolve_modelo_130_rate(profile, 2026), DEFAULT_130_RATE)

    def test_starter_from_method(self):
        profile = {"professional_registration": {"irpf_method": "Nuevo autónomo 7%"}}
        self.assertEqual(resolve_modelo_130_rate(profile, 2026), STARTER_130_RATE)

    def test_starter_from_activity_start(self):
        profile = {
            "professional_registration": {
                "irpf_method": "Estimación directa",
                "economic_activities": [{"start_date": "2026-03-01"}],
            }
        }
        self.assertEqual(resolve_modelo_130_rate(profile, 2026), STARTER_130_RATE)
        self.assertEqual(resolve_modelo_130_rate(profile, 2028), DEFAULT_130_RATE)

    def test_empty_profile_is_20(self):
        self.assertEqual(resolve_modelo_130_rate(None, 2026), DEFAULT_130_RATE)


class Payable130Tests(unittest.TestCase):
    def test_subtracts_prior_and_withholding(self):
        # YTD 20_000 × 20% = 4_000 − 2_000 prior − 100 withheld = 1_900
        self.assertEqual(modelo_130_payable(20000, 0.20, 100, 2000), 1900.0)

    def test_never_negative(self):
        self.assertEqual(modelo_130_payable(1000, 0.20, 0, 500), 0.0)

    def test_starter_rate(self):
        self.assertEqual(modelo_130_payable(10000, 0.07, 0, 0), 700.0)


class Modelo130FilePriorTests(unittest.TestCase):
    def test_casilla_19_uses_prior_payments(self):
        blob = build_modelo_130_file(
            nif="55238025Y",
            name="BROWN FERNANDEZ ROBERT GLASCO",
            year=2026,
            quarter="Q2",
            totals=Modelo130Results(
                total_income=20000,
                total_expenses=0,
                taxable_income=20000,
                irpf_rate=0.2,
                irpf_already_withheld=0,
                prior_payments=2000,
                irpf_payable=2000,
            ),
        )
        page = blob.split("<T13001000>")[1]
        page = "<T13001000>" + page.split("</T13001000>")[0] + "</T13001000>"
        # casilla 04 at 160, 05 at 177, 19 at 415 — 4000 - 2000 prior = 2000
        self.assertEqual(slice_page_field(page, 160, 17), "00000000000400000")
        self.assertEqual(slice_page_field(page, 177, 17), "00000000000200000")
        self.assertEqual(slice_page_field(page, 415, 17), "00000000000200000")


if __name__ == "__main__":
    unittest.main()
