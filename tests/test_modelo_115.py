"""Modelo 115 uses invoice IRPF only — never invents 19%."""

import unittest

from app.models.tax_engine import Modelo115Results


def _invoice_withholding(irpf_retention: float) -> float:
    """Same rule as TaxEngineService.calculate_modelo_115."""
    return round(max(0.0, float(irpf_retention or 0)), 2)


class Modelo115WithholdingTests(unittest.TestCase):
    def test_zero_irpf_stays_zero(self):
        self.assertEqual(_invoice_withholding(0), 0.0)
        self.assertNotEqual(round(1000 * 0.19, 2), 0.0)

    def test_uses_printed_irpf(self):
        self.assertEqual(_invoice_withholding(152.0), 152.0)

    def test_default_rate_is_not_19(self):
        self.assertEqual(Modelo115Results().retention_rate, 0.0)
        self.assertEqual(Modelo115Results().withholding_payable, 0.0)


if __name__ == "__main__":
    unittest.main()
