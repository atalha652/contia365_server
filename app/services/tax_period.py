"""Canonical fiscal periods for quarterly and monthly (REDEME) modelos."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple, Union

from app.models.tax_engine import Quarter


MONTHLY = "MENSUAL"
QUARTERLY = "TRIMESTRAL"
ANNUAL = "ANUAL"

_QUARTER_AEAT = {"Q1": "1T", "Q2": "2T", "Q3": "3T", "Q4": "4T"}
_QUARTERS = frozenset(_QUARTER_AEAT)


@dataclass(frozen=True)
class TaxPeriod:
    year: int
    kind: str  # quarter | month | annual
    period_key: str
    aeat_period: str
    report_quarter: str
    quarter: Optional[str] = None
    month: Optional[int] = None

    @property
    def is_monthly(self) -> bool:
        return self.kind == "month"

    @property
    def is_redeme(self) -> bool:
        return self.is_monthly

    @property
    def label(self) -> str:
        if self.kind == "month" and self.month:
            return f"{self.month:02d} {self.year}"
        if self.kind == "annual":
            return str(self.year)
        return f"{self.quarter} {self.year}"

    def date_range(self) -> Tuple[datetime, datetime]:
        if self.kind == "month" and self.month:
            last = monthrange(self.year, self.month)[1]
            return (
                datetime(self.year, self.month, 1),
                datetime(self.year, self.month, last, 23, 59, 59),
            )
        if self.kind == "annual":
            return (
                datetime(self.year, 1, 1),
                datetime(self.year, 12, 31, 23, 59, 59),
            )
        ranges = {
            "Q1": (datetime(self.year, 1, 1), datetime(self.year, 3, 31, 23, 59, 59)),
            "Q2": (datetime(self.year, 4, 1), datetime(self.year, 6, 30, 23, 59, 59)),
            "Q3": (datetime(self.year, 7, 1), datetime(self.year, 9, 30, 23, 59, 59)),
            "Q4": (datetime(self.year, 10, 1), datetime(self.year, 12, 31, 23, 59, 59)),
        }
        return ranges[self.quarter or "Q4"]


def monthly_period_key(year: int, month: int) -> str:
    return f"{year}-{int(month):02d}"


def _as_quarter(value: Union[Quarter, str, None]) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, Quarter):
        return value.value
    raw = str(value).strip().upper()
    if raw in _QUARTERS:
        return raw
    return None


def parse_month(
    month: Union[int, str, None] = None,
    period_key: Optional[str] = None,
    quarter: Union[Quarter, str, None] = None,
    *,
    reject_quarter: bool = False,
) -> Optional[int]:
    if month is not None and month != "":
        try:
            parsed = int(month)
        except (TypeError, ValueError) as exc:
            raise ValueError("month must be 1-12.") from exc
        if not 1 <= parsed <= 12:
            raise ValueError("month must be 1-12.")
        return parsed

    for raw in (period_key, quarter):
        if raw is None or raw == "":
            continue
        text = str(raw).strip().upper()
        if text in _QUARTERS:
            if reject_quarter:
                raise ValueError(
                    "Modelo 303 is monthly (REDEME). Pass month 1-12, not a quarter."
                )
            continue
        if len(text) >= 7 and text[4:5] == "-" and text[-2:].isdigit():
            parsed = int(text[-2:])
            if 1 <= parsed <= 12:
                return parsed
        digits = text[1:] if text.startswith("M") else text
        if digits.isdigit():
            parsed = int(digits)
            if 1 <= parsed <= 12:
                return parsed
    return None


def resolve_tax_period(
    *,
    year: int,
    periodicity: str,
    quarter: Union[Quarter, str, None] = None,
    month: Union[int, str, None] = None,
    period_key: Optional[str] = None,
    modelo: str = "303",
    allow_monthly: bool = True,
) -> TaxPeriod:
    """Build a TaxPeriod from the profile periodicity and the caller's period args."""
    periodicity = (periodicity or QUARTERLY).upper()
    if periodicity in ("ANUAL", "ANNUAL"):
        return TaxPeriod(
            year=year,
            kind="annual",
            period_key="ANNUAL",
            aeat_period="0A",
            report_quarter=Quarter.Q4.value,
            quarter=None,
        )

    if periodicity == MONTHLY:
        if not allow_monthly or str(modelo) != "303":
            raise ValueError(
                f"Monthly filing is only implemented for Modelo 303, not {modelo}."
            )
        parsed_month = parse_month(
            month, period_key, quarter, reject_quarter=True
        )
        if not parsed_month:
            raise ValueError(
                "month is required for a monthly 303 (REDEME) filing."
            )
        return TaxPeriod(
            year=year,
            kind="month",
            period_key=monthly_period_key(year, parsed_month),
            aeat_period=f"{parsed_month:02d}",
            report_quarter=f"M{parsed_month:02d}",
            month=parsed_month,
        )

    if month not in (None, ""):
        raise ValueError(
            "month is only valid when the fiscal profile files 303 monthly (REDEME)."
        )
    parsed_quarter = _as_quarter(quarter) or _as_quarter(period_key)
    if not parsed_quarter:
        raise ValueError("quarter is required for a non-annual filing.")
    return TaxPeriod(
        year=year,
        kind="quarter",
        period_key=parsed_quarter,
        aeat_period=_QUARTER_AEAT[parsed_quarter],
        report_quarter=parsed_quarter,
        quarter=parsed_quarter,
    )
