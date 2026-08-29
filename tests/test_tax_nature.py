"""Structured tax nature — classify from fields and amounts, not wording."""

import unittest

from app.services.tax_nature import (
    infer_operation_type,
    infer_withholding_type,
    persistable_nature,
    signals_from_nature,
)
from app.services.tax_classification_service import _extract_signals, _modelo_matches_signals
from app.services.vat_303 import classify_303_line


class TaxNatureTests(unittest.TestCase):
    def test_stored_operation_type_wins_over_description(self):
        data = {
            "operation_type": "isp",
            "withholding_type": "none",
            "items": [{"description": "Office rent alquiler honorarios"}],
            "totals": {"VAT_amount": 210, "VAT_rate": 21, "total": 1000, "Total_with_Tax": 1210},
        }
        self.assertEqual(infer_operation_type(data, ocr_text="alquiler", seed_from_text=True), "isp")
        self.assertEqual(infer_withholding_type(data, ocr_text="alquiler", seed_from_text=True), "none")

    def test_wrong_description_does_not_change_stored_withholding(self):
        data = {
            "operation_type": "general",
            "withholding_type": "professional",
            "items": [{"description": "Monthly rent for the office"}],
            "totals": {"IRPF_amount": 150, "IRPF_rate": 15},
        }
        self.assertEqual(
            infer_withholding_type(data, ocr_text="alquiler arrendamiento", seed_from_text=True),
            "professional",
        )

    def test_seed_from_text_only_when_fields_missing(self):
        data = {
            "items": [{"description": "Inversión del sujeto pasivo"}],
            "totals": {"VAT_amount": 0, "VAT_rate": 21, "total": 1000, "Total_with_Tax": 1000},
        }
        seeded, nature = persistable_nature(data, ocr_text="Inversión del sujeto pasivo")
        self.assertEqual(nature["operation_type"], "isp")
        self.assertEqual(seeded["operation_type"], "isp")

    def test_signals_use_amounts_not_iva_word(self):
        data = {
            "transaction_type": "income",
            "operation_type": "general",
            "withholding_type": "none",
            "totals": {"VAT_amount": 210, "VAT_rate": 21, "total": 1000, "Total_with_Tax": 1210},
        }
        signals = signals_from_nature(data, {"operation_type": "general", "withholding_type": "none"})
        self.assertTrue(signals["has_vat"])
        self.assertFalse(signals["has_irpf"])
        self.assertFalse(signals["is_rent"])

    def test_extract_signals_ignores_consulting_keyword_when_stored(self):
        entry = {
            "ocr_text": "software development consulting honorarios alquiler",
            "invoice_data": {
                "transaction_type": "expense",
                "operation_type": "general",
                "withholding_type": "rental",
                "totals": {"VAT_amount": 0, "VAT_rate": 0, "IRPF_amount": 190, "IRPF_rate": 19, "total": 1000, "Total_with_Tax": 810},
            },
        }
        signals = _extract_signals(entry)
        self.assertTrue(signals["is_rent"])
        self.assertFalse(signals["is_professional"])
        self.assertTrue(signals["has_irpf"])

    def test_modelo_match_from_rent_signal(self):
        explanation = _modelo_matches_signals(
            "Retenciones e ingresos a cuenta. Rendimientos procedentes del arrendamiento",
            {"is_rent": True, "has_irpf": True, "has_vat": False, "is_professional": False,
             "is_income": False, "is_expense": True},
        )
        self.assertIsNotNone(explanation)
        self.assertIn("is_rent", explanation)

    def test_303_stored_type_wins_over_wrong_text(self):
        self.assertEqual(
            classify_303_line(
                vat_rate=21,
                text="REBU bienes usados alquiler",
                stored_operation_type="isp",
            ),
            "isp",
        )

    def test_303_legacy_text_when_no_stored_type(self):
        self.assertEqual(
            classify_303_line(vat_rate=21, text="Inversión del sujeto pasivo"),
            "isp",
        )


if __name__ == "__main__":
    unittest.main()
