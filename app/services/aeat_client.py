"""
AEAT VeriFactu SOAP Client
===========================
Correct SOAP structure per official AEAT WSDL:

  <soap:Envelope>
    <soap:Body>
      <sfLR:RegFactuSistemaFacturacion>
        <sfLR:Cabecera>          <- direct child of RegFactuSistemaFacturacion
          <sf:ObligadoEmision>
            <sf:NombreRazon>...</sf:NombreRazon>
            <sf:NIF>...</sf:NIF>
          </sf:ObligadoEmision>
        </sfLR:Cabecera>
        <sf:RegistroFacturacion>   <- direct child of RegFactuSistemaFacturacion
          <sf:IDFactura>...</sf:IDFactura>
          ...
        </sf:RegistroFacturacion>
      </sfLR:RegFactuSistemaFacturacion>
    </soap:Body>
  </soap:Envelope>
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

AEAT_SUBMIT_URL = os.getenv(
    "AEAT_SUBMIT_URL",
    "https://prewww1.aeat.es/wlpl/TIKE-CONT/ws/SistemaFacturacion/VerifactuSOAP",
)

NS_SLR = "https://www2.agenciatributaria.gob.es/static_files/common/internet/dep/aplicaciones/es/aeat/tike/cont/ws/SuministroLR.xsd"
NS_SF  = "https://www2.agenciatributaria.gob.es/static_files/common/internet/dep/aplicaciones/es/aeat/tike/cont/ws/SuministroInformacion.xsd"

SOAP_ACTION = f"{NS_SLR}/RegFactuSistemaFacturacion"

_SOAP_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:sfLR="__NS_SLR__"
               xmlns:sf="__NS_SF__">
  <soap:Header/>
  <soap:Body>
    <sfLR:RegFactuSistemaFacturacion>
      <sfLR:Cabecera>
        <sf:IDVersion>1.0</sf:IDVersion>
        <sf:ObligadoEmision>
          <sf:NombreRazon>__SELLER_NAME__</sf:NombreRazon>
          <sf:NIF>__SELLER_NIF__</sf:NIF>
        </sf:ObligadoEmision>
      </sfLR:Cabecera>
      <sfLR:RegistroFacturacion>
__REGISTRO_CHILDREN__
      </sfLR:RegistroFacturacion>
    </sfLR:RegFactuSistemaFacturacion>
  </soap:Body>
</soap:Envelope>"""


@dataclass
class AeatResponse:
    success: bool
    code: str
    description: str
    csv: Optional[str]
    raw_response: str


class AeatSubmissionError(Exception):
    def __init__(self, code: str, description: str, raw: str):
        super().__init__(f"AEAT error {code}: {description}")
        self.code = code
        self.description = description
        self.raw = raw


class AeatClient:

    def submit(
        self,
        signed_xml_bytes: bytes,
        p12_bytes: bytes,
        p12_password: str,
        seller_nif: str,
        seller_name: str,
    ) -> AeatResponse:
        """
        Embed the signed RegistroFacturacion XML directly inside the SOAP
        RegistroFacturacion wrapper. The inner element keeps its own namespace
        declaration — AEAT resolves by URI, not prefix.
        """
        registro_children = self._clean_xml(signed_xml_bytes.decode("utf-8"))

        soap_body = (
            _SOAP_TEMPLATE
            .replace("__NS_SLR__", NS_SLR)
            .replace("__NS_SF__", NS_SF)
            .replace("__SELLER_NAME__", self._esc(seller_name))
            .replace("__SELLER_NIF__", self._esc(seller_nif))
            .replace("__REGISTRO_CHILDREN__", registro_children)
        )

        logger.info("=== AEAT SOAP REQUEST ===\n%s\n=== END REQUEST ===", soap_body)

        cert_file = key_file = None
        try:
            cert_file, key_file = self._extract_pem_files(p12_bytes, p12_password)

            with httpx.Client(cert=(cert_file, key_file), verify=True, timeout=60.0) as client:
                resp = client.post(
                    AEAT_SUBMIT_URL,
                    content=soap_body.encode("utf-8"),
                    headers={
                        "Content-Type": "text/xml; charset=utf-8",
                        "SOAPAction": f'"{SOAP_ACTION}"',
                    },
                )

            logger.info("AEAT HTTP %s:\n%s", resp.status_code, resp.text)
            raw = resp.text

        finally:
            self._cleanup(cert_file, key_file)

        return self._parse(raw)

    # -------------------------------------------------------------------------

    def _clean_xml(self, xml_str: str) -> str:
        """
        Parse the RegistroFacturacion XML and return its inner children
        serialised with the sf: namespace prefix for embedding inside
        <sf:RegistroFacturacion> in the SOAP body.

        Registers namespace prefixes before serialising so ET uses sf:/sfLR:
        instead of emitting xmlns:ns0=... on every element.
        """
        # Register prefixes so ET.tostring uses them instead of ns0, ns1, etc.
        ET.register_namespace("sf", NS_SF)
        ET.register_namespace("sfLR", NS_SLR)

        xml_str = xml_str.strip()
        root = ET.fromstring(xml_str)

        parts = []
        for child in root:
            child_str = ET.tostring(child, encoding="unicode")
            # Strip any redundant namespace declarations that are already on
            # the envelope root — avoids xmlns:sf="..." on every inner element.
            child_str = child_str.replace(f' xmlns:sf="{NS_SF}"', "")
            child_str = child_str.replace(f' xmlns:sfLR="{NS_SLR}"', "")
            parts.append(child_str)

        inner = "\n".join(parts)
        return "\n".join("        " + line for line in inner.splitlines() if line.strip())

    def _esc(self, v: str) -> str:
        return v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _extract_pem_files(self, p12_bytes: bytes, password: str) -> tuple[str, str]:
        pw = password.encode("utf-8") if password else None
        private_key, cert, _ = load_key_and_certificates(p12_bytes, pw)

        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        key_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )

        ct = tempfile.NamedTemporaryFile(delete=False, suffix=".pem", mode="wb")
        ct.write(cert_pem); ct.close(); os.chmod(ct.name, 0o600)

        kt = tempfile.NamedTemporaryFile(delete=False, suffix=".pem", mode="wb")
        kt.write(key_pem); kt.close(); os.chmod(kt.name, 0o600)

        return ct.name, kt.name

    def _cleanup(self, *paths: Optional[str]) -> None:
        for p in paths:
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def _parse(self, raw: str) -> AeatResponse:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as e:
            logger.error("Cannot parse AEAT response: %s\n%s", e, raw)
            raise AeatSubmissionError("PARSE_ERROR", str(e), raw)

        fault_code   = self._text(root, "faultcode")
        fault_string = self._text(root, "faultstring")
        if fault_code or fault_string:
            logger.error("SOAP Fault — %s: %s", fault_code, fault_string)
            raise AeatSubmissionError(fault_code or "SOAP_FAULT", fault_string or "", raw)

        code = self._text(root, "CodigoRespuesta") or ""
        desc = self._text(root, "DescripcionRespuesta") or ""
        csv  = self._text(root, "CSV")

        logger.info("AEAT response — code=%r desc=%r csv=%r", code, desc, csv)

        if code == "0":
            return AeatResponse(True, code, desc or "Factura Correctamente Recibida", csv, raw)

        raise AeatSubmissionError(
            code or "UNKNOWN",
            desc or f"No recognisable response. Raw: {raw[:500]}",
            raw,
        )

    @staticmethod
    def _text(root: ET.Element, tag: str) -> Optional[str]:
        for el in root.iter():
            local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
            if local == tag:
                return (el.text or "").strip() or None
        return None
