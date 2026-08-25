"""
AEAT Modelo presentación client (autoliquidaciones).

Separate from VeriFactu invoice submission (`aeat_client.py`).
Uses per-modelo URL + mTLS. Posts the T5 diseño-de-registro file.
SOAP wrapping is optional via AEAT_MODELO_SOAP_ACTION.

The public WSDL is not on the sede (Pre* is HTML + file import).
Set AEAT_MODELO_SUBMIT_URL_{modelo} or AEAT_MODELO_SUBMIT_URL.
The default is 303 preprod and must not equal AEAT_SUBMIT_URL.
"""

from __future__ import annotations

import logging
import os
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization.pkcs12 import load_key_and_certificates

logger = logging.getLogger(__name__)

# Not VeriFactu. Override with the official modelo-presentación endpoint.
AEAT_MODELO_SUBMIT_URL = os.getenv(
    "AEAT_MODELO_SUBMIT_URL",
    "https://prewww1.aeat.es/wlpl/inwinvoc/es.aeat.dit.mdel.mod303.ws.PresentacionSOAP",
)


def modelo_submit_url(modelo: str = "303") -> str:
    modelo = str(modelo or "303")
    specific = (os.getenv(f"AEAT_MODELO_SUBMIT_URL_{modelo}") or "").strip()
    if specific:
        return specific
    base = (os.getenv("AEAT_MODELO_SUBMIT_URL") or AEAT_MODELO_SUBMIT_URL).strip()
    if modelo == "303":
        return base
    return base.replace("mod303", f"mod{modelo}")

VERIFACTU_URL_MARKER = "VerifactuSOAP"


@dataclass
class AeatModeloResponse:
    success: bool
    code: str
    description: str
    csv: Optional[str]
    justificante: Optional[str]
    http_status: int
    raw_response: str


class AeatModeloClientError(Exception):
    def __init__(self, code: str, description: str, raw: str = ""):
        super().__init__(f"AEAT modelo error {code}: {description}")
        self.code = code
        self.description = description
        self.raw = raw


class AeatModeloClient:
    def submit(
        self,
        declaration_bytes: bytes,
        p12_bytes: bytes,
        p12_password: str,
        modelo: str = "303",
    ) -> AeatModeloResponse:
        """
        POST the official modelo file over mTLS.

        AEAT accept *or* reject is returned as a parsed object (not raised),
        so a sandbox reject still counts as a parseable response.
        """
        url = modelo_submit_url(modelo)
        if not url:
            raise AeatModeloClientError("CONFIG", "AEAT_MODELO_SUBMIT_URL is not set", "")
        if VERIFACTU_URL_MARKER in url:
            raise AeatModeloClientError(
                "CONFIG",
                "AEAT_MODELO_SUBMIT_URL must not be the VeriFactu invoice endpoint",
                url,
            )

        body, headers = self._build_request(declaration_bytes, modelo)
        cert_file = key_file = None
        try:
            cert_file, key_file = self._extract_pem_files(p12_bytes, p12_password)
            with httpx.Client(cert=(cert_file, key_file), verify=True, timeout=60.0) as client:
                resp = client.post(url, content=body, headers=headers)
            raw = resp.text or ""
            logger.info("AEAT modelo %s HTTP %s (%s chars)", modelo, resp.status_code, len(raw))
        except httpx.RequestError as exc:
            raise AeatModeloClientError("TRANSPORT", str(exc), "") from exc
        finally:
            self._cleanup(cert_file, key_file)

        return self._parse(raw, resp.status_code)

    def _build_request(self, declaration_bytes: bytes, modelo: str = "303") -> tuple[bytes, dict]:
        soap_action = (os.getenv("AEAT_MODELO_SOAP_ACTION") or "").strip()
        if soap_action:
            payload = declaration_bytes.decode("latin-1", errors="replace")
            envelope = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
                "<soap:Header/><soap:Body>"
                f'<aeat:Presentacion xmlns:aeat="urn:aeat:modelo:{modelo}">'
                f"<aeat:Fichero><![CDATA[{payload}]]></aeat:Fichero>"
                "</aeat:Presentacion></soap:Body></soap:Envelope>"
            )
            return envelope.encode("utf-8"), {
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f'"{soap_action}"',
            }
        return declaration_bytes, {
            "Content-Type": "text/plain; charset=ISO-8859-1",
        }

    def _extract_pem_files(self, p12_bytes: bytes, password: str) -> tuple[str, str]:
        pw = password.encode("utf-8") if password else None
        try:
            private_key, cert, _ = load_key_and_certificates(p12_bytes, pw)
        except Exception as exc:
            raise AeatModeloClientError("CERT", "Invalid .p12 or password", "") from exc
        if private_key is None or cert is None:
            raise AeatModeloClientError("CERT", "Certificate did not contain a key pair", "")

        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        key_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
        ct = tempfile.NamedTemporaryFile(delete=False, suffix=".pem", mode="wb")
        ct.write(cert_pem)
        ct.close()
        os.chmod(ct.name, 0o600)
        kt = tempfile.NamedTemporaryFile(delete=False, suffix=".pem", mode="wb")
        kt.write(key_pem)
        kt.close()
        os.chmod(kt.name, 0o600)
        return ct.name, kt.name

    def _cleanup(self, *paths: Optional[str]) -> None:
        for path in paths:
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def _parse(self, raw: str, http_status: int) -> AeatModeloResponse:
        code = str(http_status)
        desc = ""
        csv = None
        justificante = None

        stripped = (raw or "").strip()
        if stripped.startswith("<") or "Envelope" in stripped[:200]:
            try:
                root = ET.fromstring(raw)
            except ET.ParseError:
                desc = stripped[:500]
            else:
                fault = self._first(root, ("faultstring", "FaultString", "faultcode"))
                code = (
                    self._first(root, ("CodigoRespuesta", "Codigo", "codigo", "Code"))
                    or fault
                    or code
                )
                desc = (
                    self._first(
                        root,
                        (
                            "DescripcionRespuesta",
                            "Descripcion",
                            "descripcion",
                            "Description",
                            "faultstring",
                        ),
                    )
                    or desc
                )
                csv = self._first(root, ("CSV", "Csv", "csv"))
                justificante = self._first(
                    root,
                    ("NumeroJustificante", "Justificante", "justificante", "NRC"),
                )
        else:
            desc = stripped[:500]

        success_codes = {"0", "00", "0000", "200"}
        success = http_status < 400 and (
            (code in success_codes) or (http_status == 200 and not desc and csv)
        )
        if http_status == 200 and code in success_codes:
            success = True
        if http_status >= 400:
            success = False
            if not desc:
                desc = f"HTTP {http_status}"

        return AeatModeloResponse(
            success=success,
            code=code or str(http_status),
            description=desc or "",
            csv=csv,
            justificante=justificante,
            http_status=http_status,
            raw_response=raw or "",
        )

    @staticmethod
    def _first(root: ET.Element, names: tuple[str, ...]) -> Optional[str]:
        wanted = {n.lower() for n in names}
        for el in root.iter():
            local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
            if local.lower() in wanted:
                text = (el.text or "").strip()
                if text:
                    return text
        return None
