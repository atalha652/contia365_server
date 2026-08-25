"""T8 backend: mapped AEAT messages, 303 result lock, justificante PDF."""

import unittest
from datetime import datetime

from unittest.mock import MagicMock

from app.models.tax_filing import TaxFilingStatus
from app.services.aeat_result_messages import enrich_aeat_result, readable_aeat_message
from app.services.tax_filing_service import TaxFilingService, serialize_filing
from tests.test_tax_filing_submit import (
    InMemoryFilingRepo,
    _approved_filing,
    _service,
    _user,
)


class AeatMessageMapTests(unittest.TestCase):
    def test_known_accept_code(self):
        self.assertIn("accepted", readable_aeat_message("0").lower())

    def test_unknown_code_keeps_description(self):
        text = readable_aeat_message("1234", "Periodo no valido")
        self.assertEqual(text, "Periodo no valido")

    def test_enrich_sets_message_and_flag(self):
        enriched = enrich_aeat_result({
            "code": "0",
            "description": "Declaracion aceptada",
            "csv": "CSVTEST123",
            "justificante": "JUS-1",
        })
        self.assertIn("accepted", enriched["message"].lower())
        self.assertTrue(enriched["has_justificante"])


class TaxFilingResultPanelTests(unittest.TestCase):
    def test_serialize_exposes_panel_fields(self):
        payload = serialize_filing({
            "_id": "abc",
            "status": TaxFilingStatus.REJECTED.value,
            "modelo": "303",
            "year": 2026,
            "quarter": "Q2",
            "aeat_result": {
                "code": "1234",
                "description": "Periodo no valido",
                "justificante": None,
                "csv": None,
                "recorded_at": datetime(2026, 8, 25, 12, 0, 0),
            },
        })
        self.assertTrue(payload["can_recalculate"])
        self.assertFalse(payload["justificante_available"])
        self.assertEqual(payload["aeat_result"]["message"], "Periodo no valido")
        self.assertIsInstance(payload["aeat_result"]["recorded_at"], str)

    def test_manual_result_refused_for_303(self):
        filing = _approved_filing(status=TaxFilingStatus.SUBMITTED.value)
        svc, repo = _service(filing)
        with self.assertRaises(ValueError) as ctx:
            svc.record_result(
                "507f1f77bcf86cd799439011",
                _user(),
                True,
                {"code": "TEST-OK", "description": "nope"},
                None,
            )
        self.assertIn("303", str(ctx.exception))
        self.assertEqual(repo.filing["status"], TaxFilingStatus.SUBMITTED.value)

    def test_justificante_pdf_for_accepted_filing(self):
        filing = _approved_filing(
            status=TaxFilingStatus.ACCEPTED.value,
            aeat_result={
                "code": "0",
                "description": "Declaracion aceptada",
                "csv": "CSVTEST123",
                "justificante": "JUS-1",
            },
        )
        svc = TaxFilingService(
            repo=InMemoryFilingRepo(filing),
            engine=MagicMock(),
            aeat_modelo_client=MagicMock(),
        )
        pdf_bytes, filename = svc.justificante_pdf(
            "507f1f77bcf86cd799439011", _user()
        )
        self.assertTrue(pdf_bytes.startswith(b"%PDF"))
        self.assertIn("justificante-modelo-303-2026-Q2.pdf", filename)
        self.assertGreater(len(pdf_bytes), 200)

    def test_justificante_missing_without_aeat_result(self):
        svc, _repo = _service(_approved_filing())
        with self.assertRaises(LookupError):
            svc.justificante_pdf("507f1f77bcf86cd799439011", _user())


if __name__ == "__main__":
    unittest.main()
