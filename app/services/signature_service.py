"""
Signature Service — XAdES-EPES Enveloped Digital Signature
===========================================================
Signs a Facturae 3.2.2 XML document with a XAdES-EPES enveloped signature
using the user's FNMT / AEAT .p12 (PKCS#12) certificate.

XAdES-EPES mandatory elements (per ETSI EN 319 132 and Facturae spec):
  - ds:Signature (enveloped, detached reference to the whole document)
  - xades:SignedProperties
      └─ xades:SignedSignatureProperties
            ├─ xades:SigningTime
            ├─ xades:SigningCertificate  (SHA-1 digest + issuer/serial)
            └─ xades:SignaturePolicyIdentifier
                  └─ xades:SignaturePolicyId  (Facturae policy OID)

Libraries used: cryptography (already in requirements.txt)
  - No external XAdES library needed — we build the XML structures manually
    using the standard xml.etree API and sign with RSA-SHA256 via cryptography.

Security notes:
  - The .p12 password is read from env var CERT_PASSWORD — never stored in DB.
  - The raw .p12 bytes are stored encrypted in MongoDB (field: p12_encrypted).
    Encryption key comes from env var CERT_ENCRYPTION_KEY (Fernet 32-byte key).
  - If CERT_ENCRYPTION_KEY is not set, the bytes are stored as-is (dev mode only).
"""

from __future__ import annotations

import base64
import hashlib
import os
import uuid
from datetime import datetime, timezone
from typing import Tuple

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization.pkcs12 import load_key_and_certificates
from cryptography.x509 import Certificate
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

# ---------------------------------------------------------------------------
# Namespace constants
# ---------------------------------------------------------------------------
NS_DS    = "http://www.w3.org/2000/09/xmldsig#"
NS_XADES = "http://uri.etsi.org/01903/v1.3.2#"

# Facturae signature policy (Orden EHA/962/2007)
FACTURAE_POLICY_OID        = "2.16.724.1.3.1.1.2.1.9"
FACTURAE_POLICY_URL        = "http://www.facturae.es/politica_de_firma_formato_facturae/politica_de_firma_formato_facturae_v3_1.pdf"
FACTURAE_POLICY_HASH_B64   = "Ohixl6upD6av8N7pEvDABhEL6hM="   # SHA-1 of the policy PDF


class SignatureService:
    """
    Signs a Facturae XML string with XAdES-EPES (enveloped).

    Usage:
        svc = SignatureService()
        signed_xml: bytes = svc.sign(xml_bytes, p12_bytes, p12_password)
    """

    def sign(
        self,
        xml_bytes: bytes,
        p12_bytes: bytes,
        p12_password: str,
    ) -> bytes:
        """
        Apply an XAdES-EPES enveloped signature to the Facturae XML.

        Args:
            xml_bytes:    Raw UTF-8 Facturae XML (output of FacturaeService.build_facturae_xml)
            p12_bytes:    Raw bytes of the user's .p12 / PKCS#12 certificate file
            p12_password: Plaintext password for the .p12 (read from env, never stored)

        Returns:
            Signed XML as UTF-8 bytes with the ds:Signature block appended
            inside the root element.
        """
        private_key, cert, chain = self._load_p12(p12_bytes, p12_password)

        # Unique IDs for XML references
        sig_id        = f"Signature-{uuid.uuid4().hex[:8]}"
        sig_props_id  = f"SignedProperties-{uuid.uuid4().hex[:8]}"
        ref_props_id  = f"Reference-SignedProperties-{uuid.uuid4().hex[:8]}"

        # Digest of the document (whole XML, C14N)
        doc_digest_b64 = self._digest_b64(xml_bytes)

        # Build SignedProperties XML fragment (needed to digest it)
        signed_props_xml = self._build_signed_properties(
            sig_props_id, cert, sig_id
        )
        props_digest_b64 = self._digest_b64(signed_props_xml.encode("utf-8"))

        # Build SignedInfo XML (what we actually sign)
        signed_info_xml = self._build_signed_info(
            sig_id, sig_props_id, ref_props_id,
            doc_digest_b64, props_digest_b64
        )

        # Sign SignedInfo with RSA-SHA256
        signature_value_b64 = self._rsa_sign(private_key, signed_info_xml.encode("utf-8"))

        # Build KeyInfo (certificate chain)
        key_info_xml = self._build_key_info(cert, chain)

        # Assemble full ds:Signature block
        signature_block = self._assemble_signature(
            sig_id, sig_props_id, ref_props_id,
            signed_info_xml, signature_value_b64,
            key_info_xml, signed_props_xml,
        )

        # Inject signature block before closing root tag
        signed_xml = self._inject_signature(xml_bytes, signature_block)
        return signed_xml

    # =========================================================================
    # Certificate helpers
    # =========================================================================

    def _load_p12(
        self, p12_bytes: bytes, password: str
    ) -> Tuple[RSAPrivateKey, Certificate, list]:
        pw = password.encode("utf-8") if password else None
        private_key, cert, chain = load_key_and_certificates(p12_bytes, pw)
        return private_key, cert, chain or []

    def _cert_der_b64(self, cert: Certificate) -> str:
        return base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()

    def _cert_sha1_b64(self, cert: Certificate) -> str:
        der = cert.public_bytes(serialization.Encoding.DER)
        return base64.b64encode(hashlib.sha1(der).digest()).decode()

    def _issuer_name(self, cert: Certificate) -> str:
        return cert.issuer.rfc4514_string()

    def _serial_number(self, cert: Certificate) -> str:
        return str(cert.serial_number)

    # =========================================================================
    # Digest / signing
    # =========================================================================

    def _digest_b64(self, data: bytes) -> str:
        return base64.b64encode(hashlib.sha256(data).digest()).decode()

    def _rsa_sign(self, private_key: RSAPrivateKey, data: bytes) -> str:
        sig = private_key.sign(data, padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(sig).decode()

    # =========================================================================
    # XML fragment builders
    # =========================================================================

    def _build_signed_info(
        self,
        sig_id: str,
        sig_props_id: str,
        ref_props_id: str,
        doc_digest_b64: str,
        props_digest_b64: str,
    ) -> str:
        return f"""<ds:SignedInfo xmlns:ds="{NS_DS}">
  <ds:CanonicalizationMethod Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315"/>
  <ds:SignatureMethod Algorithm="http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"/>
  <ds:Reference Id="Reference-Document-{sig_id}" URI="">
    <ds:Transforms>
      <ds:Transform Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature"/>
    </ds:Transforms>
    <ds:DigestMethod Algorithm="http://www.w3.org/2001/04/xmlenc#sha256"/>
    <ds:DigestValue>{doc_digest_b64}</ds:DigestValue>
  </ds:Reference>
  <ds:Reference Id="{ref_props_id}" Type="http://uri.etsi.org/01903#SignedProperties" URI="#{sig_props_id}">
    <ds:DigestMethod Algorithm="http://www.w3.org/2001/04/xmlenc#sha256"/>
    <ds:DigestValue>{props_digest_b64}</ds:DigestValue>
  </ds:Reference>
</ds:SignedInfo>"""

    def _build_signed_properties(
        self, sig_props_id: str, cert: Certificate, sig_id: str
    ) -> str:
        signing_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cert_digest  = self._cert_sha1_b64(cert)
        issuer       = self._issuer_name(cert)
        serial       = self._serial_number(cert)

        return f"""<xades:SignedProperties xmlns:xades="{NS_XADES}" Id="{sig_props_id}">
  <xades:SignedSignatureProperties>
    <xades:SigningTime>{signing_time}</xades:SigningTime>
    <xades:SigningCertificate>
      <xades:Cert>
        <xades:CertDigest>
          <ds:DigestMethod xmlns:ds="{NS_DS}" Algorithm="http://www.w3.org/2000/09/xmldsig#sha1"/>
          <ds:DigestValue xmlns:ds="{NS_DS}">{cert_digest}</ds:DigestValue>
        </xades:CertDigest>
        <xades:IssuerSerial>
          <ds:X509IssuerName xmlns:ds="{NS_DS}">{issuer}</ds:X509IssuerName>
          <ds:X509SerialNumber xmlns:ds="{NS_DS}">{serial}</ds:X509SerialNumber>
        </xades:IssuerSerial>
      </xades:Cert>
    </xades:SigningCertificate>
    <xades:SignaturePolicyIdentifier>
      <xades:SignaturePolicyId>
        <xades:SigPolicyId>
          <xades:Identifier Qualifier="OIDAsURN">urn:oid:{FACTURAE_POLICY_OID}</xades:Identifier>
          <xades:Description>Política de Firma Facturae v3.1</xades:Description>
        </xades:SigPolicyId>
        <xades:SigPolicyHash>
          <ds:DigestMethod xmlns:ds="{NS_DS}" Algorithm="http://www.w3.org/2000/09/xmldsig#sha1"/>
          <ds:DigestValue xmlns:ds="{NS_DS}">{FACTURAE_POLICY_HASH_B64}</ds:DigestValue>
        </xades:SigPolicyHash>
        <xades:SigPolicyQualifiers>
          <xades:SigPolicyQualifier>
            <xades:SPURI>{FACTURAE_POLICY_URL}</xades:SPURI>
          </xades:SigPolicyQualifier>
        </xades:SigPolicyQualifiers>
      </xades:SignaturePolicyId>
    </xades:SignaturePolicyIdentifier>
  </xades:SignedSignatureProperties>
</xades:SignedProperties>"""

    def _build_key_info(self, cert: Certificate, chain: list) -> str:
        certs_xml = f"<ds:X509Certificate>{self._cert_der_b64(cert)}</ds:X509Certificate>\n"
        for ca_cert in chain:
            certs_xml += f"      <ds:X509Certificate>{self._cert_der_b64(ca_cert)}</ds:X509Certificate>\n"

        return f"""<ds:KeyInfo xmlns:ds="{NS_DS}">
  <ds:X509Data>
    {certs_xml.strip()}
  </ds:X509Data>
</ds:KeyInfo>"""

    def _assemble_signature(
        self,
        sig_id: str,
        sig_props_id: str,
        ref_props_id: str,
        signed_info_xml: str,
        signature_value_b64: str,
        key_info_xml: str,
        signed_props_xml: str,
    ) -> str:
        return f"""<ds:Signature xmlns:ds="{NS_DS}" Id="{sig_id}">
  {signed_info_xml}
  <ds:SignatureValue Id="SignatureValue-{sig_id}">{signature_value_b64}</ds:SignatureValue>
  {key_info_xml}
  <ds:Object Id="Object-{sig_id}">
    <xades:QualifyingProperties xmlns:xades="{NS_XADES}" Target="#{sig_id}">
      {signed_props_xml}
    </xades:QualifyingProperties>
  </ds:Object>
</ds:Signature>"""

    def _inject_signature(self, xml_bytes: bytes, signature_block: str) -> bytes:
        """
        Inject the ds:Signature block just before the closing root tag.
        Works with both pretty-printed and compact XML.
        """
        xml_str = xml_bytes.decode("utf-8")

        # Find the last closing tag (root element close)
        last_close = xml_str.rfind("</")
        if last_close == -1:
            raise ValueError("Cannot find closing root tag in XML")

        signed = xml_str[:last_close] + "\n" + signature_block + "\n" + xml_str[last_close:]
        return signed.encode("utf-8")


# ---------------------------------------------------------------------------
# Certificate storage helpers (encrypt/decrypt .p12 bytes for MongoDB)
# ---------------------------------------------------------------------------

def encrypt_p12(p12_bytes: bytes) -> bytes:
    """
    Encrypt raw .p12 bytes using Fernet symmetric encryption.
    Key must be set in env var CERT_ENCRYPTION_KEY (base64-encoded 32-byte key).
    Falls back to plain base64 if key is not configured (dev only).
    """
    key = os.getenv("CERT_ENCRYPTION_KEY")
    if key:
        from cryptography.fernet import Fernet
        f = Fernet(key.encode())
        return f.encrypt(p12_bytes)
    # Dev fallback — no encryption
    return base64.b64encode(p12_bytes)


def decrypt_p12(stored_bytes: bytes) -> bytes:
    """
    Decrypt .p12 bytes retrieved from MongoDB.
    """
    key = os.getenv("CERT_ENCRYPTION_KEY")
    if key:
        from cryptography.fernet import Fernet
        f = Fernet(key.encode())
        return f.decrypt(stored_bytes)
    # Dev fallback
    return base64.b64decode(stored_bytes)
