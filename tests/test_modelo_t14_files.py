"""T14: official files for 130 / 111 / 115 / 390 / 190."""

import unittest

from app.models.tax_engine import (
    Modelo111Results,
    Modelo115Results,
    Modelo130Results,
    Modelo190Results,
    Modelo390Results,
    VatByRateBucket,
)
from app.services.modelo_111_file_service import build_modelo_111_file
from app.services.modelo_115_file_service import build_modelo_115_file
from app.services.modelo_130_file_service import PAGE_LEN as PAGE_130, build_modelo_130_file, slice_page_field
from app.services.modelo_190_file_service import build_modelo_190_file
from app.services.modelo_390_file_service import build_modelo_390_file
from app.services.modelo_boe_common import ModeloFileError
from app.services.modelo_file_builder import filing_is_legally_complete


class Modelo130FileTests(unittest.TestCase):
    def test_wrapper_and_casillas(self):
        blob = build_modelo_130_file(
            nif="55238025Y",
            name="BROWN FERNANDEZ ROBERT GLASCO",
            year=2026,
            quarter="Q2",
            totals=Modelo130Results(
                total_income=10000, total_expenses=2000, taxable_income=8000,
                irpf_rate=0.2, irpf_already_withheld=0, irpf_payable=1600,
            ),
        )
        self.assertTrue(blob.startswith("<T130020262T0000>"))
        self.assertIn("<T13001000>", blob)
        self.assertTrue(blob.endswith("</T130020262T0000>"))
        page = blob.split("<T13001000>")[1]
        page = "<T13001000>" + page.split("</T13001000>")[0] + "</T13001000>"
        self.assertEqual(len(page), PAGE_130)
        self.assertEqual(slice_page_field(page, 14, 9), "55238025Y")
        self.assertEqual(slice_page_field(page, 107, 2), "2T")


class Modelo111FileTests(unittest.TestCase):
    def test_refuses_without_percipients(self):
        with self.assertRaises(ModeloFileError):
            build_modelo_111_file(
                nif="55238025Y",
                name="TEST",
                year=2026,
                quarter="Q1",
                totals=Modelo111Results(legally_complete=False, lines=[]),
            )

    def test_builds_with_lines(self):
        blob = build_modelo_111_file(
            nif="55238025Y",
            name="TEST",
            year=2026,
            quarter="Q1",
            totals=Modelo111Results(
                total_base=1000,
                total_withheld=150,
                withholding_payable=150,
                percipient_count=1,
                legally_complete=True,
                lines=[{
                    "nif": "12345678Z",
                    "full_name": "WORKER",
                    "perception_key": "G",
                    "base_amount": 1000,
                    "withheld_amount": 150,
                }],
            ),
        )
        self.assertTrue(blob.startswith("<T111020261T0000>"))
        self.assertIn("<T11101000>", blob)


class Modelo115FileTests(unittest.TestCase):
    def test_builds(self):
        blob = build_modelo_115_file(
            nif="55238025Y",
            name="TEST",
            year=2026,
            quarter="Q3",
            totals=Modelo115Results(total_rent_base=3000, withholding_payable=570, percipient_count=1),
        )
        self.assertTrue(blob.startswith("<T115020263T0000>"))
        self.assertIn("<T11501000>", blob)


class Modelo390FileTests(unittest.TestCase):
    def test_annual_wrapper_and_page02(self):
        blob = build_modelo_390_file(
            nif="55238025Y",
            name="TEST",
            year=2026,
            totals=Modelo390Results(
                total_sales=1000, output_vat=210, net_vat=210,
                vat_by_rate={"21": VatByRateBucket(output_base=1000, output_vat=210)},
            ),
        )
        self.assertTrue(blob.startswith("<T390020260A0000>"))
        self.assertIn("<T39001000>", blob)
        self.assertIn("<T39002000>", blob)


class Modelo190FileTests(unittest.TestCase):
    def test_refuses_without_lines(self):
        with self.assertRaises(ModeloFileError):
            build_modelo_190_file(
                nif="55238025Y", name="TEST", year=2026,
                totals=Modelo190Results(legally_complete=False, lines=[]),
            )

    def test_tipo1_and_tipo2(self):
        blob = build_modelo_190_file(
            nif="55238025Y",
            name="TEST",
            year=2026,
            totals=Modelo190Results(
                total_base=1000, total_withheld=150, percipient_count=1,
                legally_complete=True,
                lines=[{
                    "nif": "12345678Z",
                    "full_name": "WORKER ONE",
                    "perception_key": "A",
                    "perception_subkey": "01",
                    "base_amount": 1000,
                    "withheld_amount": 150,
                    "province_code": "28",
                }],
            ),
        )
        self.assertTrue(blob.startswith("1190202655238025Y"))
        self.assertIn("\r\n2", blob)
        self.assertIn("12345678Z", blob)
        self.assertNotIn("taxable_income", blob)


class FileBuilderDispatchTests(unittest.TestCase):
    def test_legal_complete_flag(self):
        self.assertTrue(filing_is_legally_complete("303", {"totals": {}}))
        self.assertFalse(filing_is_legally_complete("111", {
            "totals": {"legally_complete": False, "lines": []}
        }))


if __name__ == "__main__":
    unittest.main()
