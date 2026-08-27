"""Dashboard VAT/IRPF summaries expose tax-engine field names."""

from datetime import date
from decimal import Decimal
import unittest

from app.models.tax_models import IRPFSummary, VATSummary


class EngineFieldAliasTests(unittest.TestCase):
    def test_vat_summary_fills_engine_names(self):
        summary = VATSummary(
            period_start=date(2026, 4, 1),
            period_end=date(2026, 6, 30),
            output_vat_amount=Decimal("210"),
            input_vat_amount=Decimal("21"),
            vat_payable=Decimal("189"),
        )
        self.assertEqual(summary.output_vat, Decimal("210"))
        self.assertEqual(summary.input_vat, Decimal("21"))

    def test_irpf_summary_fills_engine_names(self):
        summary = IRPFSummary(
            period_start=date(2026, 4, 1),
            period_end=date(2026, 6, 30),
            quarter=2,
            gross_income=Decimal("1000"),
            deductible_expenses=Decimal("200"),
            net_income=Decimal("800"),
            irpf_payable=Decimal("160"),
            previous_quarters_irpf=Decimal("40"),
            irpf_to_pay=Decimal("120"),
        )
        self.assertEqual(summary.total_income, Decimal("1000"))
        self.assertEqual(summary.total_expenses, Decimal("200"))
        self.assertEqual(summary.taxable_income, Decimal("800"))
        self.assertEqual(summary.prior_payments, Decimal("40"))
        self.assertEqual(summary.irpf_to_pay, Decimal("120"))


if __name__ == "__main__":
    unittest.main()
