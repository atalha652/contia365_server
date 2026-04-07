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
from typing import Tuple, List, Optional

from app.models.tax_engine import (
    Quarter, TaxReport, TaxReportStatus,
    Modelo303Results, Modelo130Results,
    Modelo303Response, Modelo130Response,
)
from app.repos.tax_engine_repo import TaxEngineRepository

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
    from a ledger entry. No classification logic here — only arithmetic.
    """
    import re
    invoice_data = entry.get("invoice_data") or {}
    totals       = invoice_data.get("totals") or {}
    items        = invoice_data.get("items") or []
    ocr_text     = entry.get("ocr_text") or ""
    tx_type      = str(invoice_data.get("transaction_type", "expense")).lower()

    total_with_tax = float(totals.get("Total_with_Tax") or totals.get("total") or 0)
    vat_rate       = float(totals.get("VAT_rate") or 21)
    raw_vat        = float(totals.get("VAT_amount") or 0)
    raw_total      = float(totals.get("total") or 0)

    # Detect OCR bug: VAT_amount stores the rate instead of the monetary value
    if raw_vat == vat_rate and raw_total > 0:
        total_with_tax = raw_total
        base_amount    = round(total_with_tax / (1 + vat_rate / 100), 2)
        vat_amount     = round(total_with_tax - base_amount, 2)
    else:
        # Try line items first (most trustworthy pre-VAT base)
        # Only use items_base if it's clearly less than total_with_tax (i.e. pre-VAT)
        items_base = sum(float(i.get("subtotal") or i.get("unit_price") or 0) for i in items)
        items_vat  = round(items_base * vat_rate / 100, 2)
        items_is_pretax = items_base > 0 and items_base < total_with_tax and abs(items_base + items_vat - total_with_tax) < 1.0
        if items_is_pretax:
            base_amount = round(items_base, 2)
            vat_amount  = items_vat
        elif raw_vat > vat_rate and total_with_tax > 0:
            vat_amount  = raw_vat
            base_amount = round(total_with_tax - vat_amount, 2)
        elif total_with_tax > 0:
            base_amount = round(total_with_tax / (1 + vat_rate / 100), 2)
            vat_amount  = round(total_with_tax - base_amount, 2)
        else:
            base_amount = 0.0
            vat_amount  = 0.0

    # IRPF retention — only if explicitly in OCR text
    irpf_retention = 0.0
    m = re.search(r"(?:retenci[oó]n|irpf)[^\d\-]*(-?\s*[\d,\.]+)", ocr_text, re.IGNORECASE)
    if m:
        try:
            irpf_retention = abs(float(m.group(1).replace(",", "").replace(" ", "")))
        except ValueError:
            pass

    return {
        "transaction_type": tx_type,
        "base_amount":      base_amount,
        "vat_amount":       vat_amount,
        "irpf_retention":   irpf_retention,
        "total_with_tax":   total_with_tax,
    }


def _is_income(tx_type: str) -> bool:
    return tx_type in ("income", "credit")


# ─────────────────────── service ─────────────────────────────────────────────

class TaxEngineService:
    """
    Pure computation engine.
    Reads ledger entries that have been pre-classified by TaxClassificationService
    and aggregates financial totals per modelo.
    """

    def __init__(self):
        self.repo = TaxEngineRepository()

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
        year: int, quarter: Quarter, modelo_id: str
    ) -> Modelo303Response:
        """
        Aggregate VAT for entries pre-classified as belonging to modelo_id.

        vatPayable = outputVAT (income) - inputVAT (expense)
        """
        start, end = _quarter_date_range(year, quarter)
        raw        = self._get_entries_for_modelo(user_id, organization_id, modelo_id, start, end)
        entries    = self._filter_by_invoice_date(raw, start, end)

        totals = Modelo303Results()
        count  = 0

        for entry in entries:
            a = _extract_amounts(entry)
            if a["total_with_tax"] == 0:
                continue
            count += 1
            if _is_income(a["transaction_type"]):
                totals.total_sales += a["base_amount"]
                totals.output_vat  += a["vat_amount"]
            else:
                totals.total_expenses += a["base_amount"]
                totals.input_vat      += a["vat_amount"]

        totals.total_sales    = round(totals.total_sales, 2)
        totals.total_expenses = round(totals.total_expenses, 2)
        totals.output_vat     = round(totals.output_vat, 2)
        totals.input_vat      = round(totals.input_vat, 2)
        totals.vat_payable    = round(totals.output_vat - totals.input_vat, 2)

        report = TaxReport(
            user_id=user_id, organization_id=organization_id,
            modelo="303", year=year, quarter=quarter,
            results=totals.model_dump(),
            status=TaxReportStatus.DRAFT,
            transactions_count=count,
        )
        self.repo.upsert_tax_report(report)

        return Modelo303Response(
            period=f"{quarter} {year}", year=year, quarter=quarter,
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
        IRPF pago fraccionado.
        taxableIncome = income - expenses
        irpfPayable   = max(0, taxableIncome × 20%) - already_withheld
        """
        start, end = _quarter_date_range(year, quarter)
        raw        = self._get_entries_for_modelo(user_id, organization_id, modelo_id, start, end)
        entries    = self._filter_by_invoice_date(raw, start, end)

        totals = Modelo130Results()
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
        gross_irpf                   = round(max(0.0, totals.taxable_income * totals.irpf_rate), 2)
        totals.irpf_payable          = round(max(0.0, gross_irpf - totals.irpf_already_withheld), 2)

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
        Aggregates IRPF retention amounts from rent invoices.
        withholding_payable = sum of irpf_retention values found in OCR text.
        """
        from app.models.tax_engine import Modelo115Results, Modelo115Response
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
            totals.total_rent_base   += a["base_amount"]
            # Use explicitly extracted IRPF retention if present,
            # otherwise apply standard 19% retention rate
            if a["irpf_retention"] > 0:
                totals.withholding_payable += a["irpf_retention"]
            else:
                totals.withholding_payable += round(a["base_amount"] * totals.retention_rate, 2)

        totals.total_rent_base     = round(totals.total_rent_base, 2)
        totals.withholding_payable = round(totals.withholding_payable, 2)

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
        start, end = _quarter_date_range(year, quarter)
        raw     = self._get_entries_for_modelo(user_id, organization_id, modelo_id, start, end)
        entries = self._filter_by_invoice_date(raw, start, end)

        totals = Modelo111Results()
        count  = 0

        for entry in entries:
            a = _extract_amounts(entry)
            if a["total_with_tax"] == 0:
                continue
            count += 1
            totals.total_base    += a["base_amount"]
            totals.total_withheld += a["irpf_retention"]

        totals.total_base         = round(totals.total_base, 2)
        totals.total_withheld     = round(totals.total_withheld, 2)
        totals.withholding_payable = totals.total_withheld  # already deducted at source

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
        Annual VAT summary — aggregates all 4 quarters.
        Also reads previously saved 303 quarterly reports to compute
        quarterly_payments already made.
        """
        from app.models.tax_engine import Modelo390Results, Modelo390Response, Quarter
        start = datetime(year, 1, 1)
        end   = datetime(year, 12, 31, 23, 59, 59)
        raw     = self._get_entries_for_modelo(user_id, organization_id, modelo_id, start, end)
        entries = self._filter_by_invoice_date(raw, start, end)

        totals = Modelo390Results()
        count  = 0

        for entry in entries:
            a = _extract_amounts(entry)
            if a["total_with_tax"] == 0:
                continue
            count += 1
            if _is_income(a["transaction_type"]):
                totals.total_sales += a["base_amount"]
                totals.output_vat  += a["vat_amount"]
            else:
                totals.total_expenses += a["base_amount"]
                totals.input_vat      += a["vat_amount"]

        totals.total_sales    = round(totals.total_sales, 2)
        totals.total_expenses = round(totals.total_expenses, 2)
        totals.output_vat     = round(totals.output_vat, 2)
        totals.input_vat      = round(totals.input_vat, 2)
        totals.net_vat        = round(totals.output_vat - totals.input_vat, 2)

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
        Annual IRPF summary — aggregates all 4 quarters.
        balance_payable = annual_irpf - quarterly_130_payments - total_withheld
        """
        from app.models.tax_engine import Modelo190Results, Modelo190Response, Quarter
        start = datetime(year, 1, 1)
        end   = datetime(year, 12, 31, 23, 59, 59)
        raw     = self._get_entries_for_modelo(user_id, organization_id, modelo_id, start, end)
        entries = self._filter_by_invoice_date(raw, start, end)

        totals = Modelo190Results()
        count  = 0

        for entry in entries:
            a = _extract_amounts(entry)
            if a["total_with_tax"] == 0:
                continue
            count += 1
            if _is_income(a["transaction_type"]):
                totals.total_income   += a["base_amount"]
                totals.total_withheld += a["irpf_retention"]
            else:
                totals.total_expenses += a["base_amount"]

        totals.total_income   = round(totals.total_income, 2)
        totals.total_expenses = round(totals.total_expenses, 2)
        totals.taxable_income = round(totals.total_income - totals.total_expenses, 2)
        totals.total_withheld = round(totals.total_withheld, 2)
        totals.annual_irpf    = round(max(0.0, totals.taxable_income * totals.irpf_rate), 2)

        # Sum quarterly 130 payments already filed
        quarterly_reports = self.repo.list_tax_reports_by_modelo_no(user_id, "130", year)
        totals.quarterly_payments = round(
            sum(r.get("results", {}).get("irpf_payable", 0) for r in quarterly_reports), 2
        )

        totals.balance_payable = round(
            max(0.0, totals.annual_irpf - totals.quarterly_payments - totals.total_withheld), 2
        )

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
