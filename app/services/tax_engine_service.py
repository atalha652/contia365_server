"""
Tax Calculation Engine Service
================================
Strict separation of concerns:
  - Classification Layer  → decides which modelos an entry belongs to
  - Tax Engine (this file) → aggregates numbers for a given modelo_id only

This service does NOT classify entries. It reads the precomputed
`tax_classification.modelo_ids` field written by TaxClassificationService.

If an entry has no tax_classification yet (legacy data), it is skipped
and a warning is logged. Run TaxClassificationService.backfill_user()
to classify existing entries.
"""

import logging
from datetime import datetime
from typing import Tuple, List, Optional, Union

from app.models.tax_engine import (
    Quarter, TaxReport, TaxReportStatus,
    Modelo303Results, Modelo130Results,
    Modelo303Response, Modelo130Response,
)
from app.repos.tax_engine_repo import TaxEngineRepository
from app.repos.tax_percipient_repo import TaxPercipientRepository
from app.services.fiscal_profile_service import get_canonical_fiscal_profile
from app.services.irpf_130 import modelo_130_payable, resolve_modelo_130_rate
from app.services.tax_period import resolve_tax_period
from app.services.vat_303 import (
    apply_vat_line,
    classify_303_line,
    empty_vat_accumulator,
    finalize_vat_accumulator,
    is_recargo_rate,
    is_prorrata_especial,
    is_regimen_simplificado,
    modelo_303_payable,
    parse_prorrata_percent,
    snap_recargo_rate,
    snap_rg_vat_rate,
)

logger = logging.getLogger(__name__)


# ─────────────────────── date helpers ────────────────────────────────────────

def _quarter_date_range(year: int, quarter: Quarter) -> Tuple[datetime, datetime]:
    ranges = {
        Quarter.Q1: (datetime(year, 1, 1),  datetime(year, 3, 31, 23, 59, 59)),
        Quarter.Q2: (datetime(year, 4, 1),  datetime(year, 6, 30, 23, 59, 59)),
        Quarter.Q3: (datetime(year, 7, 1),  datetime(year, 9, 30, 23, 59, 59)),
        Quarter.Q4: (datetime(year, 10, 1), datetime(year, 12, 31, 23, 59, 59)),
    }
    return ranges[quarter]


def _parse_invoice_date(date_str: str) -> datetime | None:
    if not date_str or date_str == "N/A":
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    return None


def _in_quarter(invoice_date_str: str, start: datetime, end: datetime) -> bool:
    dt = _parse_invoice_date(invoice_date_str)
    if dt is None:
        return False
    return start <= dt <= end


# ─────────────────────── amount extraction ───────────────────────────────────

def _extract_amounts(entry: dict) -> dict:
    """
    Extract pre-VAT base, VAT amount, IRPF retention, and transaction type
    from a ledger entry. Reads directly from invoice_data.totals — no recalculation.
    """
    invoice_data   = entry.get("invoice_data") or {}
    totals         = invoice_data.get("totals") or {}
    tx_type        = str(invoice_data.get("transaction_type", "expense")).lower()

    # Read base directly — new entries have explicit "base" field
    base_amount    = float(totals.get("base") or 0)
    vat_amount     = float(totals.get("VAT_amount") or 0)
    vat_rate       = float(totals.get("VAT_rate") or 0)
    irpf_retention = float(totals.get("IRPF_amount") or 0)
    recargo_rate   = float(totals.get("recargo_rate") or totals.get("RECARGO_rate") or 0)
    recargo_amount = float(totals.get("recargo_amount") or totals.get("RECARGO_amount") or 0)
    total_with_tax = float(totals.get("Total_with_Tax") or totals.get("total") or 0)
    ocr_text = entry.get("ocr_text") or ""
    stored_regime = str(
        totals.get("vat_regime")
        or invoice_data.get("vat_regime")
        or invoice_data.get("vat_special")
        or ""
    )
    operation_type = str(
        invoice_data.get("operation_type")
        or totals.get("operation_type")
        or stored_regime
        or ""
    )

    # Fallback for legacy entries that don't have the "base" field:
    # only recalculate if base is missing AND vat_amount looks like a real amount (not the rate)
    if base_amount == 0 and total_with_tax > 0:
        if vat_amount > vat_rate and vat_rate > 0:
            # vat_amount is a real monetary value
            base_amount = round(total_with_tax - vat_amount, 2)
        elif vat_rate > 0:
            # vat_amount is missing or equals the rate (old OCR bug) — reverse calc
            base_amount = round(total_with_tax / (1 + vat_rate / 100), 2)
            vat_amount  = round(total_with_tax - base_amount, 2)
        else:
            # No VAT (e.g. rent) — base = total
            base_amount = total_with_tax

    # Infer rate from amounts when OCR omitted VAT_rate
    if vat_rate <= 0 and base_amount > 0 and vat_amount > 0:
        vat_rate = round((vat_amount / base_amount) * 100, 2)

    return {
        "transaction_type": tx_type,
        "base_amount":      base_amount,
        "vat_amount":       vat_amount,
        "vat_rate":         vat_rate,
        "irpf_retention":   irpf_retention,
        "recargo_rate":     recargo_rate,
        "recargo_amount":   recargo_amount,
        "vat_regime":       stored_regime,
        "operation_type":   operation_type,
        "ocr_text":         ocr_text,
        "total_with_tax":   total_with_tax,
    }


_STANDARD_VAT_RATES = (21, 10, 4, 0)


def _empty_vat_by_rate() -> dict:
    return {
        str(rate): {
            "output_base": 0.0,
            "output_vat": 0.0,
            "input_base": 0.0,
            "input_vat": 0.0,
        }
        for rate in _STANDARD_VAT_RATES
    }


def _bucket_vat_rate(rate: float) -> str:
    """Snap a stored rate onto the Spanish 21 / 10 / 4 / 0 split."""
    return snap_rg_vat_rate(rate)


def _is_income(tx_type: str) -> bool:
    return tx_type in ("income", "credit")


# ─────────────────────── service ─────────────────────────────────────────────

class TaxEngineService:
    """
    Pure computation engine.
    Reads ledger entries that have been pre-classified by TaxClassificationService
    and aggregates financial totals per modelo.
    """

    def __init__(self, repo=None, percipient_repo=None):
        self.repo = repo if repo is not None else TaxEngineRepository()
        self.percipient_repo = (
            percipient_repo if percipient_repo is not None else TaxPercipientRepository()
        )

    def _percipient_lines(self, user_id: str, year: int, quarter: Optional[str] = None) -> list[dict]:
        rows = self.percipient_repo.list(user_id, year, quarter)
        lines = []
        for row in rows:
            lines.append({
                "nif": row.get("nif"),
                "full_name": row.get("full_name"),
                "perception_key": row.get("perception_key") or "G",
                "perception_subkey": row.get("perception_subkey") or "01",
                "year": row.get("year"),
                "quarter": row.get("quarter"),
                "base_amount": round(float(row.get("base_amount") or 0), 2),
                "withheld_amount": round(float(row.get("withheld_amount") or 0), 2),
                "in_kind": bool(row.get("in_kind")),
                "province_code": row.get("province_code"),
                "kind": row.get("kind") or "professional",
            })
        return lines

    def _require_applicable_modelo(self, user_id: str, modelo: str) -> None:
        applicable = self.repo.get_applicable_modelos(user_id)
        if modelo not in applicable:
            raise ValueError(
                f"Modelo {modelo} is not applicable under the canonical fiscal profile"
            )

    def _get_entries_for_modelo(
        self, user_id: str, organization_id: str,
        modelo_id: Optional[str], start: datetime, end: datetime
    ) -> List[dict]:
        """
        Fetch ledger entries for the period.
        - If modelo_id is provided: only entries classified for that specific modelo.
        - If modelo_id is None: all successfully processed entries (broad fallback).
        """
        if modelo_id:
            entries = self.repo.get_classified_entries_for_modelo(
                user_id, modelo_id, start, end
            )
        else:
            entries = self.repo.get_ocr_ledger_entries_for_period(user_id, start, end)

        # Final fallback: accounting ledger_entries
        if not entries:
            entries = self.repo.get_accounting_ledger_entries_for_period(
                organization_id, start, end
            )
        return entries

    def _filter_by_invoice_date(
        self, entries: List[dict], start: datetime, end: datetime
    ) -> List[dict]:
        """
        Secondary filter by invoice_date string when available.
        Only drops entries whose invoice_date is explicitly outside the period
        AND whose transaction_date is also outside — avoids dropping entries
        uploaded in one quarter for an invoice from a prior quarter.
        """
        result = []
        for e in entries:
            date_str = (
                (e.get("invoice_data") or {})
                .get("invoice", {})
                .get("invoice_date", "")
            )
            # If no invoice_date, keep the entry
            if not date_str or date_str == "N/A":
                result.append(e)
                continue
            # If invoice_date is in range, keep it
            if _in_quarter(date_str, start, end):
                result.append(e)
                continue
            # Invoice date is outside range — keep if transaction_date is in range
            tx_date = e.get("transaction_date")
            if tx_date and start <= tx_date <= end:
                result.append(e)
        return result

    # ── Modelo 303 ────────────────────────────────────────────────────────────

    def calculate_modelo_303(
        self, user_id: str, organization_id: str,
        year: int, quarter: Optional[Union[Quarter, str]] = None,
        modelo_id: Optional[str] = None,
        month: Optional[int] = None,
        period_key: Optional[str] = None,
    ) -> Modelo303Response:
        """
        Aggregate VAT for entries pre-classified as belonging to modelo_id.

        Régimen general: output − input at 21/10/4/0.
        Also routes ISP, intra-community, recargo, imports, used-goods, prorrata.
        Monthly (REDEME) filers pass month 1-12; quarterly filers pass Q1-Q4.
        """
        self._require_applicable_modelo(user_id, "303")
        periodicity = self.repo.get_modelo_periodicity(user_id, "303")
        period = resolve_tax_period(
            year=year,
            periodicity=periodicity,
            quarter=quarter,
            month=month,
            period_key=period_key,
            modelo="303",
        )
        start, end = period.date_range()
        raw        = self._get_entries_for_modelo(user_id, organization_id, modelo_id, start, end)
        entries    = self._filter_by_invoice_date(raw, start, end)

        try:
            profile = get_canonical_fiscal_profile(self.repo.users, self.repo.census_data, user_id)
        except Exception:
            profile = None

        totals = Modelo303Results(
            prorrata_percent=parse_prorrata_percent(profile),
            prorrata_especial=is_prorrata_especial(profile),
        )
        vat_by_rate = _empty_vat_by_rate()
        recargo_by_rate: dict = {}
        count  = 0

        for entry in entries:
            a = _extract_amounts(entry)
            if a["total_with_tax"] == 0:
                continue
            count += 1
            regime = classify_303_line(
                vat_rate=a["vat_rate"],
                recargo_rate=a["recargo_rate"],
                recargo_amount=a["recargo_amount"],
                text=a["ocr_text"],
                stored_regime=a["vat_regime"],
                stored_operation_type=a.get("operation_type") or "",
            )
            income = _is_income(a["transaction_type"])

            if regime == "isp":
                totals.isp_base += a["base_amount"]
                totals.isp_vat += a["vat_amount"]
                continue
            if regime == "intra":
                totals.intra_base += a["base_amount"]
                totals.intra_vat += a["vat_amount"]
                continue
            if regime == "import":
                totals.import_base += a["base_amount"]
                totals.import_vat += a["vat_amount"]
                continue
            if regime == "used_goods":
                totals.used_goods_base += a["base_amount"]
                continue
            if regime == "investment":
                totals.investment_base += a["base_amount"]
                totals.investment_vat += a["vat_amount"]
                continue

            recargo_amt = a["recargo_amount"]
            recargo_rate = a["recargo_rate"]
            rg_rate = a["vat_rate"]
            rg_vat = a["vat_amount"]
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
                bucket_re = recargo_by_rate.setdefault(slot, {"base": 0.0, "vat": 0.0})
                bucket_re["base"] += a["base_amount"]
                bucket_re["vat"] += recargo_amt
                if rg_vat <= 0:
                    continue

            bucket = vat_by_rate[_bucket_vat_rate(rg_rate)]
            if income:
                totals.total_sales += a["base_amount"]
                totals.output_vat += rg_vat
                bucket["output_base"] += a["base_amount"]
                bucket["output_vat"] += rg_vat
            else:
                totals.total_expenses += a["base_amount"]
                totals.input_vat += rg_vat
                bucket["input_base"] += a["base_amount"]
                bucket["input_vat"] += rg_vat

        totals.total_sales    = round(totals.total_sales, 2)
        totals.total_expenses = round(totals.total_expenses, 2)
        totals.output_vat     = round(totals.output_vat, 2)
        totals.input_vat      = round(totals.input_vat, 2)
        totals.isp_base = round(totals.isp_base, 2)
        totals.isp_vat = round(totals.isp_vat, 2)
        totals.intra_base = round(totals.intra_base, 2)
        totals.intra_vat = round(totals.intra_vat, 2)
        totals.import_base = round(totals.import_base, 2)
        totals.import_vat = round(totals.import_vat, 2)
        totals.investment_base = round(totals.investment_base, 2)
        totals.investment_vat = round(totals.investment_vat, 2)
        totals.used_goods_base = round(totals.used_goods_base, 2)
        totals.recargo_by_rate = {
            rate: {"base": round(vals["base"], 2), "vat": round(vals["vat"], 2)}
            for rate, vals in recargo_by_rate.items()
        }
        totals.recargo_vat = round(sum(v["vat"] for v in totals.recargo_by_rate.values()), 2)
        totals.vat_payable = modelo_303_payable(
            totals.output_vat,
            totals.input_vat,
            isp_vat=totals.isp_vat,
            intra_vat=totals.intra_vat,
            recargo_vat=totals.recargo_vat,
            import_vat=totals.import_vat,
            investment_vat=totals.investment_vat,
            prorrata_percent=totals.prorrata_percent,
        )
        factor = max(0.0, min(100.0, totals.prorrata_percent)) / 100.0
        totals.input_vat_deductible = round(
            (totals.input_vat + totals.isp_vat + totals.intra_vat + totals.import_vat + totals.investment_vat)
            * factor,
            2,
        )
        totals.vat_by_rate = {
            rate: {field: round(value, 2) for field, value in bucket.items()}
            for rate, bucket in vat_by_rate.items()
        }

        report = TaxReport(
            user_id=user_id, organization_id=organization_id,
            modelo="303", year=year,
            quarter=period.report_quarter,
            period_key=period.period_key,
            results=totals.model_dump(),
            status=TaxReportStatus.DRAFT,
            transactions_count=count,
        )
        self.repo.upsert_tax_report(report)

        return Modelo303Response(
            period=period.label, year=year,
            quarter=period.quarter,
            month=period.month,
            period_key=period.period_key,
            redeme=period.is_redeme,
            totals=totals, status=TaxReportStatus.DRAFT,
            transactions_count=count,
            calculated_at=datetime.utcnow().isoformat(),
        )

    # ── Modelo 130 ────────────────────────────────────────────────────────────

    def calculate_modelo_130(
        self, user_id: str, organization_id: str,
        year: int, quarter: Quarter, modelo_id: str
    ) -> Modelo130Response:
        """
        IRPF pago fraccionado (official 130 is year-to-date).
        taxableIncome = YTD income - YTD expenses
        irpfPayable   = max(0, taxableIncome × régimen rate − withheld − prior 130 payments)
        """
        self._require_applicable_modelo(user_id, "130")
        year_start, _ = _quarter_date_range(year, Quarter.Q1)
        _, end = _quarter_date_range(year, quarter)
        raw        = self._get_entries_for_modelo(user_id, organization_id, modelo_id, year_start, end)
        entries    = self._filter_by_invoice_date(raw, year_start, end)

        try:
            profile = get_canonical_fiscal_profile(self.repo.users, self.repo.census_data, user_id)
        except Exception:
            profile = None
        totals = Modelo130Results(irpf_rate=resolve_modelo_130_rate(profile, year))
        count  = 0

        for entry in entries:
            a = _extract_amounts(entry)
            if a["total_with_tax"] == 0:
                continue
            count += 1
            if _is_income(a["transaction_type"]):
                totals.total_income          += a["base_amount"]
                totals.irpf_already_withheld += a["irpf_retention"]
            else:
                totals.total_expenses += a["base_amount"]

        totals.total_income          = round(totals.total_income, 2)
        totals.total_expenses        = round(totals.total_expenses, 2)
        totals.taxable_income        = round(totals.total_income - totals.total_expenses, 2)
        totals.irpf_already_withheld = round(totals.irpf_already_withheld, 2)
        prior = 0.0
        for report in self.repo.list_tax_reports_by_modelo_no(user_id, "130", year):
            report_q = str(report.get("quarter") or "")
            if report_q and report_q < str(quarter.value if hasattr(quarter, "value") else quarter):
                prior += float((report.get("results") or {}).get("irpf_payable") or 0)
        totals.prior_payments = round(max(0.0, prior), 2)
        totals.irpf_payable = modelo_130_payable(
            totals.taxable_income,
            totals.irpf_rate,
            totals.irpf_already_withheld,
            totals.prior_payments,
        )

        report = TaxReport(
            user_id=user_id, organization_id=organization_id,
            modelo="130", year=year, quarter=quarter,
            results=totals.model_dump(),
            status=TaxReportStatus.DRAFT,
            transactions_count=count,
        )
        self.repo.upsert_tax_report(report)

        return Modelo130Response(
            period=f"{quarter} {year}", year=year, quarter=quarter,
            totals=totals, status=TaxReportStatus.DRAFT,
            transactions_count=count,
            calculated_at=datetime.utcnow().isoformat(),
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    def list_reports(self, user_id: str, modelo: str = None):
        return self.repo.list_tax_reports(user_id, modelo)

    def update_status(self, report_id: str, status: TaxReportStatus) -> bool:
        return self.repo.update_status(report_id, status)

    # ── Modelo 115 ────────────────────────────────────────────────────────────

    def calculate_modelo_115(
        self, user_id: str, organization_id: str,
        year: int, quarter: Quarter, modelo_id: str
    ):
        """
        Rent IRPF withholding.
        withholding_payable = sum of IRPF amounts printed on the invoices.
        Missing / 0 IRPF is 0 — do not invent 19% of the rent base.
        """
        from app.models.tax_engine import Modelo115Results, Modelo115Response
        self._require_applicable_modelo(user_id, "115")
        start, end = _quarter_date_range(year, quarter)
        raw     = self._get_entries_for_modelo(user_id, organization_id, modelo_id, start, end)
        entries = self._filter_by_invoice_date(raw, start, end)

        totals = Modelo115Results()
        count  = 0

        for entry in entries:
            a = _extract_amounts(entry)
            if a["total_with_tax"] == 0:
                continue
            count += 1
            totals.total_rent_base += a["base_amount"]
            totals.withholding_payable += max(0.0, a["irpf_retention"])

        totals.total_rent_base     = round(totals.total_rent_base, 2)
        totals.withholding_payable = round(totals.withholding_payable, 2)
        if totals.total_rent_base > 0 and totals.withholding_payable > 0:
            totals.retention_rate = round(totals.withholding_payable / totals.total_rent_base, 4)
        else:
            totals.retention_rate = 0.0
        totals.percipient_count = 1 if totals.withholding_payable or totals.total_rent_base else 0

        report = TaxReport(
            user_id=user_id, organization_id=organization_id,
            modelo="115", year=year, quarter=quarter,
            results=totals.model_dump(),
            status=TaxReportStatus.DRAFT,
            transactions_count=count,
        )
        self.repo.upsert_tax_report(report)

        return Modelo115Response(
            period=f"{quarter} {year}", year=year, quarter=quarter,
            totals=totals, status=TaxReportStatus.DRAFT,
            transactions_count=count,
            calculated_at=datetime.utcnow().isoformat(),
        )

    # ── Modelo 111 ────────────────────────────────────────────────────────────

    def calculate_modelo_111(
        self, user_id: str, organization_id: str,
        year: int, quarter: Quarter, modelo_id: str
    ):
        """
        Employee / professional IRPF withholding.
        Aggregates all IRPF retentions from professional invoices (honorarios).
        withholding_payable = sum of all irpf_retention values.
        """
        from app.models.tax_engine import Modelo111Results, Modelo111Response
        self._require_applicable_modelo(user_id, "111")
        qval = quarter.value if hasattr(quarter, "value") else str(quarter)
        lines = self._percipient_lines(user_id, year, qval)
        totals = Modelo111Results()
        count = 0

        if lines:
            totals.lines = lines
            totals.percipient_count = len(lines)
            totals.legally_complete = True
            for line in lines:
                totals.total_base += float(line["base_amount"] or 0)
                totals.total_withheld += float(line["withheld_amount"] or 0)
            count = len(lines)
        else:
            start, end = _quarter_date_range(year, quarter)
            raw     = self._get_entries_for_modelo(user_id, organization_id, modelo_id, start, end)
            entries = self._filter_by_invoice_date(raw, start, end)
            for entry in entries:
                a = _extract_amounts(entry)
                if a["total_with_tax"] == 0:
                    continue
                count += 1
                totals.total_base    += a["base_amount"]
                totals.total_withheld += a["irpf_retention"]
            totals.legally_complete = False

        totals.total_base         = round(totals.total_base, 2)
        totals.total_withheld     = round(totals.total_withheld, 2)
        totals.withholding_payable = totals.total_withheld

        report = TaxReport(
            user_id=user_id, organization_id=organization_id,
            modelo="111", year=year, quarter=quarter,
            results=totals.model_dump(),
            status=TaxReportStatus.DRAFT,
            transactions_count=count,
        )
        self.repo.upsert_tax_report(report)

        return Modelo111Response(
            period=f"{quarter} {year}", year=year, quarter=quarter,
            totals=totals, status=TaxReportStatus.DRAFT,
            transactions_count=count,
            calculated_at=datetime.utcnow().isoformat(),
        )

    # ── Modelo 390 ────────────────────────────────────────────────────────────

    def calculate_modelo_390(
        self, user_id: str, organization_id: str,
        year: int, modelo_id: str
    ):
        """
        Annual VAT summary — same regime split as 303 across the full year.
        Also reads previously saved 303 quarterly reports to compute
        quarterly_payments already made.
        """
        from app.models.tax_engine import Modelo390Results, Modelo390Response, Quarter
        self._require_applicable_modelo(user_id, "390")
        start = datetime(year, 1, 1)
        end   = datetime(year, 12, 31, 23, 59, 59)
        raw     = self._get_entries_for_modelo(user_id, organization_id, modelo_id, start, end)
        entries = self._filter_by_invoice_date(raw, start, end)

        try:
            profile = get_canonical_fiscal_profile(self.repo.users, self.repo.census_data, user_id)
        except Exception:
            profile = None
        prorrata = parse_prorrata_percent(profile)
        acc = empty_vat_accumulator()
        count  = 0

        for entry in entries:
            a = _extract_amounts(entry)
            if a["total_with_tax"] == 0:
                continue
            count += 1
            apply_vat_line(acc, a, income=_is_income(a["transaction_type"]))

        finalized = finalize_vat_accumulator(acc, prorrata)
        totals = Modelo390Results(
            **finalized,
            prorrata_especial=is_prorrata_especial(profile),
            regimen_simplificado=is_regimen_simplificado(profile),
        )

        # Sum quarterly 303 payments already filed
        quarterly_reports = self.repo.list_tax_reports_by_modelo_no(user_id, "303", year)
        totals.quarterly_payments = round(
            sum(r.get("results", {}).get("vat_payable", 0) for r in quarterly_reports
                if r.get("results", {}).get("vat_payable", 0) > 0),
            2
        )

        report = TaxReport(
            user_id=user_id, organization_id=organization_id,
            modelo="390", year=year, quarter=Quarter.Q4,
            results=totals.model_dump(),
            status=TaxReportStatus.DRAFT,
            transactions_count=count,
        )
        self.repo.upsert_tax_report(report)

        return Modelo390Response(
            period=str(year), year=year, quarter=Quarter.Q4,
            totals=totals, status=TaxReportStatus.DRAFT,
            transactions_count=count,
            calculated_at=datetime.utcnow().isoformat(),
        )

    # ── Modelo 190 ────────────────────────────────────────────────────────────

    def calculate_modelo_190(
        self, user_id: str, organization_id: str,
        year: int, modelo_id: str
    ):
        """
        Annual withholding summary of percipient lines (Modelo 190).
        Not an annual IRPF income return.
        """
        from app.models.tax_engine import Modelo190Results, Modelo190Response, Quarter
        self._require_applicable_modelo(user_id, "190")
        lines = self._percipient_lines(user_id, year, None)
        totals = Modelo190Results(lines=lines, percipient_count=len(lines))
        for line in lines:
            totals.total_base += float(line["base_amount"] or 0)
            totals.total_withheld += float(line["withheld_amount"] or 0)
        totals.total_base = round(totals.total_base, 2)
        totals.total_withheld = round(totals.total_withheld, 2)
        totals.legally_complete = bool(lines)
        count = len(lines)

        report = TaxReport(
            user_id=user_id, organization_id=organization_id,
            modelo="190", year=year, quarter=Quarter.Q4,
            results=totals.model_dump(),
            status=TaxReportStatus.DRAFT,
            transactions_count=count,
        )
        self.repo.upsert_tax_report(report)

        return Modelo190Response(
            period=str(year), year=year, quarter=Quarter.Q4,
            totals=totals, status=TaxReportStatus.DRAFT,
            transactions_count=count,
            calculated_at=datetime.utcnow().isoformat(),
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    def list_reports(self, user_id: str, modelo: str = None):
        return self.repo.list_tax_reports(user_id, modelo)

    def update_status(self, report_id: str, status: TaxReportStatus) -> bool:
        return self.repo.update_status(report_id, status)
