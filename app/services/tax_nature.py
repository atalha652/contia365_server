"""
Structured tax nature for Spanish invoices.

Classification must use these fields (plus amounts), not invoice wording.
Description stays commercial text. OCR may seed the fields once; after that
the stored values win until the user edits them.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

OPERATION_TYPES = (
    "general",
    "isp",
    "intra",
    "import",
    "recargo",
    "used_goods",
    "investment",
)

WITHHOLDING_TYPES = (
    "none",
    "irpf_work",
    "professional",
    "rental",
)

_OPERATION_ALIASES = {
    "rg": "general",
    "general iva": "general",
    "regimen general": "general",
    "régimen general": "general",
    "reverse charge": "isp",
    "inversion del sujeto pasivo": "isp",
    "inversión del sujeto pasivo": "isp",
    "intra-community": "intra",
    "intracomunitaria": "intra",
    "importacion": "import",
    "importación": "import",
    "recargo de equivalencia": "recargo",
    "rebu": "used_goods",
    "bienes usados": "used_goods",
}

_WITHHOLDING_ALIASES = {
    "": "none",
    "no": "none",
    "work": "irpf_work",
    "trabajo": "irpf_work",
    "employee": "irpf_work",
    "honorarios": "professional",
    "profesional": "professional",
    "alquiler": "rental",
    "arrendamiento": "rental",
    "rent": "rental",
}


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def normalize_operation_type(value: Any) -> Optional[str]:
    raw = _norm(value)
    if raw in OPERATION_TYPES:
        return raw
    return _OPERATION_ALIASES.get(raw)


def normalize_withholding_type(value: Any) -> Optional[str]:
    raw = _norm(value)
    if raw in WITHHOLDING_TYPES:
        return raw
    return _WITHHOLDING_ALIASES.get(raw)


def _money(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _invoice_blob(invoice_data: dict, ocr_text: str = "") -> str:
    totals = (invoice_data or {}).get("totals") or {}
    items = (invoice_data or {}).get("items") or []
    descriptions = " ".join(str(item.get("description") or "") for item in items)
    return " ".join((
        ocr_text or "",
        str(invoice_data.get("description") or ""),
        str(totals.get("vat_regime") or ""),
        descriptions,
    )).lower()


def infer_operation_type(
    invoice_data: Optional[dict],
    *,
    ocr_text: str = "",
    seed_from_text: bool = False,
) -> str:
    data = invoice_data or {}
    totals = data.get("totals") or {}
    stored = normalize_operation_type(
        data.get("operation_type") or totals.get("operation_type")
    )
    if stored:
        return stored

    regime = normalize_operation_type(
        totals.get("vat_regime") or data.get("vat_regime") or data.get("vat_special")
    )
    if regime:
        return regime

    recargo_amount = _money(totals.get("recargo_amount") or totals.get("RECARGO_amount"))
    recargo_rate = _money(totals.get("recargo_rate") or totals.get("RECARGO_rate"))
    if recargo_amount > 0 or recargo_rate > 0:
        return "recargo"

    if seed_from_text:
        blob = _invoice_blob(data, ocr_text)
        if re.search(r"bienes?\s+usados|\brebu\b|regimen especial de bienes", blob):
            return "used_goods"
        if re.search(r"inversi[oó]n\s+del\s+sujeto\s+pasivo|\bisp\b|reverse\s+charge", blob):
            return "isp"
        if re.search(r"intracomunitar|intra-communit|intra community", blob):
            return "intra"
        if re.search(r"importaci[oó]n|\bdua\b|aduana", blob):
            return "import"
        if re.search(r"bien(?:es)?\s+de\s+inversi[oó]n", blob):
            return "investment"
        if re.search(r"recargo(?:\s+de)?\s+equivalencia", blob):
            return "recargo"
    return "general"


def infer_withholding_type(
    invoice_data: Optional[dict],
    *,
    ocr_text: str = "",
    seed_from_text: bool = False,
) -> str:
    data = invoice_data or {}
    totals = data.get("totals") or {}
    stored = normalize_withholding_type(
        data.get("withholding_type") or totals.get("withholding_type")
    )
    if stored:
        return stored

    if seed_from_text:
        blob = _invoice_blob(data, ocr_text)
        if re.search(r"alquiler|arrendamiento|\brent\b|inmueble", blob):
            return "rental"
        if re.search(
            r"honorarios|servicios?\s+profesionales?|prestaci[oó]n\s+de\s+servicios?"
            r"|freelance|consulting",
            blob,
        ):
            return "professional"

    irpf_amount = _money(totals.get("IRPF_amount") or totals.get("irpf_amount"))
    irpf_rate = _money(totals.get("IRPF_rate") or totals.get("irpf_rate"))
    if irpf_amount > 0 or irpf_rate > 0:
        return "irpf_work"
    return "none"


def resolve_tax_nature(
    invoice_data: Optional[dict],
    *,
    ocr_text: str = "",
    seed_from_text: bool = False,
) -> Dict[str, str]:
    return {
        "operation_type": infer_operation_type(
            invoice_data, ocr_text=ocr_text, seed_from_text=seed_from_text
        ),
        "withholding_type": infer_withholding_type(
            invoice_data, ocr_text=ocr_text, seed_from_text=seed_from_text
        ),
    }


def apply_tax_nature(invoice_data: Optional[dict], nature: Dict[str, str]) -> dict:
    data = dict(invoice_data or {})
    data["operation_type"] = nature["operation_type"]
    data["withholding_type"] = nature["withholding_type"]
    totals = dict(data.get("totals") or {})
    totals["vat_regime"] = nature["operation_type"]
    data["totals"] = totals
    return data


def has_structured_tax_nature(invoice_data: Optional[dict]) -> bool:
    data = invoice_data or {}
    return bool(
        normalize_operation_type(data.get("operation_type"))
        and normalize_withholding_type(data.get("withholding_type"))
    )


def vat_present(invoice_data: Optional[dict]) -> bool:
    totals = (invoice_data or {}).get("totals") or {}
    vat_amount = _money(totals.get("VAT_amount") or totals.get("vat_amount"))
    vat_rate = _money(totals.get("VAT_rate") or totals.get("vat_rate"))
    raw_total = _money(totals.get("total") or totals.get("base"))
    total_with_tax = _money(totals.get("Total_with_Tax") or totals.get("total_with_tax"))
    monetary_vat = vat_amount > 0 and abs(vat_amount - vat_rate) > 0.009
    implicit_vat = total_with_tax > raw_total > 0
    return monetary_vat or implicit_vat or vat_amount > vat_rate > 0


def irpf_present(invoice_data: Optional[dict]) -> bool:
    totals = (invoice_data or {}).get("totals") or {}
    return (
        _money(totals.get("IRPF_amount") or totals.get("irpf_amount")) > 0
        or _money(totals.get("IRPF_rate") or totals.get("irpf_rate")) > 0
    )


def signals_from_nature(
    invoice_data: Optional[dict],
    nature: Dict[str, str],
) -> Dict[str, Any]:
    data = invoice_data or {}
    tx_type = _norm(data.get("transaction_type"))
    withholding = nature["withholding_type"]
    return {
        "has_vat": vat_present(data) or nature["operation_type"] != "general",
        "has_irpf": irpf_present(data) or withholding != "none",
        "is_rent": withholding == "rental",
        "is_professional": withholding in ("professional", "irpf_work"),
        "is_income": tx_type in ("income", "credit"),
        "is_expense": tx_type in ("expense", "debit"),
        "operation_type": nature["operation_type"],
        "withholding_type": withholding,
    }


def nature_needs_seed(invoice_data: Optional[dict]) -> bool:
    return not has_structured_tax_nature(invoice_data)


def persistable_nature(
    invoice_data: Optional[dict],
    *,
    ocr_text: str = "",
) -> Tuple[dict, Dict[str, str]]:
    seed = nature_needs_seed(invoice_data)
    nature = resolve_tax_nature(
        invoice_data, ocr_text=ocr_text, seed_from_text=seed
    )
    return apply_tax_nature(invoice_data, nature), nature
