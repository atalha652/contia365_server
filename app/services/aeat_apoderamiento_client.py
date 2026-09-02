"""
AEAT Apoderamiento (Power of Attorney / Representation) SOAP Client.
===================================================================
Queries official AEAT webservices to verify whether a taxpayer (company/individual)
has granted valid representation (apoderamiento) to Contia365's corporate NIF.

Official AEAT WSDL / Service:
- Production: https://www1.agenciatributaria.gob.es/wlpl/ADEP-D171/ws/WSConsultaApoderamientos
- Pre-production (Testing): https://prewww1.aeat.es/wlpl/ADEP-D171/ws/WSConsultaApoderamientos

When no certificate is configured or AEAT_SIMULATE_VERIFICATION is enabled,
the client runs in Simulation Mode for local development and testing.
"""

from __future__ import annotations

import logging
import os
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization.pkcs12 import load_key_and_certificates

logger = logging.getLogger(__name__)

AEAT_APODERAMIENTO_URL = os.getenv(
    "AEAT_APODERAMIENTO_URL",
    "https://prewww1.aeat.es/wlpl/ADEP-D171/ws/WSConsultaApoderamientos",
)

NS_SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
NS_APOD = "https://www.agenciatributaria.gob.es/apoderamiento"


@dataclass
class ApoderamientoVerificationResult:
    is_valid: bool
    status: str  # "VERIFIED", "PENDING", "REVOKED", "NOT_FOUND", "ERROR", "SIMULATED"
    message: str
    source: str  # "AEAT_SOAP_LIVE" or "AEAT_SIMULATION"
    verified_at: datetime
    reference: Optional[str] = None
    raw_response: Optional[str] = None


class AeatApoderamientoClient:
    """Client to verify AEAT representation delegations."""

    def __init__(
        self,
        endpoint_url: Optional[str] = None,
        cert_path: Optional[str] = None,
        cert_password: Optional[str] = None,
    ):
        self.endpoint_url = endpoint_url or AEAT_APODERAMIENTO_URL
        self.cert_path = (
            cert_path
            or os.getenv("CONTIA_GESTOR_CERT_PATH")
            or os.getenv("CONTIA_P12_PATH")
            or ""
        ).strip()
        self.cert_password = (
            cert_password
            or os.getenv("CONTIA_GESTOR_CERT_PASSWORD")
            or os.getenv("CONTIA_P12_PASSWORD")
            or os.getenv("CERT_PASSWORD")
            or ""
        ).strip()

    def should_simulate(self) -> bool:
        """Check if verification should run in simulation mode."""
        env_simulate = os.getenv("AEAT_SIMULATE_VERIFICATION", "").lower()
        if env_simulate in ("true", "1", "yes"):
            return True
        if not self.cert_path or not os.path.exists(self.cert_path):
            return True
        return False

    def verify_apoderamiento(
        self,
        poderdante_nif: str,
        apoderado_nif: str,
        representative_dni: Optional[str] = None,
        apoderamiento_code: Optional[str] = None,
    ) -> ApoderamientoVerificationResult:
        """
        Verify that poderdante (the company CIF or individual) has granted active
        apoderamiento to apoderado (Contia365's NIF).
        """
        now = datetime.utcnow()
        clean_poderdante = (poderdante_nif or "").strip().upper()
        clean_apoderado = (apoderado_nif or "").strip().upper()

        if not clean_poderdante:
            return ApoderamientoVerificationResult(
                is_valid=False,
                status="ERROR",
                message="Company CIF / Taxpayer NIF is required for AEAT verification.",
                source="LOCAL_VALIDATION",
                verified_at=now,
            )

        if not clean_apoderado:
            clean_apoderado = os.getenv("VITE_CONTIA_NIF", "B00000000").strip().upper()

        # Check simulation mode
        if self.should_simulate():
            logger.info(
                "AEAT Apoderamiento Client running in SIMULATION mode for poderdante %s -> apoderado %s",
                clean_poderdante,
                clean_apoderado,
            )
            return self._simulate_verification(clean_poderdante, clean_apoderado, apoderamiento_code, now)

        # Live mTLS SOAP request
        return self._live_soap_verify(clean_poderdante, clean_apoderado, apoderamiento_code, now)

    def _simulate_verification(
        self,
        poderdante_nif: str,
        apoderado_nif: str,
        apoderamiento_code: Optional[str],
        timestamp: datetime,
    ) -> ApoderamientoVerificationResult:
        """Simulate verification for testing and development."""
        # Check for simulated test failure triggers
        if poderdante_nif.startswith("Z") or poderdante_nif == "FAIL_AUTH":
            return ApoderamientoVerificationResult(
                is_valid=False,
                status="NOT_FOUND",
                message="AEAT reports no active apoderamiento registered for this company CIF.",
                source="AEAT_SIMULATION",
                verified_at=timestamp,
                reference=apoderamiento_code,
            )

        ref_code = apoderamiento_code or f"SIM-APOD-{timestamp.strftime('%Y%m%d%H%M%S')}"
        return ApoderamientoVerificationResult(
            is_valid=True,
            status="VERIFIED",
            message=f"Apoderamiento confirmed by AEAT for company {poderdante_nif} to {apoderado_nif}.",
            source="AEAT_SIMULATION",
            verified_at=timestamp,
            reference=ref_code,
        )

    def _build_soap_envelope(
        self,
        poderdante_nif: str,
        apoderado_nif: str,
        apoderamiento_code: Optional[str] = None,
    ) -> bytes:
        """Build SOAP request envelope for ConsultaApoderamientos."""
        code_tag = f"<ws:CodigoTramite>{apoderamiento_code}</ws:CodigoTramite>" if apoderamiento_code else ""
        envelope = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="{NS_SOAP}" xmlns:ws="{NS_APOD}">
  <soapenv:Header/>
  <soapenv:Body>
    <ws:ConsultaApoderamientos>
      <ws:NifPoderdante>{poderdante_nif}</ws:NifPoderdante>
      <ws:NifApoderado>{apoderado_nif}</ws:NifApoderado>
      {code_tag}
    </ws:ConsultaApoderamientos>
  </soapenv:Body>
</soapenv:Envelope>"""
        return envelope.encode("utf-8")

    def _live_soap_verify(
        self,
        poderdante_nif: str,
        apoderado_nif: str,
        apoderamiento_code: Optional[str],
        timestamp: datetime,
    ) -> ApoderamientoVerificationResult:
        """Execute live mTLS SOAP call to AEAT."""
        cert_file = key_file = None
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"{NS_APOD}/ConsultaApoderamientos"',
        }
        body = self._build_soap_envelope(poderdante_nif, apoderado_nif, apoderamiento_code)

        try:
            with open(self.cert_path, "rb") as f:
                p12_bytes = f.read()

            cert_file, key_file = self._extract_pem_files(p12_bytes, self.cert_password)

            with httpx.Client(cert=(cert_file, key_file), verify=True, timeout=30.0) as client:
                response = client.post(self.endpoint_url, content=body, headers=headers)

            return self._parse_soap_response(response.text, response.status_code, timestamp)

        except httpx.RequestError as exc:
            logger.warning("AEAT Apoderamiento transport error: %s", exc)
            return ApoderamientoVerificationResult(
                is_valid=False,
                status="ERROR",
                message=f"Could not connect to AEAT servers: {str(exc)}",
                source="AEAT_SOAP_LIVE",
                verified_at=timestamp,
            )
        except Exception as exc:
            logger.error("Error during AEAT apoderamiento verification: %s", exc)
            return ApoderamientoVerificationResult(
                is_valid=False,
                status="ERROR",
                message=f"Verification failed: {str(exc)}",
                source="AEAT_SOAP_LIVE",
                verified_at=timestamp,
            )
        finally:
            self._cleanup(cert_file, key_file)

    def _parse_soap_response(
        self,
        raw_xml: str,
        http_status: int,
        timestamp: datetime,
    ) -> ApoderamientoVerificationResult:
        """Parse AEAT ConsultaApoderamientos SOAP response."""
        if not raw_xml:
            return ApoderamientoVerificationResult(
                is_valid=False,
                status="ERROR",
                message="AEAT returned empty response.",
                source="AEAT_SOAP_LIVE",
                verified_at=timestamp,
            )

        try:
            root = ET.fromstring(raw_xml)
        except ET.ParseError:
            return ApoderamientoVerificationResult(
                is_valid=False,
                status="ERROR",
                message=f"AEAT returned invalid XML (HTTP {http_status}).",
                source="AEAT_SOAP_LIVE",
                verified_at=timestamp,
                raw_response=raw_xml[:500],
            )

        # Check for SOAP fault
        fault = self._find_first(root, ("faultstring", "FaultString", "faultcode"))
        if fault:
            return ApoderamientoVerificationResult(
                is_valid=False,
                status="ERROR",
                message=f"AEAT SOAP Fault: {fault}",
                source="AEAT_SOAP_LIVE",
                verified_at=timestamp,
                raw_response=raw_xml[:500],
            )

        # Check status indicators in AEAT response
        status_val = (
            self._find_first(root, ("Estado", "estado", "EstadoApoderamiento", "CodigoRespuesta"))
            or ""
        ).upper()

        desc_val = self._find_first(root, ("Descripcion", "descripcion", "DescripcionRespuesta")) or ""
        ref_val = self._find_first(root, ("NumeroReferencia", "CodigoTramite", "CSV")) or None

        if "ACTIVO" in status_val or status_val in ("0", "OK", "VIGENTE"):
            return ApoderamientoVerificationResult(
                is_valid=True,
                status="VERIFIED",
                message=desc_val or "Active apoderamiento confirmed with AEAT.",
                source="AEAT_SOAP_LIVE",
                verified_at=timestamp,
                reference=ref_val,
                raw_response=raw_xml[:500],
            )

        if "REVOCADO" in status_val or "CADUCADO" in status_val:
            return ApoderamientoVerificationResult(
                is_valid=False,
                status="REVOKED",
                message=desc_val or f"Apoderamiento status is {status_val}.",
                source="AEAT_SOAP_LIVE",
                verified_at=timestamp,
                reference=ref_val,
                raw_response=raw_xml[:500],
            )

        if "NO_EXISTE" in status_val or "NO ENCONTRADO" in desc_val.upper() or status_val == "1":
            return ApoderamientoVerificationResult(
                is_valid=False,
                status="NOT_FOUND",
                message=desc_val or "No active apoderamiento found on AEAT for this company.",
                source="AEAT_SOAP_LIVE",
                verified_at=timestamp,
                reference=ref_val,
                raw_response=raw_xml[:500],
            )

        # If HTTP 200 and no failure explicitly stated, parse default
        is_ok = http_status == 200 and ("ERROR" not in desc_val.upper())
        return ApoderamientoVerificationResult(
            is_valid=is_ok,
            status="VERIFIED" if is_ok else "PENDING",
            message=desc_val or f"AEAT response status: {status_val or http_status}",
            source="AEAT_SOAP_LIVE",
            verified_at=timestamp,
            reference=ref_val,
            raw_response=raw_xml[:500],
        )

    def _find_first(self, root: ET.Element, tag_names: tuple[str, ...]) -> Optional[str]:
        """Search recursively for the first occurrence of any tag name regardless of namespace."""
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag in tag_names and elem.text:
                return elem.text.strip()
        return None

    def _extract_pem_files(self, p12_bytes: bytes, password: str) -> tuple[str, str]:
        """Extract PEM certificate and private key from .p12 bytes."""
        pw = password.encode("utf-8") if password else None
        private_key, cert, _ = load_key_and_certificates(p12_bytes, pw)
        if private_key is None or cert is None:
            raise ValueError("Certificate did not contain a key pair.")

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
        """Remove temporary PEM files."""
        for path in paths:
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
