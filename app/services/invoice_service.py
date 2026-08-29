"""
Invoice Service
Business logic for the Invoice domain.

Flow: Voucher (approved) → Invoice (draft) → Issue → Ledger entry created
"""

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional, Dict, Any
import hashlib
import json
import logging
import os
import re

import certifi
from bson import ObjectId
from pymongo import MongoClient
from pymongo.database import Database
from dotenv import load_dotenv

from app.models.invoice import (
    Invoice,
    InvoiceCreate,
    InvoiceUpdate,
    InvoiceIssueResponse,
    InvoiceLineItem,
    InvoiceStatus,
    InvoiceType,
    InvoiceTotals,
    CustomerInfo,
)
from app.repos.invoice_repository import InvoiceRepository
from app.services.manual_voucher import is_manual_voucher

load_dotenv()
logger = logging.getLogger(__name__)

# Spanish PGC account codes — INCOME invoice (sales)
INCOME_RECEIVABLE = "430"    # Clientes (Accounts Receivable)
INCOME_REVENUE = "700"       # Ventas de mercaderías (Revenue)
INCOME_VAT_PAYABLE = "477"   # H.P. IVA repercutido (Output VAT)

# Spanish PGC account codes — EXPENSE invoice (purchases)
EXPENSE_PAYABLE = "400"      # Proveedores (Accounts Payable)
EXPENSE_COST = "600"         # Compras de mercaderías (Purchases/Cost)
EXPENSE_VAT_DEDUCTIBLE = "472"  # H.P. IVA soportado (Input VAT deductible)


def _get_db() -> Database:
    client = MongoClient(os.getenv("MONGO_URI"), tlsCAFile=certifi.where())
    return client[os.getenv("DB_NAME")]


class InvoiceService:
    def __init__(self, db: Optional[Database] = None):
        self.db = db or _get_db()
        self.repo = InvoiceRepository(self.db)
        self.vouchers = self.db["voucher"]       # raw voucher collection (legacy)
        self.ocr_ledger = self.db["ledger"]      # OCR pipeline output collection
        self.ledger = self.db["ledger_entries"]  # accounting ledger

    # ==================== HELPERS ====================

    def _compute_fingerprint(
        self,
        invoice_number: str,
        organization_id: str,
        totals: InvoiceTotals,
        customer_tax_id: Optional[str],
        issued_at: datetime,
        previous_fingerprint: str,
    ) -> str:
        """
        Compute a deterministic SHA-256 fingerprint over the invoice's
        immutable fields plus the previous invoice's fingerprint.
        All values are stringified in a fixed order to guarantee reproducibility.
        """
        canonical = "|".join([
            invoice_number,
            organization_id,
            f"{totals.total_amount:.2f}",
            f"{totals.subtotal:.2f}",
            f"{totals.total_vat:.2f}",
            customer_tax_id or "",
            issued_at.strftime("%Y-%m-%dT%H:%M:%S"),
            previous_fingerprint,
        ])
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _decimals_to_float(self, data):
        if isinstance(data, dict):
            return {k: self._decimals_to_float(v) for k, v in data.items()}
        if isinstance(data, list):
            return [self._decimals_to_float(i) for i in data]
        if isinstance(data, Decimal):
            return float(data)
        return data

    def _recalculate_lines(self, lines: List[InvoiceLineItem]) -> List[InvoiceLineItem]:
        """Server-side recalculation of every line — never trust frontend values."""
        recalculated = []
        for line in lines:
            qty        = line.quantity.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            price      = line.unit_price.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            vat_rate   = line.vat_rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            irpf_rate  = line.irpf_rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            subtotal   = (qty * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            vat_amount = (subtotal * vat_rate / Decimal("100")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            irpf_amount = (subtotal * irpf_rate / Decimal("100")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            # total = what is actually paid: base + VAT - IRPF retention
            total = subtotal + vat_amount - irpf_amount

            recalculated.append(
                InvoiceLineItem(
                    description=line.description,
                    quantity=qty,
                    unit_price=price,
                    vat_rate=vat_rate,
                    irpf_rate=irpf_rate,
                    subtotal=subtotal,
                    vat_amount=vat_amount,
                    irpf_amount=irpf_amount,
                    total=total,
                )
            )
        return recalculated

    def _compute_totals(
        self,
        lines: List[InvoiceLineItem],
        invoice_type: InvoiceType = InvoiceType.INCOME,
    ) -> InvoiceTotals:
        base        = sum((l.subtotal    for l in lines), Decimal("0.00")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        vat_amount  = sum((l.vat_amount  for l in lines), Decimal("0.00")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        irpf_amount = sum((l.irpf_amount for l in lines), Decimal("0.00")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total_with_tax = (base + vat_amount - irpf_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        # Derive representative rates from first line (single-rate invoices)
        vat_rate  = lines[0].vat_rate  if lines else Decimal("0.00")
        irpf_rate = lines[0].irpf_rate if lines else Decimal("0.00")

        return InvoiceTotals(
            subtotal=base,
            total_vat=vat_amount,
            total_amount=total_with_tax,
            # Spanish tax breakdown
            base=base,
            vat_rate=vat_rate,
            vat_amount=vat_amount,
            irpf_rate=irpf_rate,
            irpf_amount=irpf_amount,
            total_with_tax=total_with_tax,
            # Financial classification
            income_amount=total_with_tax if invoice_type == InvoiceType.INCOME else Decimal("0.00"),
            expense_amount=total_with_tax if invoice_type == InvoiceType.EXPENSE else Decimal("0.00"),
        )

    def _create_ledger_entry(
        self,
        organization_id: str,
        invoice_id: str,
        invoice_number: str,
        invoice_type: InvoiceType,
        totals: InvoiceTotals,
        counterparty_name: str,
        issued_at: datetime,
    ) -> str:
        """
        Create the double-entry ledger record based on invoice type.

        INCOME (sales invoice — we earn money):
          DR 430  Clientes (Accounts Receivable)   = total_amount
          CR 700  Ventas (Revenue)                 = subtotal
          CR 477  H.P. IVA repercutido (Output VAT)= total_vat

        EXPENSE (purchase invoice — we spend money):
          DR 600  Compras (Cost/Purchases)          = subtotal
          DR 472  H.P. IVA soportado (Input VAT)   = total_vat
          CR 400  Proveedores (Accounts Payable)    = total_amount
        """
        now = datetime.utcnow()

        if invoice_type == InvoiceType.INCOME:
            entries = [
                {
                    "account_code": INCOME_RECEIVABLE,
                    "account_name": "Clientes",
                    "entry_type": "income",
                    "accounting_side": "debit",
                    "amount": float(totals.total_amount),
                },
                {
                    "account_code": INCOME_REVENUE,
                    "account_name": "Ventas",
                    "entry_type": "income",
                    "accounting_side": "credit",
                    "amount": float(totals.subtotal),
                },
                {
                    "account_code": INCOME_VAT_PAYABLE,
                    "account_name": "H.P. IVA repercutido",
                    "entry_type": "income",
                    "accounting_side": "credit",
                    "amount": float(totals.total_vat),
                },
            ]
        else:  # EXPENSE
            entries = [
                {
                    "account_code": EXPENSE_COST,
                    "account_name": "Compras",
                    "entry_type": "expense",
                    "accounting_side": "debit",
                    "amount": float(totals.subtotal),
                },
                {
                    "account_code": EXPENSE_VAT_DEDUCTIBLE,
                    "account_name": "H.P. IVA soportado",
                    "entry_type": "expense",
                    "accounting_side": "debit",
                    "amount": float(totals.total_vat),
                },
                {
                    "account_code": EXPENSE_PAYABLE,
                    "account_name": "Proveedores",
                    "entry_type": "expense",
                    "accounting_side": "credit",
                    "amount": float(totals.total_amount),
                },
            ]

        ledger_doc = {
            "organization_id": organization_id,
            "invoice_id": invoice_id,
            "invoice_number": invoice_number,
            "invoice_type": invoice_type.value,
            "description": f"Invoice {invoice_number} — {counterparty_name}",
            # Full Spanish tax breakdown
            "base": float(totals.base),
            "vat_rate": float(totals.vat_rate),
            "vat_amount": float(totals.vat_amount),
            "irpf_rate": float(totals.irpf_rate),
            "irpf_amount": float(totals.irpf_amount),
            "total_with_tax": float(totals.total_with_tax),
            "income_amount": float(totals.income_amount),
            "expense_amount": float(totals.expense_amount),
            "transaction_date": issued_at,
            "posted_at": now,
            "created_at": now,
            "entries": entries,
        }
        result = self.ledger.insert_one(ledger_doc)
        return str(result.inserted_id)

    def _extract_from_ocr(self, voucher_id: str, user_id: Optional[str] = None) -> Optional[dict]:
        """
        Fetch the OCR ledger record for this voucher.

        Lookup strategy:
          1. Exact match on voucher_id (string)
          2. Exact match on voucher_id (ObjectId) — handles type mismatch
          3. If user_id provided: most recent successful OCR record for that user
             where invoice_data exists (last-resort fallback)
        Returns None if nothing useful is found.
        """
        # Strategy 1 & 2: exact voucher_id match (string or ObjectId stored)
        ocr_doc = self.ocr_ledger.find_one(
            {"voucher_id": voucher_id, "processing_status": "success"},
            sort=[("created_at", -1)],
        )
        if not ocr_doc and ObjectId.is_valid(voucher_id):
            ocr_doc = self.ocr_ledger.find_one(
                {"voucher_id": ObjectId(voucher_id), "processing_status": "success"},
                sort=[("created_at", -1)],
            )

        # Strategy 3: fallback to most recent OCR for this user
        # Only used when no exact match — marks result as indirect
        indirect = False
        if not ocr_doc and user_id:
            ocr_doc = self.ocr_ledger.find_one(
                {
                    "user_id": user_id,
                    "processing_status": "success",
                    "invoice_data": {"$exists": True},
                },
                sort=[("created_at", -1)],
            )
            if ocr_doc:
                indirect = True
                logger.info(
                    "[Invoice] OCR fallback: using ledger %s for voucher %s",
                    ocr_doc["_id"], voucher_id,
                )

        if not ocr_doc:
            return None

        inv_data = ocr_doc.get("invoice_data") or {}
        totals   = inv_data.get("totals") or {}
        supplier = inv_data.get("supplier") or {}
        customer_ocr = inv_data.get("customer") or {}
        items    = inv_data.get("items") or []
        tx_type  = (inv_data.get("transaction_type") or "expense").lower()

        # --- Customer / counterparty ---
        NOISE = {"N/A", "Unknown Supplier", "Unknown Customer",
                 "Self/Company", "Customer (Not Specified)", "", None}

        if tx_type == "income":
            raw_name = (customer_ocr.get("company_name")
                        or supplier.get("business_name"))
        else:
            raw_name = supplier.get("business_name")

        # Strip NIF/address noise appended by OCR regex
        # e.g. "BROWN FERNANDEZ ROBERT GLASCO NIF: 55238025Y Address: AVDA AL"
        if raw_name:
            raw_name = re.sub(
                r"\s+(NIF|CIF|DNI|Address|Dirección)[:\s].*$",
                "", raw_name, flags=re.IGNORECASE
            ).strip()

        name = raw_name if raw_name not in NOISE else None

        address_raw = supplier.get("address_line1")
        address = address_raw if address_raw not in NOISE else None

        email_raw = supplier.get("Email")
        email = email_raw if email_raw not in NOISE else None

        customer = CustomerInfo(
            name=name or "Unknown",
            tax_id=None,
            address=address,
            email=email,
        )

        # --- Line items ---
        lines: List[InvoiceLineItem] = []
        if items:
            for item in items:
                desc = item.get("description") or "Service"
                qty  = Decimal(str(item.get("qty") or 1))
                unit_price = Decimal(str(
                    item.get("unit_price") or item.get("subtotal") or 0
                ))
                vat_rate  = Decimal(str(totals.get("VAT_rate")  or 0))
                irpf_rate = Decimal(str(totals.get("IRPF_rate") or 0))
                # If unit_price looks like gross (includes VAT), back-calculate base
                gross_total = Decimal(str(totals.get("Total_with_Tax") or totals.get("total") or 0))
                base_total  = Decimal(str(totals.get("base") or 0))
                if base_total > 0 and unit_price == gross_total:
                    unit_price = base_total
                lines.append(InvoiceLineItem(
                    description=desc,
                    quantity=qty,
                    unit_price=unit_price,
                    vat_rate=vat_rate,
                    irpf_rate=irpf_rate,
                ))

        if not lines:
            base      = Decimal(str(totals.get("base") or totals.get("total") or 0))
            vat_rate  = Decimal(str(totals.get("VAT_rate")  or 0))
            irpf_rate = Decimal(str(totals.get("IRPF_rate") or 0))
            desc      = (inv_data.get("invoice", {}).get("invoice_number")
                         or "Services rendered")
            lines.append(InvoiceLineItem(
                description=desc,
                quantity=Decimal("1.00"),
                unit_price=base,
                vat_rate=vat_rate,
                irpf_rate=irpf_rate,
            ))

        return {
            "ocr_ledger_id": str(ocr_doc["_id"]),
            "indirect": indirect,
            "transaction_type": tx_type,
            "customer": customer,
            "lines": lines,
            "operation_type": inv_data.get("operation_type") or "general",
            "withholding_type": inv_data.get("withholding_type") or "none",
        }

    # ==================== PUBLIC API ====================

    def create_from_voucher(self, organization_id: str, voucher_id: str) -> Invoice:
        """
        Generate a DRAFT invoice from an approved voucher.

        Data priority (highest → lowest):
          1. OCR ledger record  (invoice_data extracted by the OCR pipeline)
          2. Voucher metadata   (fields set during upload / approval)
          3. Safe defaults

        This creates a "Ready to Review" draft — the user sees pre-filled
        customer info and line items instead of a blank form, satisfying the
        VeriFactu audit trail requirement: original document → draft → issued invoice.
        """
        # Guard: only one invoice per voucher
        existing = self.repo.get_by_voucher(organization_id, voucher_id)
        if existing:
            raise ValueError(
                f"Invoice already exists for voucher {voucher_id}: {existing.id}"
            )

        # Fetch voucher (scoped by user_id == organization_id)
        voucher = self.vouchers.find_one({
            "_id": ObjectId(voucher_id),
            "user_id": organization_id,
        })
        if not voucher:
            voucher = self.vouchers.find_one({"_id": ObjectId(voucher_id)})
        if not voucher:
            raise ValueError(f"Voucher {voucher_id} not found")

        if voucher.get("status") != "approved":
            raise ValueError(
                f"Voucher must be 'approved' before generating an invoice. "
                f"Current status: '{voucher.get('status')}'. "
                f"Use POST /api/accounting/voucher/approve to approve it first."
            )

        # ── Step 1: Try OCR / ledger data first ───────────────────────────────
        ocr_data = self._extract_from_ocr(voucher_id, user_id=organization_id)
        manual = is_manual_voucher(voucher)
        ocr_source = (
            ocr_data is not None
            and not ocr_data.get("indirect", False)
            and not manual
        )
        ocr_ledger_id = ocr_data.get("ocr_ledger_id") if ocr_data else None

        # ── Step 2: Determine invoice_type ───────────────────────────────────
        # Priority: OCR transaction_type → voucher category/transaction_type → default INCOME
        if ocr_data:
            tx = ocr_data["transaction_type"]
            invoice_type = InvoiceType.EXPENSE if tx == "expense" else InvoiceType.INCOME
        else:
            category = (voucher.get("category") or "").lower()
            tx = (voucher.get("transaction_type") or "").lower()
            if category in ("bill", "expense", "purchase") or tx == "debit":
                invoice_type = InvoiceType.EXPENSE
            else:
                invoice_type = InvoiceType.INCOME

        # ── Step 3: Build customer ────────────────────────────────────────────
        if ocr_data:
            customer = ocr_data["customer"]
            # Overlay voucher-level fields if OCR left them blank or unknown
            if not customer.name or customer.name in ("Unknown", "Unknown Customer", "Unknown Supplier"):
                title = voucher.get("title") or ""
                voucher_name = title if len(title) > 3 and not title.strip().isdigit() else None
                customer = CustomerInfo(
                    name=voucher.get("vendor_name") or voucher_name or customer.name,
                    tax_id=customer.tax_id or voucher.get("vendor_tax_id") or voucher.get("tax_id"),
                    address=customer.address or voucher.get("vendor_address"),
                    email=customer.email or voucher.get("vendor_email"),
                )
        else:
            # Only use voucher title if it looks like a real name (>3 chars, not numeric)
            title = voucher.get("title") or ""
            voucher_name = title if len(title) > 3 and not title.strip().isdigit() else None
            customer = CustomerInfo(
                name=voucher.get("vendor_name") or voucher_name or "Unknown Customer",
                tax_id=voucher.get("vendor_tax_id") or voucher.get("tax_id"),
                address=voucher.get("vendor_address"),
                email=voucher.get("vendor_email"),
            )

        # ── Step 4: Build line items ──────────────────────────────────────────
        if ocr_data:
            lines = ocr_data["lines"]
        else:
            raw_lines = voucher.get("line_items") or []
            if raw_lines:
                lines = [
                    InvoiceLineItem(
                        description=item.get("description", "Service"),
                        quantity=Decimal(str(item.get("quantity", 1))),
                        unit_price=Decimal(str(item.get("unit_price", 0))),
                        vat_rate=Decimal(str(item.get("vat_rate", 21))),
                    )
                    for item in raw_lines
                ]
            else:
                amount = Decimal(str(voucher.get("total_amount") or voucher.get("amount") or 0))
                vat_rate = Decimal(str(voucher.get("vat_rate") or 21))
                base = (amount / (1 + vat_rate / 100)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                lines = [
                    InvoiceLineItem(
                        description=voucher.get("description") or "Services rendered",
                        quantity=Decimal("1.00"),
                        unit_price=base,
                        vat_rate=vat_rate,
                    )
                ]

        # ── Step 5: Server-side recalculation ─────────────────────────────────
        lines = self._recalculate_lines(lines)
        totals = self._compute_totals(lines, invoice_type)

        invoice = Invoice(
            organization_id=organization_id,
            source_voucher_id=voucher_id,
            ocr_ledger_id=ocr_ledger_id,
            ocr_source=ocr_source,
            source="manual" if manual else ("ocr" if ocr_source else None),
            invoice_type=invoice_type,
            status=InvoiceStatus.DRAFT,
            series=voucher.get("invoice_series", "A"),
            customer=customer,
            lines=lines,
            totals=totals,
            operation_type=(ocr_data or {}).get("operation_type") or "general",
            withholding_type=(ocr_data or {}).get("withholding_type") or "none",
        )

        created = self.repo.create(invoice)
        logger.info(
            "[Invoice] Draft created %s from voucher %s — ocr_source=%s",
            created.id, voucher_id, ocr_source,
        )
        return created

    def issue(self, organization_id: str, invoice_id: str) -> InvoiceIssueResponse:
        """
        Legal issuance action:
        1. Validate invoice is DRAFT and belongs to org
        2. Recalculate totals server-side
        3. Assign atomic sequential invoice number
        4. Create double-entry ledger record
        5. Lock invoice as ISSUED (immutable from this point)
        """
        invoice = self.repo.get_by_id(organization_id, invoice_id)
        if not invoice:
            raise ValueError(f"Invoice {invoice_id} not found")

        if invoice.status != InvoiceStatus.DRAFT:
            raise ValueError(
                f"Only DRAFT invoices can be issued (current status: {invoice.status})"
            )

        # Server-side recalculation — never trust stored draft values
        lines = self._recalculate_lines(invoice.lines)
        totals = self._compute_totals(lines, invoice.invoice_type)

        # Atomic sequential number (gap-free)
        invoice_number = self.repo.next_invoice_number(organization_id, invoice.series)

        issued_at = datetime.utcnow()

        # --- VeriFactu hash chain ---
        previous_fingerprint = self.repo.get_last_fingerprint(organization_id)
        fingerprint = self._compute_fingerprint(
            invoice_number=invoice_number,
            organization_id=organization_id,
            totals=totals,
            customer_tax_id=invoice.customer.tax_id,
            issued_at=issued_at,
            previous_fingerprint=previous_fingerprint,
        )

        # Create ledger entry first so we have its ID
        ledger_entry_id = self._create_ledger_entry(
            organization_id=organization_id,
            invoice_id=invoice_id,
            invoice_number=invoice_number,
            invoice_type=invoice.invoice_type,
            totals=totals,
            counterparty_name=invoice.customer.name,
            issued_at=issued_at,
        )

        # Atomically transition to ISSUED (guard: status must still be draft)
        issued = self.repo.issue(
            organization_id=organization_id,
            invoice_id=invoice_id,
            invoice_number=invoice_number,
            totals=totals,
            ledger_entry_id=ledger_entry_id,
            fingerprint=fingerprint,
            previous_fingerprint=previous_fingerprint,
            issued_at=issued_at,
        )

        if not issued:
            raise ValueError(
                "Invoice could not be issued — it may have already been issued by a concurrent request"
            )

        return InvoiceIssueResponse(
            invoice_id=invoice_id,
            invoice_number=invoice_number,
            status=InvoiceStatus.ISSUED,
            totals=totals,
            ledger_entry_id=ledger_entry_id,
            fingerprint=fingerprint,
            previous_fingerprint=previous_fingerprint,
            issued_at=issued_at,
        )

    def update(self, organization_id: str, invoice_id: str, data: InvoiceUpdate) -> Invoice:
        """
        Update customer info and/or line items on a DRAFT invoice.
        Recalculates line totals server-side from the incoming lines so the
        stored draft always has correct computed values ready for issuance.
        Raises ValueError if the invoice is not found or not in DRAFT status.
        """
        invoice = self.repo.get_by_id(organization_id, invoice_id)
        if not invoice:
            raise ValueError(f"Invoice {invoice_id} not found")
        if invoice.status != InvoiceStatus.DRAFT:
            raise ValueError(
                f"Only DRAFT invoices can be edited (current status: {invoice.status})"
            )

        fields: dict = {}

        if data.invoice_type is not None:
            fields["invoice_type"] = data.invoice_type.value

        if data.series is not None:
            fields["series"] = data.series

        if data.customer is not None:
            fields["customer"] = self._decimals_to_float(data.customer.dict())

        if data.operation_type is not None:
            fields["operation_type"] = data.operation_type
        if data.withholding_type is not None:
            fields["withholding_type"] = data.withholding_type

        if data.lines is not None:
            effective_type = data.invoice_type or invoice.invoice_type
            lines = self._recalculate_lines(data.lines)
            totals = self._compute_totals(lines, effective_type)
            fields["lines"] = self._decimals_to_float([l.dict() for l in lines])
            fields["totals.subtotal"]       = float(totals.subtotal)
            fields["totals.total_vat"]      = float(totals.total_vat)
            fields["totals.total_amount"]   = float(totals.total_amount)
            fields["totals.base"]           = float(totals.base)
            fields["totals.vat_rate"]       = float(totals.vat_rate)
            fields["totals.vat_amount"]     = float(totals.vat_amount)
            fields["totals.irpf_rate"]      = float(totals.irpf_rate)
            fields["totals.irpf_amount"]    = float(totals.irpf_amount)
            fields["totals.total_with_tax"] = float(totals.total_with_tax)
            fields["totals.income_amount"]  = float(totals.income_amount)
            fields["totals.expense_amount"] = float(totals.expense_amount)

        if not fields:
            return invoice  # nothing to update

        updated = self.repo.update_draft(organization_id, invoice_id, fields)
        if not updated:
            raise ValueError(
                "Update failed — invoice may no longer be in DRAFT status"
            )
        self._sync_tax_nature_to_ledger(updated)
        return updated

    def _sync_tax_nature_to_ledger(self, invoice: Invoice) -> None:
        """Keep the OCR ledger tax nature in sync so tax filing uses the same fields."""
        ledger_id = invoice.ocr_ledger_id
        if not ledger_id or not ObjectId.is_valid(str(ledger_id)):
            return
        entry = self.ocr_ledger.find_one({"_id": ObjectId(str(ledger_id))})
        if not entry:
            return
        invoice_data = dict(entry.get("invoice_data") or {})
        invoice_data["operation_type"] = invoice.operation_type or "general"
        invoice_data["withholding_type"] = invoice.withholding_type or "none"
        totals = dict(invoice_data.get("totals") or {})
        totals["vat_regime"] = invoice.operation_type or "general"
        invoice_data["totals"] = totals
        self.ocr_ledger.update_one(
            {"_id": ObjectId(str(ledger_id))},
            {"$set": {"invoice_data": invoice_data, "updated_at": datetime.utcnow()}},
        )
        user_id = str(entry.get("user_id") or invoice.organization_id)
        try:
            from app.services.tax_classification_service import TaxClassificationService
            TaxClassificationService().classify_ledger_entry(str(ledger_id), user_id)
        except Exception as exc:
            logger.warning("[Invoice] tax reclassify after draft save failed: %s", exc)

    def refresh_ocr(self, organization_id: str, invoice_id: str) -> Invoice:
        """
        Re-run the OCR lookup on an existing DRAFT invoice and overwrite
        customer, lines, totals, invoice_type, ocr_source, ocr_ledger_id
        with the latest data from the ledger collection.
        Useful when the invoice was created before OCR finished, or before
        the OCR auto-fill fix was deployed.
        Raises ValueError if invoice not found or not in DRAFT status.
        """
        invoice = self.repo.get_by_id(organization_id, invoice_id)
        if not invoice:
            raise ValueError(f"Invoice {invoice_id} not found")
        if invoice.status != InvoiceStatus.DRAFT:
            raise ValueError(
                f"Only DRAFT invoices can be refreshed (current status: {invoice.status})"
            )
        if invoice.source == "manual" or is_manual_voucher(
            self.vouchers.find_one({"_id": ObjectId(invoice.source_voucher_id)})
            if ObjectId.is_valid(invoice.source_voucher_id) else None
        ):
            raise ValueError("This invoice was entered by hand. OCR is not used.")

        ocr_data = self._extract_from_ocr(
            invoice.source_voucher_id,
            user_id=organization_id,
        )
        if not ocr_data:
            raise ValueError(
                "No OCR data found for this voucher. "
                "Run OCR first via POST /api/accounting/ocr/voucher_ocr"
            )

        tx = ocr_data["transaction_type"]
        invoice_type = InvoiceType.EXPENSE if tx == "expense" else InvoiceType.INCOME
        lines = self._recalculate_lines(ocr_data["lines"])
        totals = self._compute_totals(lines, invoice_type)
        customer = ocr_data["customer"]

        fields = {
            "invoice_type": invoice_type.value,
            "ocr_source": not ocr_data.get("indirect", False),
            "ocr_ledger_id": ocr_data["ocr_ledger_id"],
            "operation_type": ocr_data.get("operation_type") or "general",
            "withholding_type": ocr_data.get("withholding_type") or "none",
            "customer": self._decimals_to_float(customer.dict()),
            "lines": self._decimals_to_float([l.dict() for l in lines]),
            "totals.subtotal":       float(totals.subtotal),
            "totals.total_vat":      float(totals.total_vat),
            "totals.total_amount":   float(totals.total_amount),
            "totals.base":           float(totals.base),
            "totals.vat_rate":       float(totals.vat_rate),
            "totals.vat_amount":     float(totals.vat_amount),
            "totals.irpf_rate":      float(totals.irpf_rate),
            "totals.irpf_amount":    float(totals.irpf_amount),
            "totals.total_with_tax": float(totals.total_with_tax),
            "totals.income_amount":  float(totals.income_amount),
            "totals.expense_amount": float(totals.expense_amount),
        }

        updated = self.repo.update_draft(organization_id, invoice_id, fields)
        if not updated:
            raise ValueError("Refresh failed — invoice may no longer be in DRAFT status")

        logger.info(
            "[Invoice] OCR refresh applied to draft %s — ocr_ledger_id=%s",
            invoice_id, ocr_data["ocr_ledger_id"],
        )
        return updated

    def get(self, organization_id: str, invoice_id: str) -> Invoice:
        invoice = self.repo.get_by_id(organization_id, invoice_id)
        if not invoice:
            raise ValueError(f"Invoice {invoice_id} not found")
        return invoice

    def list(
        self,
        organization_id: str,
        status: Optional[InvoiceStatus] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Invoice]:
        return self.repo.list_by_org(organization_id, status, limit, offset)

    def verify_chain(self, organization_id: str) -> Dict[str, Any]:
        """
        Walk all issued invoices in chronological order and verify the hash chain.
        Recomputes each fingerprint and checks it matches the stored value,
        and that each previous_fingerprint matches the prior invoice's fingerprint.
        Returns a report with pass/fail and the first broken link if any.
        """
        invoices = self.repo.get_chain_for_verification(organization_id)

        if not invoices:
            return {"valid": True, "total": 0, "message": "No issued invoices found"}

        errors = []
        expected_previous = "0"

        for doc in invoices:
            inv_number = doc.get("invoice_number", "")
            stored_fp = doc.get("fingerprint")
            stored_prev = doc.get("previous_fingerprint")
            totals_raw = doc.get("totals", {})
            customer_raw = doc.get("customer", {})
            issued_at = doc.get("issued_at")

            # Check previous_fingerprint linkage
            if stored_prev != expected_previous:
                errors.append({
                    "invoice_number": inv_number,
                    "error": "broken_chain",
                    "expected_previous_fingerprint": expected_previous,
                    "stored_previous_fingerprint": stored_prev,
                })
                # Continue walking — report all breaks, not just the first
                expected_previous = stored_fp or ""
                continue

            # Recompute fingerprint
            totals = InvoiceTotals(
                subtotal=Decimal(str(totals_raw.get("subtotal", 0))),
                total_vat=Decimal(str(totals_raw.get("total_vat", 0))),
                total_amount=Decimal(str(totals_raw.get("total_amount", 0))),
                base=Decimal(str(totals_raw.get("base", 0))),
                vat_rate=Decimal(str(totals_raw.get("vat_rate", 0))),
                vat_amount=Decimal(str(totals_raw.get("vat_amount", 0))),
                irpf_rate=Decimal(str(totals_raw.get("irpf_rate", 0))),
                irpf_amount=Decimal(str(totals_raw.get("irpf_amount", 0))),
                total_with_tax=Decimal(str(totals_raw.get("total_with_tax", 0))),
            )
            recomputed = self._compute_fingerprint(
                invoice_number=inv_number,
                organization_id=organization_id,
                totals=totals,
                customer_tax_id=customer_raw.get("tax_id"),
                issued_at=issued_at,
                previous_fingerprint=expected_previous,
            )

            if recomputed != stored_fp:
                errors.append({
                    "invoice_number": inv_number,
                    "error": "fingerprint_mismatch",
                    "stored_fingerprint": stored_fp,
                    "recomputed_fingerprint": recomputed,
                })

            expected_previous = stored_fp or ""

        return {
            "valid": len(errors) == 0,
            "total": len(invoices),
            "errors": errors,
            "message": "Chain intact" if not errors else f"{len(errors)} integrity violation(s) detected",
        }

    def cancel(self, organization_id: str, invoice_id: str, reason: str) -> Invoice:
        invoice = self.repo.get_by_id(organization_id, invoice_id)
        if not invoice:
            raise ValueError(f"Invoice {invoice_id} not found")
        if invoice.status != InvoiceStatus.ISSUED:
            raise ValueError("Only ISSUED invoices can be cancelled")

        cancelled = self.repo.cancel(organization_id, invoice_id, reason)
        if not cancelled:
            raise ValueError("Cancellation failed — invoice may have changed state")
        return cancelled

    def mark_submitted(
        self, organization_id: str, invoice_id: str, csv_code: Optional[str]
    ) -> None:
        """
        Update invoice status to SUBMITTED and store the AEAT CSV code.
        Called after a successful AEAT VeriFactu submission.
        """
        from datetime import datetime as _dt
        self.repo.invoices.update_one(
            {
                "_id": ObjectId(invoice_id),
                "organization_id": organization_id,
                "status": InvoiceStatus.ISSUED.value,
            },
            {
                "$set": {
                    "status": InvoiceStatus.SUBMITTED.value,
                    "csv_code": csv_code,
                    "submitted_at": _dt.utcnow(),
                    "updated_at": _dt.utcnow(),
                }
            },
        )
