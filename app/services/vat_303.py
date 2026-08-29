"""Modelo 303 special-regime classification and official payable formula."""

from __future__ import annotations

import re
from typing import Any, Optional

RG_VAT_RATES = (21.0, 10.0, 4.0, 0.0)
RECARGO_RATES = (5.2, 1.75, 1.4, 0.5)

# Official recargo tipo % → (base_pos casilla, tipo constant as 5 digits)
RECARGO_FILE_SLOT = {
    1.75: ("156", "00175"),
    5.2: ("16", "00520"),
    1.4: ("19", "00140"),
    0.5: ("22", "00050"),
}


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _blob(*parts: Any) -> str:
    return " ".join(_norm(p) for p in parts if p)


def is_recargo_rate(rate: float) -> bool:
    return any(abs(float(rate or 0) - known) < 0.15 for known in RECARGO_RATES)


def snap_rg_vat_rate(rate: float) -> str:
    """Snap general IVA onto 21 / 10 / 4 / 0. Recargo types are not snapped here."""
    if rate is None or rate <= 0:
        return "0"
    nearest = min(RG_VAT_RATES, key=lambda standard: abs(standard - rate))
    return str(int(nearest)) if nearest != 0 else "0"


def snap_recargo_rate(rate: float) -> float:
    return min(RECARGO_RATES, key=lambda known: abs(known - float(rate or 0)))


def parse_prorrata_percent(profile: Optional[dict]) -> float:
    """100 = full deduction. Values in 0–100 from the fiscal profile."""
    registration = (profile or {}).get("professional_registration") or {}
    raw = (
        registration.get("prorrata_percent")
        or registration.get("prorrata")
        or (profile or {}).get("prorrata_percent")
    )
    if raw not in (None, ""):
        try:
            value = float(raw)
            if value <= 1:
                value *= 100
            return max(0.0, min(100.0, value))
        except (TypeError, ValueError):
            pass
    text = _norm(registration.get("vat_regime") or (profile or {}).get("vat_regime"))
    match = re.search(r"prorrata[^\d]*(\d{1,3}(?:[.,]\d+)?)\s*%?", text)
    if match:
        return max(0.0, min(100.0, float(match.group(1).replace(",", "."))))
    if "prorrata" in text:
        return 100.0
    return 100.0


def is_prorrata_especial(profile: Optional[dict]) -> bool:
    text = _blob(
        ((profile or {}).get("professional_registration") or {}).get("vat_regime"),
        (profile or {}).get("vat_regime"),
    )
    return "prorrata especial" in text or "prorrata-especial" in text


def classify_303_line(
    *,
    vat_rate: float,
    recargo_rate: float = 0.0,
    recargo_amount: float = 0.0,
    text: str = "",
    stored_regime: str = "",
    stored_operation_type: str = "",
) -> str:
    """
    One invoice → one 303 bucket.
    general | recargo | isp | intra | import | used_goods | investment
    Stored operation_type wins over invoice wording.
    Recargo can sit on top of general IVA; callers still post RG IVA separately.
    """
    stored = _norm(stored_operation_type or stored_regime)
    if stored in {
        "general", "isp", "intra", "import", "recargo", "used_goods", "investment",
    }:
        if stored == "general" and (
            recargo_amount > 0 or recargo_rate > 0 or is_recargo_rate(vat_rate)
        ):
            return "recargo"
        return stored

    blob = _blob(text, stored_regime)
    if any(token in blob for token in (
        "bienes usados", "bien usado", "rebu", "regimen especial de bienes",
        "régimen especial de bienes", "used goods",
    )):
        return "used_goods"
    if any(token in blob for token in (
        "inversion del sujeto pasivo", "inversión del sujeto pasivo",
        "isp", "reverse charge",
    )):
        return "isp"
    if any(token in blob for token in (
        "intracomunitar", "intra-comunitar", "intra community", "intra-community",
        "adquisicion intracomunitaria", "adquisición intracomunitaria",
    )):
        return "intra"
    if any(token in blob for token in (
        "importacion", "importación", "dua ", "aduana",
    )):
        return "import"
    if any(token in blob for token in (
        "bien de inversion", "bien de inversión", "bienes de inversion",
        "bienes de inversión",
    )):
        return "investment"
    if recargo_amount > 0 or recargo_rate > 0 or is_recargo_rate(vat_rate) or any(
        token in blob for token in ("recargo de equivalencia", "recargo equivalencia", "recargo equiv")
    ):
        return "recargo"
    return "general"


def modelo_303_payable(
    output_vat: float,
    input_vat: float,
    *,
    isp_vat: float = 0.0,
    intra_vat: float = 0.0,
    recargo_vat: float = 0.0,
    import_vat: float = 0.0,
    investment_vat: float = 0.0,
    prorrata_percent: float = 100.0,
) -> float:
    """Casilla 46: [27] − [45]. Prorrata reduces deductible input IVA."""
    accrued = (
        float(output_vat or 0)
        + float(isp_vat or 0)
        + float(intra_vat or 0)
        + float(recargo_vat or 0)
    )
    factor = max(0.0, min(100.0, float(prorrata_percent or 100))) / 100.0
    deductible = (
        float(input_vat or 0)
        + float(isp_vat or 0)
        + float(intra_vat or 0)
        + float(import_vat or 0)
        + float(investment_vat or 0)
    ) * factor
    return round(accrued - deductible, 2)


def is_regimen_simplificado(profile: Optional[dict]) -> bool:
    text = _blob(
        ((profile or {}).get("professional_registration") or {}).get("vat_regime"),
        (profile or {}).get("vat_regime"),
    )
    return "simplific" in text


def empty_vat_accumulator() -> dict:
    return {
        "total_sales": 0.0,
        "total_expenses": 0.0,
        "output_vat": 0.0,
        "input_vat": 0.0,
        "isp_base": 0.0,
        "isp_vat": 0.0,
        "intra_base": 0.0,
        "intra_vat": 0.0,
        "import_base": 0.0,
        "import_vat": 0.0,
        "investment_base": 0.0,
        "investment_vat": 0.0,
        "used_goods_base": 0.0,
        "recargo_by_rate": {},
        "vat_by_rate": {
            str(int(rate) if rate else 0): {
                "output_base": 0.0, "output_vat": 0.0,
                "input_base": 0.0, "input_vat": 0.0,
            }
            for rate in RG_VAT_RATES
        },
    }


def apply_vat_line(acc: dict, a: dict, *, income: bool) -> None:
    """Route one invoice into RG / ISP / intra / recargo / import / used-goods."""
    regime = classify_303_line(
        vat_rate=a.get("vat_rate") or 0,
        recargo_rate=a.get("recargo_rate") or 0,
        recargo_amount=a.get("recargo_amount") or 0,
        text=a.get("ocr_text") or "",
        stored_regime=a.get("vat_regime") or "",
        stored_operation_type=a.get("operation_type") or "",
    )
    if regime == "isp":
        acc["isp_base"] += a["base_amount"]
        acc["isp_vat"] += a["vat_amount"]
        return
    if regime == "intra":
        acc["intra_base"] += a["base_amount"]
        acc["intra_vat"] += a["vat_amount"]
        return
    if regime == "import":
        acc["import_base"] += a["base_amount"]
        acc["import_vat"] += a["vat_amount"]
        return
    if regime == "used_goods":
        acc["used_goods_base"] += a["base_amount"]
        return
    if regime == "investment":
        acc["investment_base"] += a["base_amount"]
        acc["investment_vat"] += a["vat_amount"]
        return

    recargo_amt = float(a.get("recargo_amount") or 0)
    recargo_rate = float(a.get("recargo_rate") or 0)
    rg_rate = float(a.get("vat_rate") or 0)
    rg_vat = float(a.get("vat_amount") or 0)
    if is_recargo_rate(rg_rate) and recargo_amt <= 0:
        recargo_amt = rg_vat
        recargo_rate = rg_rate
        rg_vat = 0.0
        rg_rate = 0.0
    if recargo_amt > 0 or recargo_rate > 0:
        if recargo_amt <= 0 and recargo_rate:
            recargo_amt = round(a["base_amount"] * recargo_rate / 100.0, 2)
        rate_for_slot = recargo_rate
        if rate_for_slot <= 0 and a["base_amount"] > 0 and recargo_amt > 0:
            rate_for_slot = recargo_amt / a["base_amount"] * 100.0
        if rate_for_slot <= 0:
            rate_for_slot = 5.2
        slot = str(snap_recargo_rate(rate_for_slot))
        bucket_re = acc["recargo_by_rate"].setdefault(slot, {"base": 0.0, "vat": 0.0})
        bucket_re["base"] += a["base_amount"]
        bucket_re["vat"] += recargo_amt
        if rg_vat <= 0:
            return

    bucket = acc["vat_by_rate"][snap_rg_vat_rate(rg_rate)]
    if income:
        acc["total_sales"] += a["base_amount"]
        acc["output_vat"] += rg_vat
        bucket["output_base"] += a["base_amount"]
        bucket["output_vat"] += rg_vat
    else:
        acc["total_expenses"] += a["base_amount"]
        acc["input_vat"] += rg_vat
        bucket["input_base"] += a["base_amount"]
        bucket["input_vat"] += rg_vat


def finalize_vat_accumulator(acc: dict, prorrata_percent: float = 100.0) -> dict:
    recargo_by_rate = {
        rate: {"base": round(vals["base"], 2), "vat": round(vals["vat"], 2)}
        for rate, vals in acc["recargo_by_rate"].items()
    }
    recargo_vat = round(sum(v["vat"] for v in recargo_by_rate.values()), 2)
    money = (
        "total_sales", "total_expenses", "output_vat", "input_vat",
        "isp_base", "isp_vat", "intra_base", "intra_vat",
        "import_base", "import_vat", "investment_base", "investment_vat",
        "used_goods_base",
    )
    out = {key: round(float(acc[key]), 2) for key in money}
    out["recargo_by_rate"] = recargo_by_rate
    out["recargo_vat"] = recargo_vat
    out["prorrata_percent"] = float(prorrata_percent)
    out["vat_by_rate"] = {
        rate: {field: round(value, 2) for field, value in bucket.items()}
        for rate, bucket in acc["vat_by_rate"].items()
    }
    out["net_vat"] = modelo_303_payable(
        out["output_vat"],
        out["input_vat"],
        isp_vat=out["isp_vat"],
        intra_vat=out["intra_vat"],
        recargo_vat=recargo_vat,
        import_vat=out["import_vat"],
        investment_vat=out["investment_vat"],
        prorrata_percent=prorrata_percent,
    )
    factor = max(0.0, min(100.0, float(prorrata_percent))) / 100.0
    out["input_vat_deductible"] = round(
        (out["input_vat"] + out["isp_vat"] + out["intra_vat"] + out["import_vat"] + out["investment_vat"])
        * factor,
        2,
    )
    return out
