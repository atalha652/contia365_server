"""
Certificate Service — Secure .p12 / PKCS#12 storage for individual users
=========================================================================
Handles upload, encryption, and retrieval of the user's FNMT digital
certificate so Contia365 can sign and submit AEAT filings on their behalf.

Security model:
  - The .p12 file is validated (password verified) on upload.
  - The raw bytes are immediately encrypted with AES-128-CBC (Fernet/AESGCM)
    using the master key from env var CERT_ENCRYPTION_KEY.
  - Only the encrypted blob is stored in MongoDB — never the raw .p12.
  - The plaintext password is NOT stored. We only store the fact that the
    certificate was validated successfully.
  - On signing, the decrypted bytes are loaded into memory, used, then
    immediately discarded — never written to disk.

Dev mode (no CERT_ENCRYPTION_KEY set):
  - Bytes are stored as base64 without encryption.
  - A warning is logged. Never deploy this way to production.
"""

from __future__ import annotations

import base64
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from cryptography.hazmat.primitives.serialization.pkcs12 import load_key_and_certificates
from cryptography.x509 import Certificate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Encryption helpers
# ---------------------------------------------------------------------------

def _get_fernet():
    """Return a Fernet instance using CERT_ENCRYPTION_KEY env var."""
    key = os.getenv("CERT_ENCRYPTION_KEY")
    if not key:
        return None
    from cryptography.fernet import Fernet
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_p12(raw_bytes: bytes) -> bytes:
    """
    Encrypt raw .p12 bytes for storage in MongoDB.
    Returns encrypted bytes (Fernet token) or base64 in dev mode.
    """
    f = _get_fernet()
    if f:
        return f.encrypt(raw_bytes)
    # Dev fallback — log a warning
    logger.warning(
        "CERT_ENCRYPTION_KEY is not set. Storing certificate as base64 (dev mode only)."
    )
    return base64.b64encode(raw_bytes)


def decrypt_p12(stored_bytes: bytes) -> bytes:
    """
    Decrypt stored bytes back to raw .p12 bytes.
    Used when signing a tax filing.
    """
    f = _get_fernet()
    if f:
        return f.decrypt(stored_bytes)
    # Dev fallback
    return base64.b64decode(stored_bytes)


# ---------------------------------------------------------------------------
# Certificate validation
# ---------------------------------------------------------------------------

class CertificateInfo:
    """Basic info extracted from a validated .p12 certificate."""

    def __init__(self, cert: Certificate):
        self.subject = cert.subject.rfc4514_string()
        self.issuer = cert.issuer.rfc4514_string()
        self.serial_number = str(cert.serial_number)
        self.valid_from: datetime = cert.not_valid_before_utc
        self.valid_until: datetime = cert.not_valid_after_utc

    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) > self.valid_until

    def to_dict(self) -> dict:
        return {
            "subject": self.subject,
            "issuer": self.issuer,
            "serial_number": self.serial_number,
            "valid_from": self.valid_from.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "is_expired": self.is_expired(),
        }


def validate_p12(p12_bytes: bytes, password: str) -> CertificateInfo:
    """
    Validate a .p12 file by attempting to load it with the given password.

    Raises:
        ValueError: if the password is wrong or the file is not a valid .p12.

    Returns:
        CertificateInfo with subject, validity dates, etc.
    """
    try:
        pw = password.encode("utf-8") if password else None
        _, cert, _ = load_key_and_certificates(p12_bytes, pw)
        if cert is None:
            raise ValueError("No certificate found in the .p12 file.")
        return CertificateInfo(cert)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(
            "Could not read the certificate. "
            "Make sure the file is a valid .p12/.pfx and the password is correct."
        ) from exc


# ---------------------------------------------------------------------------
# High-level save / load
# ---------------------------------------------------------------------------

def save_certificate(
    users_collection,
    user_id: str,
    p12_bytes: bytes,
    password: str,
) -> dict:
    """
    Validate, encrypt, and store the user's .p12 certificate.

    Returns the certificate metadata dict (no private key material).

    Raises ValueError if validation fails.
    """
    # 1. Validate — raises ValueError on bad password / bad file
    cert_info = validate_p12(p12_bytes, password)

    if cert_info.is_expired():
        raise ValueError(
            f"This certificate expired on {cert_info.valid_until.strftime('%Y-%m-%d')}. "
            "Please upload a valid, non-expired certificate."
        )

    # 2. Encrypt raw bytes
    encrypted = encrypt_p12(p12_bytes)

    # 3. Persist (upsert so re-upload replaces the old cert)
    from bson import ObjectId
    now = datetime.utcnow()
    cert_doc = {
        "p12_encrypted": encrypted,
        "certificate_info": cert_info.to_dict(),
        "certificate_uploaded_at": now,
        "certificate_valid_until": cert_info.valid_until,
    }
    users_collection.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {**cert_doc, "updated_at": now}},
    )

    # Return metadata only — never the encrypted bytes
    return {
        "uploaded_at": now.isoformat(),
        **cert_info.to_dict(),
    }


def get_certificate_status(user: dict) -> Optional[dict]:
    """
    Return certificate metadata for the user, or None if not uploaded.
    Never returns the encrypted bytes.
    """
    info = user.get("certificate_info")
    if not info:
        return None
    return {
        "uploaded": True,
        "uploaded_at": user.get("certificate_uploaded_at"),
        **info,
    }


def load_certificate_bytes(user: dict) -> bytes:
    """
    Decrypt and return raw .p12 bytes from the user document.
    Used only at signing time — call decrypt_p12 and discard the result
    immediately after use.

    Raises ValueError if no certificate is stored.
    """
    raw = user.get("p12_encrypted")
    if not raw:
        raise ValueError("No digital certificate found for this user.")
    return decrypt_p12(raw)
