"""Modelo 130 pago fraccionado: régimen rate and official payable formula."""

from datetime import date, datetime
from typing import Any, Optional

DEFAULT_130_RATE = 0.20
STARTER_130_RATE = 0.07


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _parse_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _registration(profile: Optional[dict]) -> dict:
    return (profile or {}).get("professional_registration") or {}


def irpf_method_of(profile: Optional[dict]) -> str:
    return _norm(_registration(profile).get("irpf_method") or (profile or {}).get("irpf_method"))


def earliest_activity_start(profile: Optional[dict]) -> Optional[date]:
    starts = []
    for activity in _registration(profile).get("economic_activities") or []:
        parsed = _parse_date((activity or {}).get("start_date"))
        if parsed:
            starts.append(parsed)
    return min(starts) if starts else None


def is_130_starter(profile: Optional[dict], year: int) -> bool:
    """AEAT 7% applies when the activity started in the current or previous year."""
    start = earliest_activity_start(profile)
    if start is None:
        return False
    return start.year in {int(year), int(year) - 1}


def resolve_modelo_130_rate(profile: Optional[dict], year: int) -> float:
    """Return the 130 rate as a fraction (0.20 / 0.07). Default is estimación directa 20%."""
    method = irpf_method_of(profile)
    if any(token in method for token in ("7%", "starter", "nuevo", "nueva", "inicio")):
        return STARTER_130_RATE
    if any(token in method for token in ("objetiva", "modulo", "módulo", "modulos", "módulos")):
        return DEFAULT_130_RATE
    if is_130_starter(profile, year):
        return STARTER_130_RATE
    return DEFAULT_130_RATE


def resolve_modelo_130_rate_percent(profile: Optional[dict], year: int) -> float:
    return round(resolve_modelo_130_rate(profile, year) * 100, 2)


def modelo_130_payable(
    taxable_income: float,
    irpf_rate: float,
    already_withheld: float = 0.0,
    prior_payments: float = 0.0,
) -> float:
    """Casilla 19: max(0, YTD profit × rate − prior 130 payments − withholdings)."""
    gross = round(max(0.0, float(taxable_income) * float(irpf_rate)), 2)
    return round(max(0.0, gross - float(already_withheld or 0) - float(prior_payments or 0)), 2)
