"""
Invoice Models
Canonical legal financial object between vouchers and ledger entries.
"""

from pydantic import BaseModel, Field, validator, ConfigDict
from typing import Optional, List
from datetime import datetime
from decimal import Decimal
from enum import Enum
from bson import ObjectId


class InvoiceStatus(str, Enum):
    DRAFT = "draft"
    ISSUED = "issued"
    SUBMITTED = "submitted"   # Signed and accepted by AEAT VeriFactu
    LOCKED = "locked"
    CANCELLED = "cancelled"


class InvoiceType(str, Enum):
    INCOME = "income"    # Sales invoice — money coming in (customer owes us)
    EXPENSE = "expense"  # Purchase invoice — money going out (we owe supplier)


class InvoiceLineItem(BaseModel):
    description: str
    quantity: Decimal = Decimal("1.00")
    unit_price: Decimal
    vat_rate: Decimal = Decimal("21.00")
    irpf_rate: Decimal = Decimal("0.00")       # IRPF retention rate % (0 for most, 19 for rent)
    subtotal: Decimal = Decimal("0.00")        # quantity * unit_price (server-computed)
    vat_amount: Decimal = Decimal("0.00")      # subtotal * vat_rate / 100 (server-computed)
    irpf_amount: Decimal = Decimal("0.00")     # subtotal * irpf_rate / 100 (server-computed)
    total: Decimal = Decimal("0.00")           # subtotal + vat_amount - irpf_amount (server-computed)

    model_config = ConfigDict(
        json_encoders={Decimal: float},
        arbitrary_types_allowed=True,
    )


class InvoiceTotals(BaseModel):
    # Core computed fields (server-calculated from lines)
    subtotal: Decimal = Decimal("0.00")        # base amount excl. all taxes
    total_vat: Decimal = Decimal("0.00")       # VAT amount
    total_amount: Decimal = Decimal("0.00")    # subtotal + VAT + IRPF net

    # Spanish tax breakdown (mirrors OCR ledger totals structure)
    base: Decimal = Decimal("0.00")            # taxable base (= subtotal)
    vat_rate: Decimal = Decimal("0.00")        # VAT rate %
    vat_amount: Decimal = Decimal("0.00")      # VAT amount (= total_vat)
    irpf_rate: Decimal = Decimal("0.00")       # IRPF retention rate %
    irpf_amount: Decimal = Decimal("0.00")     # IRPF retention amount
    total_with_tax: Decimal = Decimal("0.00")  # amount actually payable (base + VAT - IRPF)

    # Financial classification
    income_amount: Decimal = Decimal("0.00")
    expense_amount: Decimal = Decimal("0.00")

    model_config = ConfigDict(
        json_encoders={Decimal: float},
        arbitrary_types_allowed=True,
    )


class CustomerInfo(BaseModel):
    name: str
    tax_id: Optional[str] = None
    address: Optional[str] = None
    email: Optional[str] = None


# ==================== INVOICE ====================

class Invoice(BaseModel):
    id: Optional[str] = Field(None, alias="_id")
    organization_id: str
    source_voucher_id: str
    ocr_ledger_id: Optional[str] = None        # _id of the source ledger/OCR record
    ocr_source: bool = False                    # True when auto-filled from OCR data
    invoice_type: InvoiceType = InvoiceType.INCOME
    status: InvoiceStatus = InvoiceStatus.DRAFT
    series: str = "A"
    invoice_number: Optional[str] = None
    customer: CustomerInfo
    lines: List[InvoiceLineItem]
    totals: InvoiceTotals
    ledger_entry_id: Optional[str] = None
    # VeriFactu hash chain fields
    fingerprint: Optional[str] = None
    previous_fingerprint: Optional[str] = None
    # AEAT VeriFactu submission fields
    csv_code: Optional[str] = None              # Código Seguro de Verificación from AEAT
    submitted_at: Optional[datetime] = None     # When AEAT accepted the invoice
    issued_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    cancellation_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str, Decimal: float},
        arbitrary_types_allowed=True,
    )


class InvoiceCreate(BaseModel):
    """Used internally when creating a draft from a voucher."""
    organization_id: str
    source_voucher_id: str
    invoice_type: InvoiceType = InvoiceType.INCOME
    series: str = "A"
    customer: CustomerInfo
    lines: List[InvoiceLineItem]


class InvoiceUpdate(BaseModel):
    """PATCH payload — all fields optional, only DRAFT invoices can be updated."""
    invoice_type: Optional[InvoiceType] = None
    series: Optional[str] = None
    customer: Optional[CustomerInfo] = None
    lines: Optional[List[InvoiceLineItem]] = None

    model_config = ConfigDict(
        json_encoders={Decimal: float},
        arbitrary_types_allowed=True,
    )


class InvoiceIssueResponse(BaseModel):
    invoice_id: str
    invoice_number: str
    status: InvoiceStatus
    totals: InvoiceTotals
    ledger_entry_id: str
    fingerprint: str
    previous_fingerprint: str
    issued_at: datetime
