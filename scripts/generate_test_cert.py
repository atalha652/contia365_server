"""
Generate a test PKCS#12 (.p12) digital certificate for Contia Gestor mode testing.
Uses standard Python cryptography library.
"""

import os
from datetime import datetime, timezone, timedelta
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12

def generate_p12(out_path: str, password: str, nif: str = "12345678Z", org_name: str = "Contia365 Gestor Test"):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    # 1. Generate RSA Key Pair
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    # 2. Build X509 Certificate
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "ES"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, org_name),
        x509.NameAttribute(NameOID.COMMON_NAME, f"{org_name} - {nif}"),
        x509.NameAttribute(NameOID.SERIAL_NUMBER, nif),
    ])

    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .sign(private_key, hashes.SHA256())
    )

    # 3. Serialize to PKCS#12 (.p12)
    p12_bytes = pkcs12.serialize_key_and_certificates(
        name=b"Contia Gestor Test Certificate",
        key=private_key,
        cert=cert,
        cas=None,
        encryption_algorithm=serialization.BestAvailableEncryption(password.encode("utf-8")),
    )

    with open(out_path, "wb") as f:
        f.write(p12_bytes)

    print(f"SUCCESS: Generated test PKCS#12 certificate!")
    print(f"   Path:     {out_path}")
    print(f"   Password: {password}")
    print(f"   Test NIF: {nif}")


if __name__ == "__main__":
    target = os.path.abspath("certs/contia_gestor.p12")
    generate_p12(target, password="1234")
