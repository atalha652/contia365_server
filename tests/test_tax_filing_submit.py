"""T7: tax filing submit — test_mode fallback and live 303 (T5+T6)."""

import copy
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from app.models.tax_engine import Modelo303Results, VatByRateBucket
from app.models.tax_filing import TaxFilingStatus
from app.services.aeat_modelo_client import AeatModeloClientError, AeatModeloResponse
from app.services.tax_filing_service import TaxFilingService


class InMemoryFilingRepo:
    def __init__(self, filing: dict):
        self.filing = copy.deepcopy(filing)
        self.db = {
            "users": MagicMock(),
            "census_data": MagicMock(),
            "tax_reports": MagicMock(),
        }
        self.db["tax_reports"].find_one.return_value = None

    def create(self, document):
        doc = copy.deepcopy(document)
        doc.setdefault("_id", "new-filing-id")
        self.filing = doc
        return copy.deepcopy(doc)

    def get_by_period(self, user_id, modelo, year, period_key):
        filing = self.filing
        if (
            str(filing.get("user_id")) == str(user_id)
            and str(filing.get("modelo")) == str(modelo)
            and filing.get("year") == year
            and str(filing.get("period_key") or filing.get("quarter")) == str(period_key)
        ):
            return copy.deepcopy(filing)
        return None

    def get_by_id_any(self, filing_id):
        if str(self.filing.get("_id")) != str(filing_id):
            return None
        return copy.deepcopy(self.filing)

    def get_by_id(self, filing_id, user_id):
        if str(self.filing.get("_id")) != str(filing_id):
            return None
        if str(self.filing.get("user_id")) != str(user_id):
            return None
        return copy.deepcopy(self.filing)

    def update(self, filing_id, user_id, update):
        current = self.get_by_id(filing_id, user_id)
        if not current:
            return None
        for key, value in (update.get("$set") or {}).items():
            current[key] = value
        for key, value in (update.get("$push") or {}).items():
            current.setdefault(key, [])
            current[key] = list(current[key]) + [value]
        self.filing = current
        return copy.deepcopy(current)


def _totals():
    return Modelo303Results(
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


def _approved_filing(**overrides):
    doc = {
        "_id": "507f1f77bcf86cd799439011",
        "user_id": "user-1",
        "modelo": "303",
        "year": 2026,
        "quarter": "Q2",
        "period_key": "Q2",
        "status": TaxFilingStatus.APPROVED.value,
        "calculation": {
            "modelo": "303",
            "totals": _totals().model_dump(),
        },
        "history": [],
        "comments": [],
    }
    doc.update(overrides)
    return doc


def _user(**overrides):
    doc = {
        "_id": "user-1",
        "tax_id": "55238025Y",
        "full_name": "BROWN FERNANDEZ ROBERT GLASCO",
        "p12_encrypted": b"encrypted-p12",
    }
    doc.update(overrides)
    return doc


def _accept_response():
    return AeatModeloResponse(
        success=True,
        code="0",
        description="Declaracion aceptada",
        csv="CSVTEST123",
        justificante="JUS-1",
        http_status=200,
        raw_response="<ok/>",
    )


def _reject_response():
    return AeatModeloResponse(
        success=False,
        code="1234",
        description="Periodo no valido",
        csv=None,
        justificante=None,
        http_status=200,
        raw_response="<reject/>",
    )


class FakeAeat:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def submit(self, declaration_bytes, p12_bytes, p12_password, modelo="303"):
        self.calls.append(
            {
                "declaration_bytes": declaration_bytes,
                "p12_bytes": p12_bytes,
                "p12_password": p12_password,
                "modelo": modelo,
            }
        )
        if self.error:
            raise self.error
        return self.response


def _service(filing, aeat=None, user_profile=None):
    repo = InMemoryFilingRepo(filing)
    svc = TaxFilingService(
        repo=repo,
        engine=MagicMock(),
        aeat_modelo_client=aeat or FakeAeat(_accept_response()),
    )
    identity = user_profile or {
        "taxpayer_identity": {
            "nif_nie": "55238025Y",
            "full_name": "BROWN FERNANDEZ ROBERT GLASCO",
        }
    }
    svc._profile = lambda _user: identity
    return svc, repo


class TaxFilingSubmitTests(unittest.TestCase):
    def test_test_mode_does_not_call_aeat(self):
        aeat = FakeAeat(_accept_response())
        svc, repo = _service(_approved_filing(), aeat=aeat)
        updated = svc.submit("507f1f77bcf86cd799439011", _user(), None, True, "secret")
        self.assertEqual(updated["status"], TaxFilingStatus.SUBMITTED.value)
        self.assertEqual(updated["submission"]["mode"], "test")
        self.assertTrue(updated["submission"]["reference"].startswith("TEST-"))
        self.assertEqual(aeat.calls, [])
        blob = json.dumps(updated, default=str)
        self.assertNotIn("secret", blob)

    def test_live_accept_stores_aeat_fields(self):
        aeat = FakeAeat(_accept_response())
        svc, repo = _service(_approved_filing(), aeat=aeat)
        with patch(
            "app.services.tax_filing_service.decrypt_p12", return_value=b"p12-bytes"
        ):
            updated = svc.submit(
                "507f1f77bcf86cd799439011",
                _user(),
                "send it",
                False,
                "secret",
            )
        self.assertEqual(updated["status"], TaxFilingStatus.ACCEPTED.value)
        self.assertEqual(updated["submission"]["mode"], "live")
        self.assertEqual(updated["aeat_result"]["code"], "0")
        self.assertEqual(updated["aeat_result"]["csv"], "CSVTEST123")
        self.assertEqual(updated["aeat_result"]["justificante"], "JUS-1")
        self.assertEqual(updated["aeat_result"]["source"], "aeat")
        self.assertIn("accepted", updated["aeat_result"]["message"].lower())
        self.assertIsNotNone(updated.get("submitted_at"))
        self.assertIsNotNone(updated.get("accepted_at"))
        self.assertEqual(len(aeat.calls), 1)
        self.assertTrue(aeat.calls[0]["declaration_bytes"].startswith(b"<T3030"))
        self.assertEqual(aeat.calls[0]["p12_password"], "secret")
        blob = json.dumps(updated, default=str)
        self.assertNotIn("secret", blob)
        self.assertNotIn("cert_password", blob)

    def test_live_reject_is_rejected_from_aeat(self):
        aeat = FakeAeat(_reject_response())
        svc, _repo = _service(_approved_filing(), aeat=aeat)
        with patch(
            "app.services.tax_filing_service.decrypt_p12", return_value=b"p12-bytes"
        ):
            updated = svc.submit(
                "507f1f77bcf86cd799439011", _user(), None, False, "secret"
            )
        self.assertEqual(updated["status"], TaxFilingStatus.REJECTED.value)
        self.assertEqual(updated["aeat_result"]["code"], "1234")
        self.assertIn("Periodo", updated["aeat_result"]["description"])
        self.assertIsNotNone(updated.get("rejected_at"))

    def test_live_requires_cert_password(self):
        svc, repo = _service(_approved_filing())
        with patch.dict(
            os.environ, {"CERT_PASSWORD": "", "AEAT_P12_PASSWORD": ""}, clear=False
        ):
            with self.assertRaises(ValueError) as ctx:
                svc.submit("507f1f77bcf86cd799439011", _user(), None, False, "")
        self.assertIn("cert_password", str(ctx.exception))
        self.assertEqual(repo.filing["status"], TaxFilingStatus.APPROVED.value)

    def test_live_uses_env_cert_password(self):
        aeat = FakeAeat(_accept_response())
        svc, _repo = _service(_approved_filing(), aeat=aeat)
        with patch.dict(os.environ, {"CERT_PASSWORD": "env-secret"}, clear=False):
            with patch(
                "app.services.tax_filing_service.decrypt_p12", return_value=b"p12-bytes"
            ):
                updated = svc.submit(
                    "507f1f77bcf86cd799439011", _user(), None, False, None
                )
        self.assertEqual(updated["status"], TaxFilingStatus.ACCEPTED.value)
        self.assertEqual(aeat.calls[0]["p12_password"], "env-secret")
        blob = json.dumps(updated, default=str)
        self.assertNotIn("env-secret", blob)
        self.assertNotIn("cert_password", blob)

    def test_live_130_accept_stores_aeat_fields(self):
        aeat = FakeAeat(_accept_response())
        filing = _approved_filing(
            modelo="130",
            calculation={
                "modelo": "130",
                "totals": {
                    "total_income": 10000.0,
                    "total_expenses": 2000.0,
                    "taxable_income": 8000.0,
                    "irpf_rate": 0.20,
                    "irpf_already_withheld": 0.0,
                    "prior_payments": 0.0,
                    "irpf_payable": 1600.0,
                },
            },
        )
        svc, _repo = _service(filing, aeat=aeat)
        with patch(
            "app.services.tax_filing_service.decrypt_p12", return_value=b"p12-bytes"
        ):
            updated = svc.submit(
                "507f1f77bcf86cd799439011", _user(), None, False, "secret"
            )
        self.assertEqual(updated["status"], TaxFilingStatus.ACCEPTED.value)
        self.assertEqual(aeat.calls[0]["modelo"], "130")
        self.assertTrue(aeat.calls[0]["declaration_bytes"].startswith(b"<T1300"))

    def test_live_111_without_percipients_is_refused(self):
        svc, repo = _service(_approved_filing(
            modelo="111",
            calculation={
                "modelo": "111",
                "totals": {
                    "total_base": 1000.0,
                    "total_withheld": 150.0,
                    "withholding_payable": 150.0,
                    "percipient_count": 0,
                    "legally_complete": False,
                    "lines": [],
                },
            },
        ))
        with patch(
            "app.services.tax_filing_service.decrypt_p12", return_value=b"p12-bytes"
        ):
            with self.assertRaises(ValueError) as ctx:
                svc.submit("507f1f77bcf86cd799439011", _user(), None, False, "secret")
        self.assertIn("percipient", str(ctx.exception).lower())
        self.assertEqual(repo.filing["status"], TaxFilingStatus.APPROVED.value)

    def test_live_unsupported_modelo_is_refused(self):
        svc, repo = _service(_approved_filing(modelo="347"))
        with self.assertRaises(ValueError) as ctx:
            svc.submit("507f1f77bcf86cd799439011", _user(), None, False, "secret")
        self.assertIn("347", str(ctx.exception))
        self.assertEqual(repo.filing["status"], TaxFilingStatus.APPROVED.value)

    def test_live_requires_uploaded_p12(self):
        svc, _repo = _service(_approved_filing())
        user = _user()
        del user["p12_encrypted"]
        with self.assertRaises(ValueError) as ctx:
            svc.submit("507f1f77bcf86cd799439011", user, None, False, "secret")
        self.assertIn("certificate", str(ctx.exception).lower())

    def test_manual_result_refused_from_approved(self):
        svc, repo = _service(_approved_filing())
        with self.assertRaises(ValueError):
            svc.record_result(
                "507f1f77bcf86cd799439011",
                _user(),
                True,
                {"code": "0", "description": "skip"},
                None,
            )
        self.assertEqual(repo.filing["status"], TaxFilingStatus.APPROVED.value)

    def test_transport_error_keeps_approved(self):
        aeat = FakeAeat(
            error=AeatModeloClientError("TRANSPORT", "timeout", "")
        )
        svc, repo = _service(_approved_filing(), aeat=aeat)
        with patch(
            "app.services.tax_filing_service.decrypt_p12", return_value=b"p12-bytes"
        ):
            with self.assertRaises(AeatModeloClientError):
                svc.submit(
                    "507f1f77bcf86cd799439011", _user(), None, False, "secret"
                )
        self.assertEqual(repo.filing["status"], TaxFilingStatus.APPROVED.value)
        self.assertIsNone(repo.filing.get("aeat_result"))


if __name__ == "__main__":
    unittest.main()
