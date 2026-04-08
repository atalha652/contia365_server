"""
OCR Route — OCR.space API for text extraction.
Images + scanned PDFs → OCR.space API
Digital PDFs → pdfplumber (instant) with OCR.space fallback for scanned pages
Data extraction: regex patterns (no AI/LLM)
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks, Form, Depends
import io
import re
import os
import boto3
import certifi
import requests
from bson import ObjectId
from datetime import datetime
from dotenv import load_dotenv
from pymongo import MongoClient
from urllib.parse import unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
from app.services.tax_classification_service import TaxClassificationService
from app.services.ai_modelo_analyzer import AIModeloAnalyzer
from app.services.invoice_extractor import extract_invoice_data_enhanced
from app.utils.period_guard import validate_upload_window

load_dotenv()

# ── Config ───────────────────────────────────────────────────────────────────
MONGO_URI     = os.getenv("MONGO_URI")
DB_NAME       = os.getenv("DB_NAME")
OCR_API_KEY   = os.getenv("OCR_SPACE_API_KEY")
OCR_SPACE_URL = "https://api.ocr.space/parse/image"
bucket_name   = "ai-auto-invoice"

s3 = boto3.client(
    "s3",
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    region_name="eu-north-1",
)

mongo_client        = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db                  = mongo_client[DB_NAME]
voucher_collection  = db["voucher"]
ledger_collection   = db["ledger"]
ocr_jobs_collection = db["ocr_jobs"]

_classifier = TaxClassificationService()
_ai_analyzer = AIModeloAnalyzer()

router = APIRouter(prefix="/accounting/ocr", tags=["OCR"])


def ocr_with_ocrspace(file_bytes: bytes, file_ext: str = "jpg") -> str:
    """Send file to OCR.space and return extracted text."""
    if not OCR_API_KEY:
        raise ValueError("OCR_SPACE_API_KEY is not configured in environment variables")
    
    mime_map = {
        "pdf": "application/pdf", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "gif": "image/gif", "bmp": "image/bmp", "tiff": "image/tiff",
    }
    response = requests.post(
        OCR_SPACE_URL,
        files={"file": (f"document.{file_ext}", file_bytes, mime_map.get(file_ext.lower(), "image/jpeg"))},
        data={"apikey": OCR_API_KEY, "language": "eng", "isOverlayRequired": False, "scale": True, "OCREngine": 2},
        timeout=60,
    )
    
    # Better error handling for API issues
    if response.status_code == 403:
        raise ValueError("OCR.space API key is invalid or has exceeded quota. Check OCR_SPACE_API_KEY in .env")
    
    response.raise_for_status()
    result = response.json()
    if result.get("IsErroredOnProcessing"):
        raise ValueError(f"OCR.space error: {result.get('ErrorMessage', 'Unknown')}")
    text = "\n".join(p.get("ParsedText", "") for p in result.get("ParsedResults", [])).strip()
    print(f"[OCR] OCR.space extracted {len(text)} chars")
    return text


def ocr_image(image_bytes: bytes, file_ext: str = "jpg") -> str:
    """Extract text from an image using OCR.space."""
    return ocr_with_ocrspace(image_bytes, file_ext=file_ext)


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """
    1. pdfplumber — instant for digital PDFs
    2. OCR.space  — fallback for scanned/image-based PDFs
    """
    import pdfplumber
    text_parts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text_parts.append(page.extract_text() or "")
    text = "\n".join(text_parts).strip()
    if text:
        print(f"[OCR] pdfplumber extracted {len(text)} chars (digital PDF)")
        return text

    # Scanned PDF — send whole PDF to OCR.space directly
    print("[OCR] pdfplumber empty — sending to OCR.space")
    return ocr_with_ocrspace(pdf_bytes, file_ext="pdf")


# ── Helpers ───────────────────────────────────────────────────────────────────

def convert_toon_to_readable(toon_string: str) -> str:
    try:
        lines = []
        for pair in toon_string.split("|"):
            if ":" in pair:
                key, value = pair.split(":", 1)
                lines.append(f"{key.strip()}: {value.replace('\\|', '|').strip()}")
        return "\n".join(lines)
    except Exception:
        return toon_string


def clean_ocr_text(raw: str) -> str:
    text = re.sub(r"\n+", "\n", raw)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"(?<=\w)\n(?=\w)", " ", text)
    fixes = {
        r"jank Name": "Bank Name",
        r"\\ccount": "Account",
        r"ase make the payment": "Please make the payment",
        r"\bO H W\b": "OHW",
    }
    for wrong, right in fixes.items():
        text = re.sub(wrong, right, text, flags=re.IGNORECASE)
    return text.strip()


# ── Regex invoice extraction ──────────────────────────────────────────────────

def generate_invoice_number(voucher_id: str, file_index: int = 0) -> str:
    """Generate a system invoice number when extraction fails"""
    from datetime import datetime
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


def extract_invoice_data(text: str, voucher_id: str = None, file_index: int = 0) -> dict:
    t = text

    # Classify as income (you issued the invoice / you receive money)
    # or expense (you received the invoice / you pay money).
    # Income signals: you are the supplier/seller
    # Expense signals: you are the client/buyer, or it's a purchase/rent/cost
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
        transaction_type = "expense"  # safe default — unknown = treat as cost

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

    dates = re.findall(
        r"\b(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{4}[\/\-\.]\d{1,2}[\/\-\.]\d{1,2})\b", t
    )
    invoice_date = dates[0] if dates else "N/A"
    due_date     = dates[1] if len(dates) > 1 else "N/A"

    def parse_amount(s: str) -> float:
        try:
            return float(s.replace(",", "").replace(" ", ""))
        except Exception:
            return 0.0

    total_m    = re.search(r"(?:amount\s*to\s*pay|total\s*a\s*pagar|grand\s*total|amount\s*due|total)[^\d]*(\d[\d,\.]+)", t, re.IGNORECASE)
    total      = parse_amount(total_m.group(1)) if total_m else 0.0

    base_m = re.search(r"(?:base\s*(?:amount|rent|imponible)|subtotal|net\s*amount|before\s*tax)[^\d]*(\d[\d,\.]+)", t, re.IGNORECASE)
    base   = parse_amount(base_m.group(1)) if base_m else 0.0

    vat_rate_m = re.search(r"(?:vat|iva|igic)\s*\(?(\d{1,2}(?:\.\d+)?)\s*%\)?", t, re.IGNORECASE)
    vat_rate   = float(vat_rate_m.group(1)) if vat_rate_m else 0.0

    vat_amount = 0.0
    if vat_rate_m:
        after_rate = t[vat_rate_m.end():]
        vat_amt_m  = re.search(r"[^\d]*(\d[\d,\.]+)", after_rate)
        if vat_amt_m:
            vat_amount = parse_amount(vat_amt_m.group(1))

    irpf_rate_m = re.search(r"irpf[^\d]*(\d{1,2}(?:\.\d+)?)\s*%", t, re.IGNORECASE)
    irpf_rate   = float(irpf_rate_m.group(1)) if irpf_rate_m else 0.0

    irpf_amount = 0.0
    if irpf_rate_m:
        after_irpf = t[irpf_rate_m.end():]
        irpf_amt_m = re.search(r"-?\s*[€$]?\s*(\d[\d,\.]+)", after_irpf)
        if irpf_amt_m:
            irpf_amount = parse_amount(irpf_amt_m.group(1))

    total_with_tax = total

    # Extract email first (used as fallback for supplier name)
    email_m  = re.search(r"[\w\.\-]+@[\w\.\-]+\.\w{2,}", t)
    email    = email_m.group(0) if email_m else "N/A"

    # Enhanced supplier and customer extraction
    supplier_name = extract_supplier_name(t, email)
    customer_name = extract_customer_name(t, transaction_type)

    addr          = re.findall(r"\b\d+[\w\s,\.]{5,60}(?:street|st|avenue|ave|road|rd|lane|ln|blvd|calle|plaza|paseo)[^\n]*", t, re.IGNORECASE)
    address_line1 = addr[0].strip() if addr else "N/A"
    address_line2 = addr[1].strip() if len(addr) > 1 else "N/A"

    item_rows = re.findall(r"([A-Za-z][^\n]{3,50}?)\s+(\d+)\s+(\d[\d,\.]+)\s+(\d[\d,\.]+)", t)
    items = (
        [{"description": m[0].strip(), "qty": int(m[1]), "unit_price": parse_amount(m[2]), "subtotal": parse_amount(m[3])} for m in item_rows]
        if item_rows
        else [{"description": "See document", "qty": 1, "unit_price": total, "subtotal": total}]
    )

    return {
        "transaction_type": transaction_type,
        "supplier": {"business_name": supplier_name, "address_line1": address_line1, "address_line2": address_line2, "Email": email},
        "customer": {"company_name": customer_name, "address_line1": "N/A", "address_line2": "N/A", "Email": "N/A"},
        "invoice":  {"invoice_number": invoice_number, "invoice_date": invoice_date, "due_date": due_date, "amount_in_words": "N/A"},
        "items":    items,
        "totals":   {"base": round(base, 2), "total": round(total, 2), "VAT_rate": vat_rate, "VAT_amount": round(vat_amount, 2), "IRPF_rate": irpf_rate, "IRPF_amount": round(irpf_amount, 2), "Total_with_Tax": round(total_with_tax, 2)},
    }



# ── Per-voucher worker ────────────────────────────────────────────────────────

def process_single_voucher(voucher: dict, user_id: str, period: str) -> dict:
    voucher_id = str(voucher["_id"])
    voucher_collection.update_one(
        {"_id": ObjectId(voucher_id)},
        {"$set": {"OCR": "processing", "ocr_started_at": datetime.utcnow()}},
    )

    results = []
    files = voucher.get("files", [])
    
    # Log if no files found
    if not files:
        print(f"[OCR] ⚠️ Voucher {voucher_id} has no files")

    for file_obj in files:
        if not isinstance(file_obj, dict):
            print(f"[OCR] ⚠️ Skipping non-dict file object in voucher {voucher_id}: {type(file_obj)}")
            continue

        # TOON / email
        if "toon_data" in file_obj:
            file_name = file_obj.get("name", "unknown.toon")
            try:
                toon_data      = file_obj["toon_data"]
                original_email = file_obj.get("original_email", {})
                readable       = convert_toon_to_readable(toon_data)
                if original_email:
                    text = (
                        f"Sender: {original_email.get('sender_name','')} ({original_email.get('sender_email','')})\n"
                        f"Subject: {original_email.get('subject','')}\n"
                        f"Date: {original_email.get('date','')}\n"
                        f"Merchant: {original_email.get('merchant','')}\n"
                        f"Amount: {original_email.get('amount','')} {original_email.get('currency','')}\n"
                        f"Order: {original_email.get('order_number','')}\n"
                        f"{original_email.get('snippet','')}\n{readable}"
                    )
                else:
                    text = readable

                invoice_data  = extract_invoice_data_enhanced(text, voucher_id=voucher_id, file_index=0)
                
                # ── AI Modelo Analysis ────────────────────────────────────
                ai_modelo_result = None
                try:
                    ai_modelo_result = _ai_analyzer.analyze_invoice_for_modelo(
                        invoice_data=invoice_data,
                        ocr_text=text
                    )
                    print(f"[AI Modelo] ✅ Analyzed: {ai_modelo_result.get('modelo_id')} (confidence: {ai_modelo_result.get('confidence')})")
                except Exception as ae:
                    print(f"[AI Modelo] ⚠️ Analysis failed: {ae}")
                
                # ── Determine account codes and entry type for tax calculations ──
                tx_type = invoice_data.get("transaction_type", "expense")
                account_code = "4770" if tx_type == "income" else "4720"  # VAT accounts
                entry_type = "credit" if tx_type == "income" else "debit"
                amount = invoice_data.get("totals", {}).get("Total_with_Tax", 0)
                
                ledger_result = ledger_collection.insert_one({
                    "user_id": user_id,
                    "organization_id": user_id,
                    "voucher_id": voucher_id,
                    "file_name": file_name,
                    "data_type": "toon",
                    "toon_data": toon_data,
                    "ocr_text": text,
                    "invoice_data": invoice_data,
                    "processing_status": "success",
                    "period": period,
                    # ── AI Modelo Analysis Results ────────────────────────
                    "ai_modelo_id": ai_modelo_result.get("modelo_id") if ai_modelo_result else None,
                    "ai_modelo_confidence": ai_modelo_result.get("confidence") if ai_modelo_result else None,
                    "ai_modelo_reasoning": ai_modelo_result.get("reasoning") if ai_modelo_result else None,
                    # ── Accounting fields for tax calculations ──
                    "account_code": account_code,
                    "entry_type": entry_type,
                    "amount": amount,
                    "transaction_date": datetime.utcnow(),
                    "description": f"TOON: {file_name}",
                    "created_at": datetime.utcnow(),
                })
                ledger_id = str(ledger_result.inserted_id)
                # ── Tax Classification Layer ──────────────────────────────
                try:
                    _classifier.classify_ledger_entry(ledger_id, user_id)
                except Exception as ce:
                    print(f"[ClassificationLayer] ⚠️ toon entry {ledger_id}: {ce}")
                
                # ── Create Tax Transaction ───────────────────────────────
                try:
                    from app.services.tax_calculation_service import TaxCalculationService
                    tax_service = TaxCalculationService(db)
                    tax_tx_id = tax_service.create_tax_transaction_from_ledger(
                        ledger_entry_id=ledger_id,
                        organization_id=user_id
                    )
                    if tax_tx_id:
                        print(f"[TaxCalc] ✅ Created tax transaction {tax_tx_id} for ledger {ledger_id}")
                    else:
                        print(f"[TaxCalc] ℹ️ No tax transaction needed for ledger {ledger_id}")
                except Exception as te:
                    print(f"[TaxCalc] ⚠️ Failed to create tax transaction for {ledger_id}: {te}")
                
                results.append({"file_name": file_name, "data_type": "toon", "ledger_id": ledger_id, "status": "success"})
            except Exception as e:
                import traceback
                print(f"[OCR] ❌ Failed toon file {file_name} in voucher {voucher_id}:\n{traceback.format_exc()}")
                results.append({"file_name": file_name, "data_type": "toon", "status": "failed", "error": str(e)})

        # Regular file
        elif "file_url" in file_obj:
            file_url = file_obj["file_url"]
            s3_key   = file_obj.get("s3_key") or "unknown"
            try:
                if s3_key == "unknown":
                    clean_url = file_url.split("?")[0]
                    if ".amazonaws.com/" in clean_url:
                        s3_key = unquote(clean_url.split(".amazonaws.com/")[1])
                    else:
                        raise ValueError(f"Cannot parse S3 key from: {file_url}")

                buf = io.BytesIO()
                s3.download_fileobj(bucket_name, s3_key, buf)
                file_bytes = buf.getvalue()
                print(f"[OCR] Downloaded {s3_key} — {len(file_bytes)} bytes")

                ext = s3_key.lower().rsplit(".", 1)[-1]
                if ext == "pdf":
                    raw_text = extract_text_from_pdf(file_bytes)
                elif ext in ("txt", "text"):
                    raw_text = file_bytes.decode("utf-8", errors="ignore")
                elif ext in ("jpg", "jpeg", "png", "gif", "bmp", "tiff"):
                    raw_text = ocr_image(file_bytes, file_ext=ext)
                else:
                    raise ValueError(f"Unsupported file type: {ext}")

                print(f"[OCR] Extracted {len(raw_text)} chars")
                cleaned_text  = clean_ocr_text(raw_text)
                invoice_data  = extract_invoice_data_enhanced(cleaned_text, voucher_id=voucher_id, file_index=0)
                
                # ── AI Modelo Analysis ────────────────────────────────────
                ai_modelo_result = None
                try:
                    ai_modelo_result = _ai_analyzer.analyze_invoice_for_modelo(
                        invoice_data=invoice_data,
                        ocr_text=cleaned_text
                    )
                    print(f"[AI Modelo] ✅ Analyzed: {ai_modelo_result.get('modelo_id')} (confidence: {ai_modelo_result.get('confidence')})")
                except Exception as ae:
                    print(f"[AI Modelo] ⚠️ Analysis failed: {ae}")
                
                # ── Determine account codes and entry type for tax calculations ──
                tx_type = invoice_data.get("transaction_type", "expense")
                account_code = "4770" if tx_type == "income" else "4720"  # VAT accounts
                entry_type = "credit" if tx_type == "income" else "debit"
                amount = invoice_data.get("totals", {}).get("Total_with_Tax", 0)
                
                ledger_result = ledger_collection.insert_one({
                    "user_id": user_id,
                    "organization_id": user_id,
                    "voucher_id": voucher_id,
                    "file_url": file_url,
                    "s3_key": s3_key,
                    "data_type": "file",
                    "ocr_text": cleaned_text,
                    "invoice_data": invoice_data,
                    "processing_status": "success",
                    "period": period,
                    # ── AI Modelo Analysis Results ────────────────────────
                    "ai_modelo_id": ai_modelo_result.get("modelo_id") if ai_modelo_result else None,
                    "ai_modelo_confidence": ai_modelo_result.get("confidence") if ai_modelo_result else None,
                    "ai_modelo_reasoning": ai_modelo_result.get("reasoning") if ai_modelo_result else None,
                    # ── Accounting fields for tax calculations ──
                    "account_code": account_code,
                    "entry_type": entry_type,
                    "amount": amount,
                    "transaction_date": datetime.utcnow(),
                    "description": f"Invoice from {invoice_data.get('supplier', {}).get('business_name', 'Unknown')}",
                    "created_at": datetime.utcnow(),
                })
                ledger_id = str(ledger_result.inserted_id)
                # ── Tax Classification Layer ──────────────────────────────
                try:
                    _classifier.classify_ledger_entry(ledger_id, user_id)
                except Exception as ce:
                    print(f"[ClassificationLayer] ⚠️ file entry {ledger_id}: {ce}")
                
                # ── Create Tax Transaction ───────────────────────────────
                try:
                    from app.services.tax_calculation_service import TaxCalculationService
                    tax_service = TaxCalculationService(db)
                    tax_tx_id = tax_service.create_tax_transaction_from_ledger(
                        ledger_entry_id=ledger_id,
                        organization_id=user_id
                    )
                    if tax_tx_id:
                        print(f"[TaxCalc] ✅ Created tax transaction {tax_tx_id} for ledger {ledger_id}")
                    else:
                        print(f"[TaxCalc] ℹ️ No tax transaction needed for ledger {ledger_id}")
                except Exception as te:
                    print(f"[TaxCalc] ⚠️ Failed to create tax transaction for {ledger_id}: {te}")
                
                results.append({"file_url": file_url, "s3_key": s3_key, "data_type": "file", "ledger_id": ledger_id, "status": "success"})
                print(f"[OCR] ✅ Success: {s3_key}")
            except Exception as e:
                import traceback
                print(f"[OCR] ❌ Failed: {s3_key}\n{traceback.format_exc()}")
                results.append({"file_url": file_url, "s3_key": s3_key, "data_type": "file", "status": "failed", "error": str(e)})
        
        # Handle files that don't have expected structure
        else:
            print(f"[OCR] ⚠️ Skipping file with unexpected structure in voucher {voucher_id}: {file_obj}")
            results.append({
                "file_name": file_obj.get("name", "unknown"),
                "data_type": "unknown",
                "status": "failed",
                "error": "File missing required fields (file_url or toon_data)"
            })

    # Determine final OCR status
    if not results:
        # No files were processed at all
        ocr_status = "failed"
        print(f"[OCR] ⚠️ Voucher {voucher_id}: No files processed (had {len(files)} files)")
    else:
        success_count = sum(1 for r in results if r["status"] == "success")
        failed_count = sum(1 for r in results if r["status"] == "failed")
        
        if success_count == len(results):
            # All files succeeded
            ocr_status = "done"
        elif success_count > 0 and failed_count > 0:
            # Mixed results - some succeeded, some failed
            ocr_status = "partial"
            print(f"[OCR] ⚠️ Voucher {voucher_id}: Partial success ({success_count}/{len(results)} files)")
        elif failed_count == len(results):
            # All files failed
            ocr_status = "failed"
        else:
            # Shouldn't happen, but handle edge case
            ocr_status = "unknown"
    
    print(f"[OCR] Voucher {voucher_id} final status: {ocr_status} ({len(results)} files processed)")

    voucher_collection.update_one(
        {"_id": ObjectId(voucher_id)},
        {"$set": {"OCR": ocr_status, "ocr_completed_at": datetime.utcnow()}},
    )
    return {"voucher_id": voucher_id, "file_count": len(results), "ocr_status": ocr_status, "files": results}


# ── Background job ────────────────────────────────────────────────────────────

def process_vouchers_background(job_id: str, user_id: str, voucher_object_ids: list, period: str):
    try:
        print(f"[OCR JOB] Starting {job_id}")
        ocr_jobs_collection.update_one(
            {"_id": ObjectId(job_id)},
            {"$set": {"status": "processing", "started_at": datetime.utcnow()}},
        )
        vouchers = list(voucher_collection.find(
            {"_id": {"$in": voucher_object_ids}, "user_id": user_id},
            {"files": 1},
        ))
        print(f"[OCR JOB] {len(vouchers)} vouchers found")

        job_results = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(process_single_voucher, v, user_id, period): v for v in vouchers}
            for future in as_completed(futures):
                try:
                    result = future.result()
                    job_results.append(result)
                    print(f"[OCR JOB] Done: {result['voucher_id']} → {result['ocr_status']}")
                except Exception as e:
                    import traceback
                    print(f"[OCR JOB] ❌ Thread failed:\n{traceback.format_exc()}")
                    job_results.append({"voucher_id": str(futures[future]["_id"]), "ocr_status": "failed", "error": str(e)})

        ocr_jobs_collection.update_one(
            {"_id": ObjectId(job_id)},
            {"$set": {"status": "success", "completed_at": datetime.utcnow(), "results": job_results}},
        )
        print(f"[OCR JOB] ✅ Completed {job_id}")
    except Exception as e:
        import traceback
        print(f"[OCR JOB] ❌ Job failed:\n{traceback.format_exc()}")
        ocr_jobs_collection.update_one(
            {"_id": ObjectId(job_id)},
            {"$set": {"status": "failed", "error": str(e), "failed_at": datetime.utcnow()}},
        )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/voucher_ocr")
async def start_voucher_ocr(
    background_tasks: BackgroundTasks,
    user_id: str = Form(...),
    voucher_ids: str = Form(..., description="Comma-separated voucher IDs"),
    period: str = Depends(validate_upload_window),
):
    voucher_id_list = [v.strip() for v in voucher_ids.split(",") if v.strip()]
    if not voucher_id_list:
        raise HTTPException(status_code=400, detail="No voucher IDs provided")
    try:
        voucher_object_ids = [ObjectId(v) for v in voucher_id_list]
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid voucher ID format")

    count = voucher_collection.count_documents({"_id": {"$in": voucher_object_ids}, "user_id": user_id})
    if count == 0:
        raise HTTPException(status_code=404, detail="No vouchers found")

    job    = ocr_jobs_collection.insert_one({"user_id": user_id, "voucher_ids": voucher_id_list, "status": "awaiting", "total_vouchers": count, "period": period, "created_at": datetime.utcnow()})
    job_id = str(job.inserted_id)
    background_tasks.add_task(process_vouchers_background, job_id, user_id, voucher_object_ids, period)

    return {"message": "OCR processing started", "job_id": job_id, "user_id": user_id, "total_vouchers": count, "status": "awaiting", "check_status_url": f"/accounting/ocr/job/{job_id}"}


@router.get("/job/{job_id}")
async def get_ocr_job_status(job_id: str):
    job = ocr_jobs_collection.find_one({"_id": ObjectId(job_id)})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job["_id"] = str(job["_id"])
    return {"job_id": job_id, "status": job.get("status"), "user_id": job.get("user_id"), "total_vouchers": job.get("total_vouchers"), "created_at": job.get("created_at"), "started_at": job.get("started_at"), "completed_at": job.get("completed_at"), "failed_at": job.get("failed_at"), "error": job.get("error"), "results": job.get("results")}


@router.get("/voucher/{voucher_id}/debug")
async def debug_voucher_files(voucher_id: str):
    """Debug endpoint to inspect voucher file structure"""
    try:
        voucher = voucher_collection.find_one({"_id": ObjectId(voucher_id)})
        if not voucher:
            raise HTTPException(status_code=404, detail="Voucher not found")
        
        files = voucher.get("files", [])
        file_analysis = []
        
        for idx, file_obj in enumerate(files):
            analysis = {
                "index": idx,
                "type": type(file_obj).__name__,
                "has_file_url": "file_url" in file_obj if isinstance(file_obj, dict) else False,
                "has_toon_data": "toon_data" in file_obj if isinstance(file_obj, dict) else False,
                "has_s3_key": "s3_key" in file_obj if isinstance(file_obj, dict) else False,
                "keys": list(file_obj.keys()) if isinstance(file_obj, dict) else [],
                "raw": file_obj
            }
            file_analysis.append(analysis)
        
        return {
            "voucher_id": voucher_id,
            "ocr_status": voucher.get("OCR"),
            "total_files": len(files),
            "files_analysis": file_analysis
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
