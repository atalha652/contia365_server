"""Unit tests for AEAT Apoderamiento SOAP Client."""

import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from app.services.aeat_apoderamiento_client import (
    AeatApoderamientoClient,
    ApoderamientoVerificationResult,
)

AEAT_ACTIVE_SOAP_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ws="https://www.agenciatributaria.gob.es/apoderamiento">
  <soapenv:Body>
    <ws:ConsultaApoderamientosResponse>
      <ws:Estado>ACTIVO</ws:Estado>
      <ws:Descripcion>Apoderamiento vigente y confirmado</ws:Descripcion>
      <ws:NumeroReferencia>REF-2026-AEAT-001</ws:NumeroReferencia>
    </ws:ConsultaApoderamientosResponse>
  </soapenv:Body>
</soapenv:Envelope>"""

AEAT_REVOKED_SOAP_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ws="https://www.agenciatributaria.gob.es/apoderamiento">
  <soapenv:Body>
    <ws:ConsultaApoderamientosResponse>
      <ws:Estado>REVOCADO</ws:Estado>
      <ws:Descripcion>El apoderamiento fue revocado por el poderdante</ws:Descripcion>
    </ws:ConsultaApoderamientosResponse>
  </soapenv:Body>
</soapenv:Envelope>"""

AEAT_NOT_FOUND_SOAP_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ws="https://www.agenciatributaria.gob.es/apoderamiento">
  <soapenv:Body>
    <ws:ConsultaApoderamientosResponse>
      <ws:Estado>NO_EXISTE</ws:Estado>
      <ws:Descripcion>No se encontro apoderamiento para los NIF proporcionados</ws:Descripcion>
    </ws:ConsultaApoderamientosResponse>
  </soapenv:Body>
</soapenv:Envelope>"""


class AeatApoderamientoClientTests(unittest.TestCase):
    def setUp(self):
        self.client = AeatApoderamientoClient()
        self.now = datetime.utcnow()

    def test_parse_active_soap_response(self):
        result = self.client._parse_soap_response(AEAT_ACTIVE_SOAP_RESPONSE, 200, self.now)
        self.assertTrue(result.is_valid)
        self.assertEqual(result.status, "VERIFIED")
        self.assertEqual(result.reference, "REF-2026-AEAT-001")
        self.assertIn("vigente", result.message.lower())

    def test_parse_revoked_soap_response(self):
        result = self.client._parse_soap_response(AEAT_REVOKED_SOAP_RESPONSE, 200, self.now)
        self.assertFalse(result.is_valid)
        self.assertEqual(result.status, "REVOKED")
        self.assertIn("revocado", result.message.lower())

    def test_parse_not_found_soap_response(self):
        result = self.client._parse_soap_response(AEAT_NOT_FOUND_SOAP_RESPONSE, 200, self.now)
        self.assertFalse(result.is_valid)
        self.assertEqual(result.status, "NOT_FOUND")

    def test_simulation_mode_success(self):
        result = self.client._simulate_verification("B12345678", "B00000000", "APOD-123", self.now)
        self.assertTrue(result.is_valid)
        self.assertEqual(result.status, "VERIFIED")
        self.assertEqual(result.source, "AEAT_SIMULATION")
        self.assertEqual(result.reference, "APOD-123")

    def test_simulation_mode_failure_trigger(self):
        result = self.client._simulate_verification("FAIL_AUTH", "B00000000", None, self.now)
        self.assertFalse(result.is_valid)
        self.assertEqual(result.status, "NOT_FOUND")

    def test_soap_envelope_builder(self):
        envelope = self.client._build_soap_envelope("B12345678", "B00000000", "APOD-REF-456")
        decoded = envelope.decode("utf-8")
        self.assertIn("B12345678", decoded)
        self.assertIn("B00000000", decoded)
        self.assertIn("APOD-REF-456", decoded)
        self.assertIn("ConsultaApoderamientos", decoded)


if __name__ == "__main__":
    unittest.main()
