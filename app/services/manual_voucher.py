"""Build OCR-compatible invoice_data from a typed (manual) voucher payload."""

from typing import List, Literal, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


def round_money(value: float) -> float:
    return round(float(value or 0), 2)


class ManualParty(BaseModel):
    name: Optional[str] = None
    business_name: Optional[str] = None
    company_name: Optional[str] = None
    nif: Optional[str] = None
    nif_nie: Optional[str] = None
    tax_id: Optional[str] = None
    cif: Optional[str] = None
    address: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    Email: Optional[str] = None
    email: Optional[str] = None


class ManualLineItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    description: str
    qty: float = 1
    unit_price: float
    vat_percent: float = Field(
        0,
        validation_alias=AliasChoices("vat_percent", "VAT %", "vat_rate", "VAT"),
    )
    irpf_percent: float = Field(
        0,
        validation_alias=AliasChoices("irpf_percent", "IRPF %", "irpf_rate", "IRPF"),
    )

    @model_validator(mode="after")
    def validate_line(self):
        if not (self.description or "").strip():
            raise ValueError("Each item needs a description")
        if self.qty <= 0:
            raise ValueError("Item qty must be greater than 0")
        return self


class ManualTotals(BaseModel):
    base: Optional[float] = None
    VAT_amount: Optional[float] = None
    IRPF_amount: Optional[float] = None
    Total_with_Tax: Optional[float] = None


class ManualVoucherCreate(BaseModel):
    user_id: str
    title: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    period: str
    transaction_type: Literal["credit", "debit"]
    supplier: Optional[ManualParty] = None
    customer: Optional[ManualParty] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None
    items: List[ManualLineItem]
    totals: Optional[ManualTotals] = None

    @model_validator(mode="after")
    def validate_manual(self):
        if not self.items:
            raise ValueError("At least one line item is required")
        if not (party_display_name(self.supplier, "business_name") or party_display_name(self.customer, "company_name")):
            raise ValueError("Provide a supplier or customer name")
        return self


def party_display_name(party: Optional[ManualParty], preferred: str) -> str:
    if not party:
        return ""
    data = party.model_dump()
    return (
        (data.get(preferred) or "")
        or (data.get("name") or "")
        or (data.get("business_name") or "")
        or (data.get("company_name") or "")
    ).strip()


def party_nif(party: Optional[ManualParty]) -> Optional[str]:
    if not party:
        return None
    for key in ("nif", "nif_nie", "tax_id", "cif"):
        value = getattr(party, key, None)
        if value and str(value).strip():
            return str(value).strip()
    return None


def party_address(party: Optional[ManualParty]) -> tuple:
    if not party:
        return "N/A", "N/A"
    line1 = (party.address_line1 or party.address or "").strip() or "N/A"
    line2 = (party.address_line2 or "").strip() or "N/A"
    return line1, line2


def party_email(party: Optional[ManualParty]) -> str:
    if not party:
        return "N/A"
    return (party.Email or party.email or "").strip() or "N/A"


def _party_doc(party: Optional[ManualParty], name_field: str, name: str) -> dict:
    line1, line2 = party_address(party)
    doc = {
        name_field: name or "N/A",
        "address_line1": line1,
        "address_line2": line2,
        "Email": party_email(party),
    }
    nif = party_nif(party)
    if nif:
        doc["nif"] = nif
        doc["tax_id"] = nif
    return doc


def build_manual_invoice_data(body: ManualVoucherCreate) -> dict:
    nested_tx = "income" if body.transaction_type == "credit" else "expense"

    line_items = []
    vat_total = 0.0
    irpf_total = 0.0
    base_total = 0.0
    for item in body.items:
        qty = float(item.qty)
        unit_price = float(item.unit_price)
        subtotal = round_money(qty * unit_price)
        vat_pct = float(item.vat_percent or 0)
        irpf_pct = float(item.irpf_percent or 0)
        base_total += subtotal
        vat_total += round_money(subtotal * vat_pct / 100)
        irpf_total += round_money(subtotal * irpf_pct / 100)
        line_items.append({
            "description": item.description.strip(),
            "qty": qty,
            "unit_price": round_money(unit_price),
            "subtotal": subtotal,
            "vat_percent": vat_pct,
            "irpf_percent": irpf_pct,
        })

    base_total = round_money(base_total)
    vat_total = round_money(vat_total)
    irpf_total = round_money(irpf_total)
    payable = round_money(base_total + vat_total - irpf_total)
    vat_rate = round_money((vat_total / base_total * 100) if base_total else 0)
    irpf_rate = round_money((irpf_total / base_total * 100) if base_total else 0)

    supplier_name = party_display_name(body.supplier, "business_name")
    customer_name = party_display_name(body.customer, "company_name")

    return {
        "transaction_type": nested_tx,
        "supplier": _party_doc(body.supplier, "business_name", supplier_name),
        "customer": _party_doc(body.customer, "company_name", customer_name),
        "invoice": {
            "invoice_number": (body.invoice_number or "").strip() or "N/A",
            "invoice_date": (body.invoice_date or "").strip() or "N/A",
            "due_date": "N/A",
            "amount_in_words": "N/A",
        },
        "items": line_items,
        "totals": {
            "base": base_total,
            "total": base_total,
            "VAT_rate": vat_rate,
            "VAT_amount": vat_total,
            "IRPF_rate": irpf_rate,
            "IRPF_amount": irpf_total,
            "Total_with_Tax": payable,
        },
    }


def manual_ocr_text(invoice_data: dict) -> str:
    totals = invoice_data.get("totals") or {}
    invoice = invoice_data.get("invoice") or {}
    supplier = invoice_data.get("supplier") or {}
    parts = [
        f"Manual invoice {invoice.get('invoice_number')}",
        f"Date {invoice.get('invoice_date')}",
        f"Supplier {supplier.get('business_name')}",
        f"NIF {supplier.get('nif') or ''}",
        f"Base {totals.get('base')}",
        f"VAT (IVA) {totals.get('VAT_rate')}% amount {totals.get('VAT_amount')}",
        f"IRPF retention {totals.get('IRPF_rate')}% amount {totals.get('IRPF_amount')}",
        f"Total_with_Tax {totals.get('Total_with_Tax')}",
    ]
    for item in invoice_data.get("items") or []:
        parts.append(
            f"{item.get('description')} qty {item.get('qty')} "
            f"VAT {item.get('vat_percent')}% IRPF {item.get('irpf_percent')}%"
        )
    return "\n".join(parts)
