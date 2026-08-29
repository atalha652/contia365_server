"""
Tax Calculation Engine Models
Covers: Modelo 303 (IVA) and Modelo 130 (IRPF)
"""

from datetime import datetime
from typing import Optional, List, Dict, Any
from enum import Enum
from pydantic import BaseModel, Field, ConfigDict, field_validator
from bson import ObjectId


class TaxReportStatus(str, Enum):
    DRAFT = "draft"
    FINALIZED = "finalized"
    FILED = "filed"


class TransactionType(str, Enum):
    INCOME = "income"       # Repercutido — sales/revenue invoices
    EXPENSE = "expense"     # Soportado — purchase/cost invoices


class Quarter(str, Enum):
    Q1 = "Q1"
    Q2 = "Q2"
    Q3 = "Q3"
    Q4 = "Q4"


# ─────────────────────── Modelo 303 (IVA / VAT) ─────────────────────────────

class VatByRateBucket(BaseModel):
    output_base: float = 0.0
    output_vat: float = 0.0
    input_base: float = 0.0
    input_vat: float = 0.0


class RecargoBucket(BaseModel):
    base: float = 0.0
    vat: float = 0.0


class Modelo303Results(BaseModel):
    total_sales: float = 0.0          # Base imponible ventas (régimen general)
    total_expenses: float = 0.0       # Base imponible compras (régimen general)
    output_vat: float = 0.0           # IVA repercutido RG (on sales)
    input_vat: float = 0.0            # IVA soportado RG (on purchases)
    vat_payable: float = 0.0          # casilla 46 = [27] − [45]
    vat_by_rate: Dict[str, VatByRateBucket] = Field(default_factory=dict)
    isp_base: float = 0.0             # casilla 12
    isp_vat: float = 0.0              # casilla 13
    intra_base: float = 0.0           # casilla 10
    intra_vat: float = 0.0            # casilla 11
    import_base: float = 0.0          # casilla 32
    import_vat: float = 0.0           # casilla 33
    investment_base: float = 0.0      # casilla 30
    investment_vat: float = 0.0       # casilla 31
    recargo_by_rate: Dict[str, RecargoBucket] = Field(default_factory=dict)
    recargo_vat: float = 0.0
    used_goods_base: float = 0.0
    prorrata_percent: float = 100.0
    prorrata_especial: bool = False
    input_vat_deductible: float = 0.0  # casilla 45 after prorrata


# ─────────────────────── Modelo 130 (IRPF) ──────────────────────────────────

class Modelo130Results(BaseModel):
    total_income: float = 0.0         # Total ingresos del trimestre
    total_expenses: float = 0.0       # Total gastos deducibles del trimestre
    taxable_income: float = 0.0       # total_income - total_expenses
    irpf_rate: float = 0.20           # 0.20 estimación directa; 0.07 starters (from régimen)
    irpf_already_withheld: float = 0.0  # Retenciones a cuenta ya practicadas (from OCR only)
    prior_payments: float = 0.0       # Casilla 05 — 130 payments already made this year
    irpf_payable: float = 0.0         # max(0, YTD profit × rate − withheld − prior_payments)


# ─────────────────────── Generic Tax Report ─────────────────────────────────

class TaxReport(BaseModel):
    id: Optional[str] = Field(None, alias="_id")
    user_id: str
    organization_id: str
    modelo: str                        # "303" or "130"
    year: int
    quarter: Optional[str] = None      # Q1-Q4, or M01-M12 for monthly 303
    period_key: Optional[str] = None   # Q2, ANNUAL, or 2026-03
    results: dict                      # Modelo303Results or Modelo130Results as dict
    status: TaxReportStatus = TaxReportStatus.DRAFT
    transactions_count: int = 0
    calculated_at: datetime = Field(default_factory=datetime.utcnow)
    finalized_at: Optional[datetime] = None
    filed_at: Optional[datetime] = None

    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
        json_encoders={ObjectId: str, datetime: lambda v: v.isoformat()},
    )

    @field_validator("quarter", mode="before")
    @classmethod
    def _coerce_quarter(cls, value):
        if value is None:
            return None
        return value.value if isinstance(value, Quarter) else str(value)


# ─────────────────────── Request / Response ─────────────────────────────────

class TaxCalculationRequest(BaseModel):
    year: int = Field(..., description="Fiscal year e.g. 2026")
    quarter: Optional[Quarter] = Field(None, description="Q1 | Q2 | Q3 | Q4")
    month: Optional[int] = Field(None, ge=1, le=12, description="1-12 for monthly 303")


class Modelo303Response(BaseModel):
    modelo: str = "303"
    period: str                        # e.g. "Q1 2026" or "03 2026"
    year: int
    quarter: Optional[str] = None      # Q1-Q4 for quarterly; omitted when monthly
    month: Optional[int] = None        # 1-12 when monthly (REDEME)
    period_key: Optional[str] = None
    redeme: bool = False
    totals: Modelo303Results
    status: TaxReportStatus
    transactions_count: int
    calculated_at: str

    @field_validator("quarter", mode="before")
    @classmethod
    def _coerce_303_quarter(cls, value):
        if value is None:
            return None
        return value.value if isinstance(value, Quarter) else str(value)


class Modelo130Response(BaseModel):
    modelo: str = "130"
    period: str
    year: int
    quarter: Quarter
    totals: Modelo130Results
    status: TaxReportStatus
    transactions_count: int
    calculated_at: str


class TaxReportUpdateStatus(BaseModel):
    status: TaxReportStatus


# ─────────────────────── Modelo 115 (Rent withholding) ──────────────────────

class Modelo115Results(BaseModel):
    total_rent_base: float = 0.0        # Base imponible alquileres
    retention_rate: float = 0.0         # effective withheld / base (0 if invoice has no IRPF)
    withholding_payable: float = 0.0    # sum of IRPF printed on rent invoices (never invented)
    percipient_count: int = 0


class Modelo115Response(BaseModel):
    modelo: str = "115"
    period: str
    year: int
    quarter: Quarter
    totals: Modelo115Results
    status: TaxReportStatus
    transactions_count: int
    calculated_at: str


# ─────────────────────── Modelo 111 (Employee / professional withholding) ───

class Modelo111Results(BaseModel):
    total_base: float = 0.0             # Base sujeta a retención
    total_withheld: float = 0.0         # Total retenciones practicadas (from OCR)
    withholding_payable: float = 0.0    # = total_withheld (already deducted at source)
    percipient_count: int = 0
    legally_complete: bool = False
    lines: List[Dict[str, Any]] = Field(default_factory=list)


class Modelo111Response(BaseModel):
    modelo: str = "111"
    period: str
    year: int
    quarter: Quarter
    totals: Modelo111Results
    status: TaxReportStatus
    transactions_count: int
    calculated_at: str


# ─────────────────────── Modelo 390 (Annual VAT summary) ────────────────────

class Modelo390Results(BaseModel):
    total_sales: float = 0.0
    total_expenses: float = 0.0
    output_vat: float = 0.0
    input_vat: float = 0.0
    net_vat: float = 0.0                # annual casilla-style [27] − [45]
    quarterly_payments: float = 0.0     # Sum of 303 payments already made
    vat_by_rate: Dict[str, VatByRateBucket] = Field(default_factory=dict)
    isp_base: float = 0.0
    isp_vat: float = 0.0
    intra_base: float = 0.0
    intra_vat: float = 0.0
    import_base: float = 0.0
    import_vat: float = 0.0
    investment_base: float = 0.0
    investment_vat: float = 0.0
    recargo_by_rate: Dict[str, RecargoBucket] = Field(default_factory=dict)
    recargo_vat: float = 0.0
    used_goods_base: float = 0.0
    prorrata_percent: float = 100.0
    prorrata_especial: bool = False
    regimen_simplificado: bool = False
    input_vat_deductible: float = 0.0


class Modelo390Response(BaseModel):
    modelo: str = "390"
    period: str                         # Full year e.g. "2026"
    year: int
    quarter: Quarter                    # Always Q4 (annual filing)
    totals: Modelo390Results
    status: TaxReportStatus
    transactions_count: int
    calculated_at: str


# ─────────────────────── Modelo 190 (Annual IRPF summary) ───────────────────

class Modelo190Results(BaseModel):
    """Annual withholding summary (resumen de retenciones), not an income return."""
    total_base: float = 0.0
    total_withheld: float = 0.0
    percipient_count: int = 0
    legally_complete: bool = False
    lines: List[Dict[str, Any]] = Field(default_factory=list)


class Modelo190Response(BaseModel):
    modelo: str = "190"
    period: str
    year: int
    quarter: Quarter                    # Always Q4
    totals: Modelo190Results
    status: TaxReportStatus
    transactions_count: int
    calculated_at: str


# ─────────────────────── Generic calculate request ──────────────────────────

class GenericCalcRequest(BaseModel):
    year: int
    quarter: str       # "Q1" | "Q2" | "Q3" | "Q4"
    modelo_id: str     # MongoDB _id from modelos collection
