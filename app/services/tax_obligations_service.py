"""Build a fiscal calendar of obligation periods from the canonical profile."""

from calendar import monthrange
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from pymongo.collection import Collection

from app.models.tax_dashboard import TaxDeadlineItem


# Filing deadlines: month/day after the period (Q4 often January next year).
QUARTER_DEADLINES = {
    1: (4, 20),
    2: (7, 20),
    3: (10, 20),
    4: (1, 30),  # next calendar year
}

ANNUAL_DEADLINES = {
    "390": (1, 30),
    "190": (1, 31),
    "347": (2, 28),
}


def _quarter_bounds(year: int, q: int) -> Tuple[date, date]:
    starts = {1: (1, 1), 2: (4, 1), 3: (7, 1), 4: (10, 1)}
    ends = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
    sm, sd = starts[q]
    em, ed = ends[q]
    return date(year, sm, sd), date(year, em, ed)


def _quarter_deadline(modelo: str, year: int, q: int) -> date:
    month, day = QUARTER_DEADLINES[q]
    if q == 4:
        return date(year + 1, month, day)
    if modelo == "303" and q == 4:
        return date(year + 1, 1, 30)
    return date(year, month, day)


def _annual_deadline(modelo: str, year: int) -> date:
    month, day = ANNUAL_DEADLINES.get(modelo, (1, 30))
    if month == 2 and day == 28:
        day = monthrange(year + 1, 2)[1]
        return date(year + 1, 2, day)
    return date(year + 1, month, day)


def _month_bounds(year: int, month: int) -> Tuple[date, date]:
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def _month_deadline(year: int, month: int) -> date:
    if month == 12:
        return date(year + 1, 1, 20)
    return date(year, month + 1, 20)


def _calendar_status(
    today: date,
    period_end: date,
    deadline: date,
    completed: bool,
) -> str:
    if completed:
        return "completed"
    if today > deadline:
        return "overdue"
    if today > period_end or (deadline - today).days <= 7:
        return "due"
    return "upcoming"


def _report_quarter(value) -> str:
    if hasattr(value, "value"):
        return str(value.value)
    return str(value or "")


def _is_filed(report: Optional[dict]) -> bool:
    if not report:
        return False
    return str(report.get("status") or "").lower() == "filed"


def _index_reports(reports: List[dict]) -> Dict[tuple, dict]:
    indexed = {}
    for report in reports:
        modelo = str(report.get("modelo") or "")
        year = report.get("year")
        quarter = _report_quarter(report.get("quarter"))
        indexed[(modelo, year, quarter)] = report
        period_key = str(report.get("period_key") or "")
        if period_key:
            indexed[(modelo, year, period_key)] = report
        # Annual modelos are stored as Q4 in tax_reports.
        indexed.setdefault((modelo, year, "ANNUAL"), report)
    return indexed


def _expand_periods(obligation: dict, year: int) -> List[dict]:
    modelo = str(obligation.get("modelo") or "")
    description = obligation.get("description") or ""
    periodicity = (obligation.get("periodicity") or "TRIMESTRAL").upper()
    rows = []

    if periodicity == "ANUAL":
        start, end = date(year, 1, 1), date(year, 12, 31)
        rows.append({
            "modelo": modelo,
            "description": description,
            "periodicity": periodicity,
            "year": year,
            "quarter": None,
            "period_key": "ANNUAL",
            "current_period": str(year),
            "period_start": start,
            "period_end": end,
            "deadline": _annual_deadline(modelo, year),
        })
    elif periodicity == "MENSUAL":
        for month in range(1, 13):
            start, end = _month_bounds(year, month)
            rows.append({
                "modelo": modelo,
                "description": description,
                "periodicity": periodicity,
                "year": year,
                "quarter": None,
                "month": month,
                "period_key": f"{year}-{month:02d}",
                "current_period": start.strftime("%B %Y"),
                "period_start": start,
                "period_end": end,
                "deadline": _month_deadline(year, month),
            })
    else:
        for q in (1, 2, 3, 4):
            start, end = _quarter_bounds(year, q)
            rows.append({
                "modelo": modelo,
                "description": description,
                "periodicity": periodicity,
                "year": year,
                "quarter": f"Q{q}",
                "period_key": f"Q{q}",
                "current_period": f"Q{q} {year}",
                "period_start": start,
                "period_end": end,
                "deadline": _quarter_deadline(modelo, year, q),
            })
    return rows


def build_fiscal_calendar(
    profile: dict,
    user_id: str,
    tax_reports: Collection,
    tax_filings: Optional[Collection] = None,
    year: Optional[int] = None,
    today: Optional[date] = None,
) -> List[TaxDeadlineItem]:
    today = today or date.today()
    year = year or today.year
    obligations = profile.get("periodic_tax_obligations") or []
    reports = list(tax_reports.find({
        "user_id": str(user_id),
        "year": {"$in": [year - 1, year]},
    }))
    indexed = _index_reports(reports)
    filings = []
    if tax_filings is not None:
        filings = list(tax_filings.find({
            "user_id": str(user_id),
            "year": {"$in": [year - 1, year]},
        }))
    filing_index = {
        (
            str(filing.get("modelo") or ""),
            filing.get("year"),
            str(filing.get("period_key") or ""),
        ): filing
        for filing in filings
    }

    periods = []
    for obligation in obligations:
        periods.extend(_expand_periods(obligation, year))
        periodicity = (obligation.get("periodicity") or "TRIMESTRAL").upper()
        # Keep last year's Q4 / annual on the calendar while those deadlines fall this year.
        if periodicity != "MENSUAL":
            periods.extend(_expand_periods(obligation, year - 1))

    seen = set()
    items = []
    for period in periods:
        key = (period["modelo"], period["year"], period["period_key"])
        if key in seen:
            continue
        seen.add(key)
        if period["year"] != year and period["deadline"].year != year:
            continue

        report = indexed.get((period["modelo"], period["year"], period["period_key"]))
        if not report and period["quarter"]:
            report = indexed.get((period["modelo"], period["year"], period["quarter"]))
        filing = filing_index.get(
            (period["modelo"], period["year"], period["period_key"])
        )
        completed = (
            str((filing or {}).get("status") or "").upper() == "ACCEPTED"
            or _is_filed(report)
        )
        status = _calendar_status(
            today, period["period_end"], period["deadline"], completed
        )
        days_remaining = (period["deadline"] - today).days
        report_id = None
        if report:
            report_id = str(report.get("_id") or report.get("id") or "")

        items.append(TaxDeadlineItem(
            modelo=period["modelo"],
            description=period["description"],
            periodicity=period["periodicity"],
            current_period=period["current_period"],
            year=period["year"],
            quarter=period["quarter"],
            month=period.get("month"),
            period_key=period.get("period_key"),
            period_start=period["period_start"].isoformat(),
            period_end=period["period_end"].isoformat(),
            deadline_date=period["deadline"].isoformat(),
            days_remaining=days_remaining,
            status=status,
            tax_filing_id=str(filing["_id"]) if filing else None,
            tax_report_id=report_id or None,
            filed_at=(
                (filing or {}).get("accepted_at").isoformat()
                if filing
                and isinstance((filing or {}).get("accepted_at"), datetime)
                else report.get("filed_at").isoformat()
                if report and isinstance(report.get("filed_at"), datetime)
                else None
            ),
        ))

    items.sort(key=lambda item: (item.deadline_date, item.modelo))
    return items
