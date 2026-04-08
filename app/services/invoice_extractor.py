"""
Enhanced Invoice Data Extraction Service
Provides improved extraction with fallback strategies for invoice numbers, suppliers, and customers
"""

import re
from datetime import datetime
from typing import Dict, Any


def generate_invoice_number(voucher_id: str, file_index: int = 0) -> str:
    """Generate a system invoice number when extraction fails"""
    timestamp = datetime.utcnow().strftime("%Y%m%d")
    # Use last 6 chars of voucher_id for uniqueness
    short_id = str(voucher_id)[-6:].upper()
    return f"INV-{timestamp}-{short_id}-{file_index}"


def extract_supplier_name(text: str, email: str = None) -> str:
    """Extract supplier name with multiple fallback strategies"""
    t = text
    
    # Strategy 1: Look for explicit supplier/vendor/from patterns
    sup_m = re.search(
        r"(?:from[:\s]+|supplier[:\s]+|vendor[:\s]+|issued\s*by[:\s]+|"
        r"company[:\s]+|proveedor[:\s]+|de[:\s]+)([A-Z][^\n]{2,60})",
        t, re.IGNORECASE
    )
    if sup_m:
        name = sup_m.group(1).strip()
        # Clean up common noise
        name = re.sub(r'\s+(invoice|factura|bill|receipt).*$', '', name, flags=re.IGNORECASE)
        if len(name) > 3:
            return name
    
    # Strategy 2: Look for all-caps company names at start of document
    caps_m = re.search(r"^([A-Z][A-Z\s&\.,]{4,50})$", t, re.MULTILINE)
    if caps_m:
        name = caps_m.group(1).strip()
        if len(name) > 3:
            return name
    
    # Strategy 3: Extract from email domain if available
    if email and email != "N/A":
        domain_match = re.search(r'@([\w\-]+)', email)
        if domain_match:
            domain = domain_match.group(1)
            # Capitalize first letter of each word
            return domain.replace('-', ' ').title() + " (from email)"
    
    # Strategy 4: Look for any capitalized name near the top (first 200 chars)
    top_text = t[:200]
    name_m = re.search(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b', top_text)
    if name_m:
        return name_m.group(1).strip()
    
    return "Unknown Supplier"


def extract_customer_name(text: str, transaction_type: str) -> str:
    """Extract customer name with fallback strategies"""
    t = text
    
    # Strategy 1: Look for explicit customer/bill to patterns
    cust_m = re.search(
        r"(?:bill\s*to[:\s]+|invoice\s*to[:\s]+|sold\s*to[:\s]+|"
        r"customer[:\s]+|cliente[:\s]+|para[:\s]+)([A-Z][^\n]{2,60})",
        t, re.IGNORECASE
    )
    if cust_m:
        name = cust_m.group(1).strip()
        # Clean up common noise
        name = re.sub(r'\s+(invoice|factura|bill|receipt).*$', '', name, flags=re.IGNORECASE)
        if len(name) > 3:
            return name
    
    # Strategy 2: For income transactions, look for "to" patterns
    if transaction_type == "income":
        to_m = re.search(r'\bto[:\s]+([A-Z][^\n]{3,50})', t, re.IGNORECASE)
        if to_m:
            name = to_m.group(1).strip()
            if len(name) > 3:
                return name
    
    # Fallback based on transaction type
    if transaction_type == "income":
        return "Customer (Not Specified)"
    else:
        return "Self/Company"


def extract_invoice_data_enhanced(text: str, voucher_id: str = None, file_index: int = 0) -> Dict[str, Any]:
    """
    Enhanced invoice data extraction with improved fallback strategies
    
    Args:
        text: OCR extracted text
        voucher_id: Voucher ID for generating system invoice numbers
        file_index: File index within voucher for unique invoice numbers
        
    Returns:
        Dictionary with extracted invoice data
    """
    t = text

    # Classify transaction type
    if re.search(
        r"\b(factura\s+emitida|invoice\s+to|sold\s+to|bill\s+to|client[e]?[:\s]|"
        r"sale|revenue|income|payment\s+received|services?\s+rendered|prestaci[oó]n\s+de\s+servicios)\b",
        t, re.IGNORECASE
    ):
        transaction_type = "income"
    elif re.search(
        r"\b(factura\s+recibida|invoice\s+from|purchase|compra|gasto|expense|"
        r"proveedor|supplier[:\s]|vendor[:\s]|rent|alquiler|office|material|equipment)\b",
        t, re.IGNORECASE
    ):
        transaction_type = "expense"
    else:
        transaction_type = "expense"  # safe default

    # Extract invoice number with fallback to system-generated
    inv = re.search(
        r"(?:invoice\s*(?:no|number|#|num)[:\s#]*|inv[:\s#]+|n[úu]mero[:\s]+|"
        r"factura\s*(?:no|n[úu]m)[:\s#]*|order\s*(?:no|#)[:\s]*)([\w\-/]+)",
        t, re.IGNORECASE,
    )
    if inv:
        invoice_number = inv.group(1).strip()
        # Validate it's not just noise
        if len(invoice_number) < 2 or invoice_number.lower() in ['no', 'num', 'number']:
            invoice_number = generate_invoice_number(voucher_id or "unknown", file_index)
    else:
        invoice_number = generate_invoice_number(voucher_id or "unknown", file_index)

    # Extract dates
    dates = re.findall(
        r"\b(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{4}[\/\-\.]\d{1,2}[\/\-\.]\d{1,2})\b", t
    )
    invoice_date = dates[0] if dates else "N/A"
    due_date = dates[1] if len(dates) > 1 else "N/A"

    def parse_amount(s: str) -> float:
        try:
            return float(s.replace(",", "").replace(" ", ""))
        except Exception:
            return 0.0

    # Extract financial amounts — read only, never calculate
    # Total: prefer "Amount to Pay" (rent invoices) then "Total"
    total_m = re.search(r"(?:amount\s*to\s*pay|total\s*a\s*pagar|grand\s*total|amount\s*due|total)[^\d]*(\d[\d,\.]+)", t, re.IGNORECASE)
    total = parse_amount(total_m.group(1)) if total_m else 0.0

    # Base amount
    base_m = re.search(r"(?:base\s*(?:amount|rent|imponible)|subtotal|net\s*amount|before\s*tax)[^\d]*(\d[\d,\.]+)", t, re.IGNORECASE)
    base = parse_amount(base_m.group(1)) if base_m else 0.0

    # VAT/IVA — extract rate and amount separately
    # Rate: e.g. "VAT (21%)" or "IVA 21%"
    vat_rate_m = re.search(r"(?:vat|iva|igic)\s*\(?(\d{1,2}(?:\.\d+)?)\s*%\)?", t, re.IGNORECASE)
    vat_rate = float(vat_rate_m.group(1)) if vat_rate_m else 0.0

    # Amount: only grab the euro/number that comes AFTER the rate pattern, not the % digit itself
    # e.g. "VAT (21%): €210.00" → 210.00
    vat_amount = 0.0
    if vat_rate_m:
        after_rate = t[vat_rate_m.end():]
        vat_amt_m = re.search(r"[^\d]*(\d[\d,\.]+)", after_rate)
        if vat_amt_m:
            vat_amount = parse_amount(vat_amt_m.group(1))

    # IRPF retention — negative withholding, e.g. "IRPF Retention (19%): -€152.00"
    irpf_rate_m = re.search(r"irpf[^\d]*(\d{1,2}(?:\.\d+)?)\s*%", t, re.IGNORECASE)
    irpf_rate = float(irpf_rate_m.group(1)) if irpf_rate_m else 0.0

    irpf_amount = 0.0
    if irpf_rate_m:
        after_irpf = t[irpf_rate_m.end():]
        irpf_amt_m = re.search(r"-?\s*[€$]?\s*(\d[\d,\.]+)", after_irpf)
        if irpf_amt_m:
            irpf_amount = parse_amount(irpf_amt_m.group(1))

    # total_with_tax = whatever the invoice says is the final amount — no recalculation
    total_with_tax = total

    # Extract email first (used as fallback for supplier name)
    email_m = re.search(r"[\w\.\-]+@[\w\.\-]+\.\w{2,}", t)
    email = email_m.group(0) if email_m else "N/A"

    # Enhanced supplier and customer extraction
    supplier_name = extract_supplier_name(t, email)
    customer_name = extract_customer_name(t, transaction_type)

    # Extract addresses
    addr = re.findall(
        r"\b\d+[\w\s,\.]{5,60}(?:street|st|avenue|ave|road|rd|lane|ln|blvd|calle|plaza|paseo)[^\n]*",
        t, re.IGNORECASE
    )
    address_line1 = addr[0].strip() if addr else "N/A"
    address_line2 = addr[1].strip() if len(addr) > 1 else "N/A"

    # Extract line items
    item_rows = re.findall(r"([A-Za-z][^\n]{3,50}?)\s+(\d+)\s+(\d[\d,\.]+)\s+(\d[\d,\.]+)", t)
    items = (
        [{"description": m[0].strip(), "qty": int(m[1]), "unit_price": parse_amount(m[2]), "subtotal": parse_amount(m[3])} 
         for m in item_rows]
        if item_rows
        else [{"description": "See document", "qty": 1, "unit_price": total, "subtotal": total}]
    )

    return {
        "transaction_type": transaction_type,
        "supplier": {
            "business_name": supplier_name,
            "address_line1": address_line1,
            "address_line2": address_line2,
            "Email": email
        },
        "customer": {
            "company_name": customer_name,
            "address_line1": "N/A",
            "address_line2": "N/A",
            "Email": "N/A"
        },
        "invoice": {
            "invoice_number": invoice_number,
            "invoice_date": invoice_date,
            "due_date": due_date,
            "amount_in_words": "N/A"
        },
        "items": items,
        "totals": {
            "base": round(base, 2),
            "total": round(total, 2),
            "VAT_rate": vat_rate,
            "VAT_amount": round(vat_amount, 2),
            "IRPF_rate": irpf_rate,
            "IRPF_amount": round(irpf_amount, 2),
            "Total_with_Tax": round(total_with_tax, 2)
        },
    }
