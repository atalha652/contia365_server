"""T6: AEAT modelo client is separate from VeriFactu."""

import os
import unittest
from unittest.mock import MagicMock, patch

from app.services.aeat_modelo_client import (
    VERIFACTU_URL_MARKER,
    AeatModeloClient,
    AeatModeloClientError,
)


ACCEPT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Respuesta>
      <CodigoRespuesta>0</CodigoRespuesta>
      <DescripcionRespuesta>Declaracion aceptada</DescripcionRespuesta>
      <CSV>CSVTEST123</CSV>
      <NumeroJustificante>JUS-1</NumeroJustificante>
    </Respuesta>
  </soap:Body>
</soap:Envelope>
"""

REJECT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Respuesta>
  <CodigoRespuesta>1234</CodigoRespuesta>
  <DescripcionRespuesta>Periodo no valido</DescripcionRespuesta>
</Respuesta>
"""


class AeatModeloClientTests(unittest.TestCase):
    def test_parse_accept(self):
        parsed = AeatModeloClient()._parse(ACCEPT_XML, 200)
        self.assertTrue(parsed.success)
        self.assertEqual(parsed.code, "0")
        self.assertEqual(parsed.csv, "CSVTEST123")
        self.assertEqual(parsed.justificante, "JUS-1")
        self.assertIn("aceptada", parsed.description.lower())

    def test_parse_reject_is_still_parseable(self):
        parsed = AeatModeloClient()._parse(REJECT_XML, 200)
        self.assertFalse(parsed.success)
        self.assertEqual(parsed.code, "1234")
        self.assertIn("Periodo", parsed.description)

    def test_refuses_verifactu_url(self):
        with patch.dict(
            os.environ,
            {"AEAT_MODELO_SUBMIT_URL": f"https://example/{VERIFACTU_URL_MARKER}"},
            clear=False,
        ):
            with self.assertRaises(AeatModeloClientError) as ctx:
                AeatModeloClient().submit(b"<T3030", b"not-a-p12", "x")
            self.assertEqual(ctx.exception.code, "CONFIG")

    def test_submit_posts_to_modelo_url_not_verifactu(self):
        modelo_url = "https://prewww1.aeat.es/wlpl/modelo-303-test"
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = ACCEPT_XML

        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = False
        mock_client.post.return_value = fake_resp

        with patch.dict(os.environ, {"AEAT_MODELO_SUBMIT_URL": modelo_url}, clear=False):
            with patch(
                "app.services.aeat_modelo_client.httpx.Client",
                return_value=mock_client,
            ):
                with patch.object(
                    AeatModeloClient,
                    "_extract_pem_files",
                    return_value=("/tmp/c.pem", "/tmp/k.pem"),
                ):
                    with patch.object(AeatModeloClient, "_cleanup"):
                        result = AeatModeloClient().submit(b"<T3030TEST", b"p12", "secret")

        self.assertTrue(result.success)
        mock_client.post.assert_called_once()
        args, kwargs = mock_client.post.call_args
        self.assertEqual(args[0], modelo_url)
        self.assertNotIn(VERIFACTU_URL_MARKER, args[0])
        self.assertEqual(kwargs["content"], b"<T3030TEST")

    def test_submit_uses_modelo_specific_url(self):
        modelo_url = "https://prewww1.aeat.es/wlpl/inwinvoc/es.aeat.dit.mdel.mod130.ws.PresentacionSOAP"
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = ACCEPT_XML
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = False
        mock_client.post.return_value = fake_resp
        with patch.dict(
            os.environ,
            {
                "AEAT_MODELO_SUBMIT_URL": (
                    "https://prewww1.aeat.es/wlpl/inwinvoc/"
                    "es.aeat.dit.mdel.mod303.ws.PresentacionSOAP"
                )
            },
            clear=False,
        ):
            with patch(
                "app.services.aeat_modelo_client.httpx.Client",
                return_value=mock_client,
            ):
                with patch.object(
                    AeatModeloClient,
                    "_extract_pem_files",
                    return_value=("/tmp/c.pem", "/tmp/k.pem"),
                ):
                    with patch.object(AeatModeloClient, "_cleanup"):
                        AeatModeloClient().submit(b"<T1300TEST", b"p12", "secret", "130")
        args, _kwargs = mock_client.post.call_args
        self.assertEqual(args[0], modelo_url)


if __name__ == "__main__":
    unittest.main()
