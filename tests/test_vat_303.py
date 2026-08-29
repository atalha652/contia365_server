"""Modelo 303 special-regime classification and casillas."""

import unittest

from app.models.tax_engine import Modelo303Results, RecargoBucket, VatByRateBucket
from app.services.modelo_303_file_service import build_page_01, slice_page_field
from app.services.vat_303 import (
    classify_303_line,
    modelo_303_payable,
    parse_prorrata_percent,
    snap_rg_vat_rate,
)


class Classify303Tests(unittest.TestCase):
    def test_isp_from_text(self):
        self.assertEqual(
            classify_303_line(vat_rate=21, text="Inversión del sujeto pasivo"),
            "isp",
        )

    def test_recargo_rate_not_snapped_to_4(self):
        self.assertEqual(snap_rg_vat_rate(21), "21")
        self.assertEqual(
            classify_303_line(vat_rate=5.2, recargo_rate=5.2, recargo_amount=52),
            "recargo",
        )

    def test_used_goods(self):
        self.assertEqual(classify_303_line(vat_rate=0, text="REBU bienes usados"), "used_goods")

    def test_prorrata_from_profile(self):
        profile = {"professional_registration": {"vat_regime": "General prorrata 60%"}}
        self.assertEqual(parse_prorrata_percent(profile), 60.0)

    def test_payable_includes_recargo_and_prorrata(self):
        # 210 output + 52 recargo − 50% of 100 input = 262 − 50 = 212
        self.assertEqual(
            modelo_303_payable(210, 100, recargo_vat=52, prorrata_percent=50),
            212.0,
        )


class Modelo303FileSpecialTests(unittest.TestCase):
    def test_isp_and_recargo_casillas(self):
        page = build_page_01(
            nif="55238025Y",
            name="TEST",
            year=2026,
            period="2T",
            totals=Modelo303Results(
                output_vat=210,
                vat_by_rate={"21": VatByRateBucket(output_base=1000, output_vat=210)},
                isp_base=500,
                isp_vat=105,
                recargo_by_rate={"5.2": RecargoBucket(base=1000, vat=52)},
                recargo_vat=52,
            ),
        )
        self.assertEqual(slice_page_field(page, 399, 17), "00000000000050000")  # [12]
        self.assertEqual(slice_page_field(page, 416, 17), "00000000000010500")  # [13]
        self.assertEqual(slice_page_field(page, 545, 17), "00000000000100000")  # [16]
        self.assertEqual(slice_page_field(page, 562, 5), "00520")
        self.assertEqual(slice_page_field(page, 567, 17), "00000000000005200")  # [18]


class Modelo390SpecialTests(unittest.TestCase):
    def test_page02_writes_isp_and_recargo(self):
        from app.models.tax_engine import Modelo390Results
        from app.services.modelo_390_file_service import build_modelo_390_file, slice_page_field

        blob = build_modelo_390_file(
            nif="55238025Y",
            name="TEST",
            year=2026,
            totals=Modelo390Results(
                total_sales=1000,
                output_vat=210,
                net_vat=367,
                isp_base=500,
                isp_vat=105,
                recargo_vat=52,
                recargo_by_rate={"5.2": RecargoBucket(base=1000, vat=52)},
                vat_by_rate={"21": VatByRateBucket(output_base=1000, output_vat=210)},
            ),
        )
        page2 = blob.split("<T39002000>")[1]
        page2 = "<T39002000>" + page2.split("</T39002000>")[0] + "</T39002000>"
        self.assertTrue(slice_page_field(page2, 285, 17).endswith("50000"))
        self.assertTrue(slice_page_field(page2, 302, 17).endswith("10500"))
        self.assertTrue(slice_page_field(page2, 336, 17).endswith("5200"))


if __name__ == "__main__":
    unittest.main()
