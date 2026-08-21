"""
Facturae Service
================
Generates two compliance artifacts for issued invoices:

1. VeriFactu QR "legal string" — a URL-encoded string that encodes the
   invoice's key fields for the AEAT QR code (Orden HAC/1177/2024).

2. Facturae 3.2.2 XML — the structured B2B electronic invoice format
   required by Spanish public administrations (FACe) and large corporates.

Usage:
    service = FacturaeService(seller_info)
    qr_string = service.build_qr_string(invoice)
    xml_bytes  = service.build_facturae_xml(invoice)
"""

from __future__ import annotations

import hashlib
import urllib.parse
from datetime import datetime
from decimal import Decimal
from typing import Optional
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom

from app.models.invoice import Invoice, InvoiceType


# ---------------------------------------------------------------------------
# Seller / Issuer info — passed in at construction time (from user profile /
# census data).  All fields are strings to keep the service dependency-free.
# ---------------------------------------------------------------------------

class SellerInfo:
    """Issuer (seller / emisor) identity for Facturae XML and QR string."""

    def __init__(
        self,
        tax_id: str,                    # NIF / CIF  e.g. "B12345678"
        name: str,                      # Legal name  e.g. "Acme SL"
        address: str = "",
        postal_code: str = "",
        city: str = "",
        province: str = "",
        country_code: str = "ESP",      # ISO 3166-1 alpha-3
    ):
        self.tax_id = tax_id
        self.name = name
        self.address = address
        self.postal_code = postal_code
        self.city = city
        self.province = province
        self.country_code = country_code


# ---------------------------------------------------------------------------
# Facturae Service
# ---------------------------------------------------------------------------

class FacturaeService:
    """
    Stateless service — create one instance per request (or share if seller
    info is the same across requests).
    """

    # VeriFactu verification endpoint (AEAT sandbox / production)
    VERIFACTU_BASE_URL = "https://prewww2.aeat.es/wlpl/TIKE-CONT/ValidarQR"

    def __init__(self, seller: SellerInfo):
        self.seller = seller

    # =========================================================================
    # 1. QR Legal String
    # =========================================================================

    def build_qr_string(self, invoice: Invoice) -> str:
        """
        Build the VeriFactu QR URL string per Orden HAC/1177/2024.

        Format (URL-encoded query string appended to the AEAT verification URL):
          nif=<seller_nif>
          &numserie=<invoice_number>
          &fecha=<DD-MM-YYYY>
          &importe=<total_with_tax, 2 decimals>

        The full URL is what gets encoded into the QR image on the PDF.
        """
        if not invoice.invoice_number:
            raise ValueError("Invoice must be issued (have an invoice_number) before generating QR")

        issued_date = (invoice.issued_at or datetime.utcnow()).strftime("%d-%m-%Y")
        total = float(invoice.totals.total_with_tax or invoice.totals.total_amount)

        params = {
            "nif":      self.seller.tax_id,
            "numserie": invoice.invoice_number,
            "fecha":    issued_date,
            "importe":  f"{total:.2f}",
        }

        query = urllib.parse.urlencode(params)
        return f"{self.VERIFACTU_BASE_URL}?{query}"

    # =========================================================================
    # 2. Facturae 3.2.2 XML
    # =========================================================================

    def build_facturae_xml(self, invoice: Invoice) -> bytes:
        """
        Build a Facturae 3.2.2 compliant XML document.

        Returns UTF-8 encoded, pretty-printed XML bytes ready to be served
        as a file download (Content-Type: application/xml).

        Spec reference: https://www.facturae.gob.es/formato/Versiones/Esquema_Facturae_v3_2_2.xml
        """
        if not invoice.invoice_number:
            raise ValueError("Invoice must be issued before generating Facturae XML")

        ns = "http://www.facturae.gob.es/formato/Versiones/Esquema_Facturae_v3_2_2.xsd"
        root = Element("fe:Facturae", attrib={"xmlns:fe": ns})

        self._add_file_header(root, invoice)
        self._add_parties(root, invoice)
        self._add_invoices_block(root, invoice)

        raw = tostring(root, encoding="unicode")
        pretty = minidom.parseString(raw).toprettyxml(indent="  ", encoding="UTF-8")
        return pretty

    # -------------------------------------------------------------------------
    # Private XML builders
    # -------------------------------------------------------------------------

    def _add_file_header(self, root: Element, invoice: Invoice) -> None:
        header = SubElement(root, "fe:FileHeader")

        SubElement(header, "fe:SchemaVersion").text = "3.2.2"
        SubElement(header, "fe:Modality").text = "I"          # I = Individual invoice
        SubElement(header, "fe:InvoiceIssuerType").text = "EM"  # EM = Emisor (seller)

        batch = SubElement(header, "fe:Batch")
        SubElement(batch, "fe:BatchIdentifier").text = (
            f"{self.seller.tax_id}{invoice.invoice_number}"
        )
        SubElement(batch, "fe:InvoicesCount").text = "1"
        SubElement(batch, "fe:TotalInvoicesAmount").text = self._amount_block_text(
            float(invoice.totals.total_with_tax or invoice.totals.total_amount)
        )
        SubElement(batch, "fe:TotalOutstandingAmount").text = self._amount_block_text(
            float(invoice.totals.total_with_tax or invoice.totals.total_amount)
        )
        SubElement(batch, "fe:TotalExecutableAmount").text = self._amount_block_text(
            float(invoice.totals.total_with_tax or invoice.totals.total_amount)
        )
        SubElement(batch, "fe:InvoiceCurrencyCode").text = "EUR"

    def _add_parties(self, root: Element, invoice: Invoice) -> None:
        parties = SubElement(root, "fe:Parties")

        # --- Seller (Emisor) ---
        seller_el = SubElement(parties, "fe:SellerParty")
        self._add_tax_identification(seller_el, self.seller.tax_id)
        legal = SubElement(seller_el, "fe:LegalEntity")
        SubElement(legal, "fe:CorporateName").text = self.seller.name
        if self.seller.address:
            addr = SubElement(seller_el, "fe:AddressInSpain")
            SubElement(addr, "fe:Address").text = self.seller.address
            SubElement(addr, "fe:PostCode").text = self.seller.postal_code
            SubElement(addr, "fe:Town").text = self.seller.city
            SubElement(addr, "fe:Province").text = self.seller.province
            SubElement(addr, "fe:CountryCode").text = self.seller.country_code

        # --- Buyer (Receptor) ---
        buyer_el = SubElement(parties, "fe:BuyerParty")
        buyer_tax_id = invoice.customer.tax_id or "NIF-DESCONOCIDO"
        self._add_tax_identification(buyer_el, buyer_tax_id)
        legal_b = SubElement(buyer_el, "fe:LegalEntity")
        SubElement(legal_b, "fe:CorporateName").text = invoice.customer.name
        if invoice.customer.address:
            addr_b = SubElement(buyer_el, "fe:AddressInSpain")
            SubElement(addr_b, "fe:Address").text = invoice.customer.address

    def _add_invoices_block(self, root: Element, invoice: Invoice) -> None:
        invoices_el = SubElement(root, "fe:Invoices")
        inv_el = SubElement(invoices_el, "fe:Invoice")

        # --- Invoice Header ---
        inv_header = SubElement(inv_el, "fe:InvoiceHeader")
        SubElement(inv_header, "fe:InvoiceNumber").text = invoice.invoice_number
        SubElement(inv_header, "fe:InvoiceSeriesCode").text = invoice.series
        SubElement(inv_header, "fe:InvoiceDocumentType").text = "FC"  # FC = Factura Completa
        SubElement(inv_header, "fe:InvoiceClass").text = "OO"         # OO = Original

        # --- Invoice Issue Data ---
        issue_data = SubElement(inv_el, "fe:InvoiceIssueData")
        issued_dt = invoice.issued_at or datetime.utcnow()
        SubElement(issue_data, "fe:IssueDate").text = issued_dt.strftime("%Y-%m-%d")
        SubElement(issue_data, "fe:InvoiceCurrencyCode").text = "EUR"
        SubElement(issue_data, "fe:TaxCurrencyCode").text = "EUR"
        SubElement(issue_data, "fe:LanguageName").text = "es"

        # --- Tax Outputs (IVA repercutido) ---
        taxes_out = SubElement(inv_el, "fe:TaxesOutputs")
        self._add_tax_block(
            taxes_out,
            tax_type="01",   # 01 = IVA
            rate=float(invoice.totals.vat_rate),
            base=float(invoice.totals.base),
            amount=float(invoice.totals.vat_amount),
        )

        # --- Tax Withheld (IRPF) if applicable ---
        if float(invoice.totals.irpf_amount) > 0:
            taxes_wh = SubElement(inv_el, "fe:TaxesWithheld")
            self._add_tax_block(
                taxes_wh,
                tax_type="04",   # 04 = IRPF
                rate=float(invoice.totals.irpf_rate),
                base=float(invoice.totals.base),
                amount=float(invoice.totals.irpf_amount),
            )

        # --- Invoice Totals ---
        totals_el = SubElement(inv_el, "fe:InvoiceTotals")
        SubElement(totals_el, "fe:TotalGrossAmount").text = f"{float(invoice.totals.base):.2f}"
        SubElement(totals_el, "fe:TotalGeneralDiscounts").text = "0.00"
        SubElement(totals_el, "fe:TotalGeneralSurcharges").text = "0.00"
        SubElement(totals_el, "fe:TotalGrossAmountBeforeTaxes").text = f"{float(invoice.totals.base):.2f}"
        SubElement(totals_el, "fe:TotalTaxOutputs").text = f"{float(invoice.totals.vat_amount):.2f}"
        SubElement(totals_el, "fe:TotalTaxesWithheld").text = f"{float(invoice.totals.irpf_amount):.2f}"
        SubElement(totals_el, "fe:InvoiceTotal").text = f"{float(invoice.totals.total_with_tax):.2f}"
        SubElement(totals_el, "fe:TotalOutstandingAmount").text = f"{float(invoice.totals.total_with_tax):.2f}"
        SubElement(totals_el, "fe:TotalExecutableAmount").text = f"{float(invoice.totals.total_with_tax):.2f}"

        # --- Line Items ---
        items_el = SubElement(inv_el, "fe:Items")
        for idx, line in enumerate(invoice.lines, start=1):
            self._add_line_item(items_el, idx, line)

        # --- Payment Details (optional but recommended) ---
        payment_details = SubElement(inv_el, "fe:PaymentDetails")
        installment = SubElement(payment_details, "fe:Installment")
        SubElement(installment, "fe:InstallmentDueDate").text = issued_dt.strftime("%Y-%m-%d")
        SubElement(installment, "fe:InstallmentAmount").text = f"{float(invoice.totals.total_with_tax):.2f}"
        SubElement(installment, "fe:PaymentMeans").text = "04"  # 04 = Transferencia

        # --- VeriFactu additional data (fingerprint) ---
        if invoice.fingerprint:
            additional = SubElement(inv_el, "fe:AdditionalData")
            related = SubElement(additional, "fe:RelatedDocuments")
            doc_el = SubElement(related, "fe:Attachment")
            SubElement(doc_el, "fe:AttachmentDescription").text = "VeriFactu-Fingerprint"
            SubElement(doc_el, "fe:AttachmentData").text = invoice.fingerprint

    def _add_tax_identification(self, parent: Element, tax_id: str) -> None:
        tax_id_el = SubElement(parent, "fe:TaxIdentification")
        # Determine PersonTypeCode: F = Física (individual), J = Jurídica (company)
        person_type = "J" if len(tax_id) == 9 and tax_id[0].isalpha() else "F"
        SubElement(tax_id_el, "fe:PersonTypeCode").text = person_type
        SubElement(tax_id_el, "fe:ResidenceTypeCode").text = "R"  # R = Residente
        SubElement(tax_id_el, "fe:TaxIdentificationNumber").text = tax_id

    def _add_tax_block(
        self,
        parent: Element,
        tax_type: str,
        rate: float,
        base: float,
        amount: float,
    ) -> None:
        tax_el = SubElement(parent, "fe:Tax")
        SubElement(tax_el, "fe:TaxTypeCode").text = tax_type
        SubElement(tax_el, "fe:TaxRate").text = f"{rate:.2f}"
        taxable = SubElement(tax_el, "fe:TaxableBase")
        SubElement(taxable, "fe:TotalAmount").text = f"{base:.2f}"
        tax_amount_el = SubElement(tax_el, "fe:TaxAmount")
        SubElement(tax_amount_el, "fe:TotalAmount").text = f"{amount:.2f}"

    def _add_line_item(self, parent: Element, idx: int, line) -> None:
        item_el = SubElement(parent, "fe:InvoiceLine")
        SubElement(item_el, "fe:ItemDescription").text = line.description
        SubElement(item_el, "fe:Quantity").text = f"{float(line.quantity):.4f}"
        SubElement(item_el, "fe:UnitOfMeasure").text = "01"   # 01 = Units
        SubElement(item_el, "fe:UnitPriceWithoutTax").text = f"{float(line.unit_price):.6f}"
        SubElement(item_el, "fe:TotalCost").text = f"{float(line.subtotal):.2f}"
        SubElement(item_el, "fe:GrossAmount").text = f"{float(line.subtotal):.2f}"

        # Tax rate on the line
        taxes_el = SubElement(item_el, "fe:TaxesOutputs")
        tax_el = SubElement(taxes_el, "fe:Tax")
        SubElement(tax_el, "fe:TaxTypeCode").text = "01"
        SubElement(tax_el, "fe:TaxRate").text = f"{float(line.vat_rate):.2f}"
        taxable = SubElement(tax_el, "fe:TaxableBase")
        SubElement(taxable, "fe:TotalAmount").text = f"{float(line.subtotal):.2f}"
        tax_amount_el = SubElement(tax_el, "fe:TaxAmount")
        SubElement(tax_amount_el, "fe:TotalAmount").text = f"{float(line.vat_amount):.2f}"

    @staticmethod
    def _amount_block_text(amount: float) -> str:
        return f"{amount:.2f}"


# ===========================================================================
# VeriFactu XML Builder
# ===========================================================================
# VeriFactu uses a completely different XML schema from Facturae 3.2.2.
# Schema: SuministroInformacion.xsd (agenciatributaria.gob.es)
# This is what the AEAT VerifactuSOAP endpoint actually expects.
# ===========================================================================

NS_SF  = "https://www2.agenciatributaria.gob.es/static_files/common/internet/dep/aplicaciones/es/aeat/tike/cont/ws/SuministroInformacion.xsd"
NS_SLR = "https://www2.agenciatributaria.gob.es/static_files/common/internet/dep/aplicaciones/es/aeat/tike/cont/ws/SuministroLR.xsd"


class VeriFactuService:
    """
    Builds the VeriFactu RegistroFacturacion XML payload required by the
    AEAT VerifactuSOAP endpoint (SistemaFacturacion/VerifactuSOAP).
    """

    def __init__(self, seller: SellerInfo):
        self.seller = seller
        self._db = None  # lazy-loaded

    def _get_db(self):
        if self._db is None:
            import os, certifi
            from pymongo import MongoClient
            from dotenv import load_dotenv
            load_dotenv()
            client = MongoClient(os.getenv("MONGO_URI"), tlsCAFile=certifi.where())
            self._db = client[os.getenv("DB_NAME")]
        return self._db

    def _get_prev_invoice(self, invoice: Invoice) -> Optional[dict]:
        """Fetch the previous invoice document to get its number and date."""
        try:
            from bson import ObjectId
            db = self._get_db()
            doc = db["invoices"].find_one(
                {
                    "organization_id": invoice.organization_id,
                    "fingerprint": invoice.previous_fingerprint,
                },
                {"invoice_number": 1, "issued_at": 1},
            )
            return doc
        except Exception:
            return None

    def build_registro_xml(self, invoice: Invoice) -> bytes:
        """
        Build the RegistroFacturacion XML block per the VeriFactu spec
        (Orden HAC/1177/2024, Anexo I).

        Returns UTF-8 bytes of the signed-ready XML.
        """
        if not invoice.invoice_number:
            raise ValueError("Invoice must be issued before building VeriFactu XML")

        issued_dt = invoice.issued_at or datetime.utcnow()

        root = Element(
            "sum1:RegistroFacturacion",
            attrib={
                "xmlns:sum1": NS_SF,
            },
        )

        # ── IDFactura ────────────────────────────────────────────────────────
        id_factura = SubElement(root, "sum1:IDFactura")
        id_emisor = SubElement(id_factura, "sum1:IDEmisorFactura")
        SubElement(id_emisor, "sum1:NIF").text = self.seller.tax_id
        SubElement(id_factura, "sum1:NumSerieFactura").text = invoice.invoice_number
        SubElement(id_factura, "sum1:FechaExpedicionFactura").text = issued_dt.strftime("%d-%m-%Y")

        # ── NombreRazonEmisor ────────────────────────────────────────────────
        SubElement(root, "sum1:NombreRazonEmisor").text = self.seller.name

        # ── TipoFactura ──────────────────────────────────────────────────────
        # F1 = Factura completa (standard invoice)
        SubElement(root, "sum1:TipoFactura").text = "F1"

        # ── TipoRectificativa — omit for original invoices ───────────────────

        # ── FechaOperacion (same as issue date for standard invoices) ────────
        SubElement(root, "sum1:FechaOperacion").text = issued_dt.strftime("%d-%m-%Y")

        # ── DescripcionOperacion ─────────────────────────────────────────────
        desc = (invoice.lines[0].description if invoice.lines else "Prestación de servicios")
        SubElement(root, "sum1:DescripcionOperacion").text = desc[:500]

        # ── Destinatarios (buyer) ────────────────────────────────────────────
        destinatarios = SubElement(root, "sum1:Destinatarios")
        destinatario = SubElement(destinatarios, "sum1:IDDestinatario")
        buyer_nif = invoice.customer.tax_id or ""
        if buyer_nif:
            SubElement(destinatario, "sum1:NIF").text = buyer_nif
        else:
            # No NIF — use NombreRazon only (allowed for end consumers)
            SubElement(destinatario, "sum1:NombreRazon").text = invoice.customer.name

        # ── Desglose (tax breakdown) ─────────────────────────────────────────
        desglose = SubElement(root, "sum1:Desglose")
        detalle = SubElement(desglose, "sum1:DetalleDesglose")

        # Impuesto: 01 = IVA
        SubElement(detalle, "sum1:Impuesto").text = "01"
        # ClaveRegimen: 01 = Régimen general
        SubElement(detalle, "sum1:ClaveRegimen").text = "01"
        # CalificacionOperacion: S1 = Sujeta y no exenta, sin inversión
        SubElement(detalle, "sum1:CalificacionOperacion").text = "S1"
        SubElement(detalle, "sum1:TipoImpositivo").text = f"{float(invoice.totals.vat_rate):.2f}"
        SubElement(detalle, "sum1:BaseImponibleOImporteNoSujeto").text = f"{float(invoice.totals.base):.2f}"
        SubElement(detalle, "sum1:BaseImponibleACoste").text = f"{float(invoice.totals.base):.2f}"
        SubElement(detalle, "sum1:CuotaRepercutida").text = f"{float(invoice.totals.vat_amount):.2f}"

        # ── CuotaTotal & ImporteTotal ────────────────────────────────────────
        SubElement(root, "sum1:CuotaTotal").text = f"{float(invoice.totals.vat_amount):.2f}"
        SubElement(root, "sum1:ImporteTotal").text = f"{float(invoice.totals.total_with_tax):.2f}"

        # ── Encadenamiento (hash chain) ──────────────────────────────────────
        # Need the previous invoice's number and date for RegistroAnterior.
        # We look it up from the invoices collection using the previous fingerprint.
        encadenamiento = SubElement(root, "sum1:Encadenamiento")
        prev_fp = invoice.previous_fingerprint
        if prev_fp and prev_fp != "0":
            # Look up the previous invoice to get its number and date
            prev_invoice = self._get_prev_invoice(invoice)
            primer = SubElement(encadenamiento, "sum1:RegistroAnterior")
            SubElement(primer, "sum1:IDEmisorFactura").text = self.seller.tax_id
            SubElement(primer, "sum1:NumSerieFactura").text = (
                prev_invoice["invoice_number"] if prev_invoice else prev_fp[:20]
            )
            SubElement(primer, "sum1:FechaExpedicionFactura").text = (
                prev_invoice["issued_at"].strftime("%d-%m-%Y")
                if prev_invoice and prev_invoice.get("issued_at")
                else issued_dt.strftime("%d-%m-%Y")
            )
            SubElement(primer, "sum1:Huella").text = prev_fp
        else:
            # First invoice in the chain
            SubElement(encadenamiento, "sum1:PrimerRegistro").text = "S"

        # ── SistemaInformatico ───────────────────────────────────────────────
        sistema = SubElement(root, "sum1:SistemaInformatico")
        SubElement(sistema, "sum1:NombreRazon").text = self.seller.name
        SubElement(sistema, "sum1:NIF").text = self.seller.tax_id
        SubElement(sistema, "sum1:NombreSistemaInformatico").text = "Contia365"
        SubElement(sistema, "sum1:IdSistemaInformatico").text = "CONTIA365-V1"
        SubElement(sistema, "sum1:Version").text = "1.0"
        SubElement(sistema, "sum1:NumeroInstalacion").text = "1"
        SubElement(sistema, "sum1:TipoUsoPosibleSoloVerifactu").text = "S"
        SubElement(sistema, "sum1:TipoUsoPosibleMultiOT").text = "N"
        SubElement(sistema, "sum1:IndicadorMultiplesOT").text = "N"

        # ── FechaHoraHusoGenRegistro ─────────────────────────────────────────
        SubElement(root, "sum1:FechaHoraHusoGenRegistro").text = (
            issued_dt.strftime("%Y-%m-%dT%H:%M:%S") + "+01:00"
        )

        # ── TipoHuella & Huella (SHA-256 of the invoice fingerprint) ─────────
        SubElement(root, "sum1:TipoHuella").text = "01"   # 01 = SHA-256
        SubElement(root, "sum1:Huella").text = invoice.fingerprint or ""

        raw = tostring(root, encoding="unicode")
        pretty = minidom.parseString(raw).toprettyxml(indent="  ", encoding="UTF-8")
        return pretty
