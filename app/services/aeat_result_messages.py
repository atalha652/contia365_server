"""Readable AEAT / Contia modelo-submission messages for T8."""

from typing import Optional

# Known AEAT and Contia client codes. Unknown codes keep the raw description.
AEAT_RESULT_MESSAGES = {
    "0": "AEAT accepted the declaration.",
    "00": "AEAT accepted the declaration.",
    "0000": "AEAT accepted the declaration.",
    "200": "AEAT accepted the declaration.",
    "CERT": "The digital certificate could not be opened. Check the .p12 file and password.",
    "CONFIG": "Modelo 303 submission is not configured. AEAT_MODELO_SUBMIT_URL is missing or points at VeriFactu.",
    "TRANSPORT": "Could not reach the AEAT modelo service. Try again or check the sandbox endpoint.",
}


def readable_aeat_message(code: Optional[str], description: Optional[str] = None) -> str:
    key = str(code or "").strip()
    mapped = AEAT_RESULT_MESSAGES.get(key) or AEAT_RESULT_MESSAGES.get(key.upper())
    fallback = (description or "").strip()
    if mapped:
        return mapped
    if fallback:
        return fallback
    if key:
        return f"AEAT returned code {key}."
    return "No AEAT message was returned."


def enrich_aeat_result(result: Optional[dict]) -> Optional[dict]:
    if not result or not isinstance(result, dict):
        return result
    enriched = dict(result)
    enriched["message"] = readable_aeat_message(
        enriched.get("code"),
        enriched.get("description"),
    )
    enriched["has_justificante"] = bool(
        str(enriched.get("justificante") or "").strip()
        or str(enriched.get("csv") or "").strip()
    )
    return enriched
