"""T5: official 2026 Modelo 303 diseño de registro (DP30300 + DP30301)."""

import unittest

from app.models.tax_engine import Modelo303Results, VatByRateBucket
from app.services.modelo_303_file_service import (
    OFFICIAL_DISENO_PATH,
    PAGE1_LEN,
    build_modelo_303_file,
    build_page_01,
    slice_page_field,
)


class Modelo303FileTests(unittest.TestCase):
    def setUp(self):
        self.totals = Modelo303Results(
            total_sales=1000.0,
            total_expenses=0.0,
            output_vat=210.0,
            input_vat=0.0,
            vat_payable=210.0,
            vat_by_rate={
                "21": VatByRateBucket(
                    output_base=1000.0, output_vat=210.0, input_base=0.0, input_vat=0.0
                ),
                "10": VatByRateBucket(),
                "4": VatByRateBucket(),
                "0": VatByRateBucket(),
            },
        )

    def test_official_diseno_is_checked_in(self):
        self.assertTrue(
            OFFICIAL_DISENO_PATH.is_file(),
            f"Missing {OFFICIAL_DISENO_PATH} — AEAT DR303e26v101.xlsx",
        )
        self.assertGreater(OFFICIAL_DISENO_PATH.stat().st_size, 100_000)

    def test_page01_length_and_tags(self):
        page = build_page_01(
            nif="55238025Y",
            name="BROWN FERNANDEZ ROBERT GLASCO",
            year=2026,
            period="2T",
            totals=self.totals,
        )
        self.assertEqual(len(page), PAGE1_LEN)
        self.assertTrue(page.startswith("<T30301000>"))
        self.assertTrue(page.endswith("</T30301000>"))

    def test_identity_and_21_percent_casillas(self):
        page = build_page_01(
            nif="55238025Y",
            name="BROWN FERNANDEZ ROBERT GLASCO",
            year=2026,
            period="2T",
            totals=self.totals,
        )
        self.assertEqual(slice_page_field(page, 14, 9), "55238025Y")
        self.assertEqual(slice_page_field(page, 103, 4), "2026")
        self.assertEqual(slice_page_field(page, 107, 2), "2T")
        self.assertEqual(slice_page_field(page, 13, 1), "I")
        self.assertEqual(slice_page_field(page, 111, 1), "3")  # sólo RG
        self.assertEqual(slice_page_field(page, 343, 5), "02100")  # [08]
        self.assertEqual(slice_page_field(page, 326, 17), "00000000000100000")  # [07] 1000.00
        self.assertEqual(slice_page_field(page, 348, 17), "00000000000021000")  # [09] 210.00
        self.assertEqual(slice_page_field(page, 696, 17), "00000000000021000")  # [27]
        self.assertEqual(slice_page_field(page, 1019, 17), "00000000000021000")  # [46]

    def test_recargo_and_isp_stay_zero(self):
        page = build_page_01(
            nif="55238025Y",
            name="TEST",
            year=2026,
            period="2T",
            totals=self.totals,
        )
        self.assertEqual(slice_page_field(page, 399, 17), "0" * 17)  # [12] ISP
        self.assertEqual(slice_page_field(page, 467, 17), "0" * 17)  # [156] recargo

    def test_wrapper_and_full_file(self):
        blob = build_modelo_303_file(
            nif="55238025Y",
            name="BROWN FERNANDEZ ROBERT GLASCO",
            year=2026,
            quarter="Q2",
            totals=self.totals,
            developer_nif="B12345678",
        )
        self.assertTrue(blob.startswith("<T303020262T0000>"))
        self.assertTrue(blob.endswith("</T303020262T0000>"))
        self.assertIn("<AUX>", blob)
        self.assertIn("</AUX>", blob)
        self.assertIn("<T30301000>", blob)
        self.assertIn("B12345678", blob)

    def test_monthly_redeme_period_and_flag(self):
        page = build_page_01(
            nif="55238025Y",
            name="BROWN FERNANDEZ ROBERT GLASCO",
            year=2026,
            period="03",
            totals=self.totals,
            redeme=True,
        )
        self.assertEqual(slice_page_field(page, 107, 2), "03")
        self.assertEqual(slice_page_field(page, 110, 1), "1")
        self.assertEqual(slice_page_field(page, 128, 1), "0")  # not last period

        blob = build_modelo_303_file(
            nif="55238025Y",
            name="BROWN FERNANDEZ ROBERT GLASCO",
            year=2026,
            quarter="2026-03",
            totals=self.totals,
            redeme=True,
            developer_nif="B12345678",
        )
        self.assertTrue(blob.startswith("<T30302026030000>"))
        self.assertTrue(blob.endswith("</T30302026030000>"))

    def test_december_redeme_is_last_period_and_exempt_from_390(self):
        page = build_page_01(
            nif="55238025Y",
            name="TEST",
            year=2026,
            period="12",
            totals=self.totals,
            redeme=True,
        )
        self.assertEqual(slice_page_field(page, 107, 2), "12")
        self.assertEqual(slice_page_field(page, 110, 1), "1")
        self.assertEqual(slice_page_field(page, 128, 1), "1")


if __name__ == "__main__":
    unittest.main()
