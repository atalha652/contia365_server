"""
OCR Route — OCR.space API for text extraction.
Images + scanned PDFs → OCR.space API (free, K82495702388957)
Digital PDFs → pdfplumber (free, instant) with OCR.space fallback
Data extraction: regex patterns (no AI/LLM)
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks, Form
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

load_dotenv()

# ── Config ───────────────────────────────────────────────────────────────────
MONGO_URI       = os.getenv("MONGO_URI")
DB_NAME         = os.getenv("DB_NAME")
OCR_API_KEY     = os.getenv("OCR_SPACE_API_KEY")   # kept as fallback
OCR_SPACE_URL   = "https://api.ocr.space/parse/image"
VISION_CREDS    = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "certs/google_vision_service_account.json")
bucket_name     = "ai-auto-invoice"

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

router = APIRouter(prefix="/accounting/ocr", tags=["OCR"])


# ── Google Cloud Vision (service account) ────────────────────────────────────

def _get_vision_client():
    """Create Vision client using service account JSON."""
    from google.cloud import vision
    from google.oauth2 import service_account
    credentials = service_account.Credentials.from_service_account_file(
        VISION_CREDS,
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    return vision.ImageAnnotatorClient(credentials=credentials)


def ocr_with_vision(image_bytes: bytes) -> str:
    """Extract text from image bytes using Google Cloud Vision API."""
    from google.cloud import vision
    client = _get_vision_client()
    image  = vision.Image(content=image_bytes)
    response = client.document_text_detection(image=image)
    if response.error.message:
        raise ValueError(f"Vision API error: {response.error.message}")
    annotation = response.full_text_annotation
    text = annotation.text if annotation else ""
    print(f"[OCR] Vision extracted {len(text)} chars")
    return text


def ocr_image(image_bytes: bytes, file_ext: str = "jpg") -> str:
    """Extract text from an image — Vision API with OCR.space fallback."""
    try:
        return ocr_with_vision(image_bytes)
    except Exception as e:
        print(f"[OCR] Vision failed ({e}) — falling back to OCR.space")
        return ocr_with_ocrspace(image_bytes, file_ext=file_ext)


def ocr_with_ocrspace(file_bytes: bytes, file_ext: str = "jpg") -> str:
    """OCR.space fallback."""
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
    response.raise_for_status()
    result = response.json()
    if result.get("IsErroredOnProcessing"):
        raise ValueError(f"OCR.space error: {result.get('ErrorMessage', 'Unknown')}")
    text = "\n".join(p.get("ParsedText", "") for p in result.get("ParsedResults", [])).strip()
    print(f"[OCR] OCR.space extracted {len(text)} chars")
    return text


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """
    1. pdfplumber — free, instant for digital PDFs
    2. Vision API — for scanned/image-based PDFs (renders each page)
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

    # Scanned PDF — render each page → Vision API with OCR.space fallback
    print("[OCR] pdfplumber empty — rendering pages for Vision API")
    import pypdfium2 as pdfium
    doc   = pdfium.PdfDocument(pdf_bytes)
    parts = []
    for i, page in enumerate(doc):
        bitmap  = page.render(scale=2)
        pil_img = bitmap.to_pil()
        buf     = io.BytesIO()
        pil_img.save(buf, format="JPEG", quality=95)
        page_text = ocr_image(buf.getvalue(), file_ext="jpg")  # uses Vision + fallback
        print(f"[OCR] page {i+1}: {len(page_text)} chars")
        parts.append(page_text)
    return "\n".join(parts)


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

def extract_invoice_data(text: str) -> dict:
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

    inv = re.search(
        r"(?:invoice\s*(?:no|number|#|num)[:\s#]*|inv[:\s#]+|order\s*(?:no|#)[:\s]*)([\w\-/]+)",
        t, re.IGNORECASE,
    )
    invoice_number = inv.group(1).strip() if inv else "N/A"

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

    total_m    = re.search(r"(?:total|grand\s*total|amount\s*due)[^\d]*(\d[\d,\.]+)", t, re.IGNORECASE)
    vat_m      = re.search(r"(?:vat|iva|tax|igic)[^\d]*(\d[\d,\.]+)", t, re.IGNORECASE)
    vat_rate_m = re.search(r"(?:vat|iva|tax)[^\d]*(\d{1,2}(?:\.\d+)?)\s*%", t, re.IGNORECASE)

    total          = parse_amount(total_m.group(1)) if total_m else 0.0
    vat_amount_raw = parse_amount(vat_m.group(1))   if vat_m   else None
    vat_rate       = float(vat_rate_m.group(1))     if vat_rate_m else 21.0

    if vat_amount_raw is None:
        base_m = re.search(r"(?:subtotal|base|net\s*amount|before\s*tax)[^\d]*(\d[\d,\.]+)", t, re.IGNORECASE)
        if base_m:
            base           = parse_amount(base_m.group(1))
            vat_amount     = round(base * vat_rate / 100, 2)
            total_with_tax = round(base + vat_amount, 2)
            if total == 0.0:
                total = base
        else:
            base           = round(total / (1 + vat_rate / 100), 2)
            vat_amount     = round(total - base, 2)
            total_with_tax = total
    else:
        vat_amount     = vat_amount_raw
        total_with_tax = round(total + vat_amount, 2)

    sup_m    = re.search(r"(?:from[:\s]+|supplier[:\s]+|vendor[:\s]+|issued\s*by[:\s]+|company[:\s]+)([A-Z][^\n]{2,60})", t, re.IGNORECASE)
    caps_m   = re.search(r"^([A-Z][A-Z\s&\.,]{4,50})$", t, re.MULTILINE)
    business = sup_m.group(1).strip() if sup_m else (caps_m.group(1).strip() if caps_m else "N/A")

    email_m  = re.search(r"[\w\.\-]+@[\w\.\-]+\.\w{2,}", t)
    email    = email_m.group(0) if email_m else "N/A"

    cust_m   = re.search(r"(?:bill\s*to[:\s]+|invoice\s*to[:\s]+|sold\s*to[:\s]+|customer[:\s]+)([A-Z][^\n]{2,60})", t, re.IGNORECASE)
    customer = cust_m.group(1).strip() if cust_m else "N/A"

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
        "supplier": {"business_name": business, "address_line1": address_line1, "address_line2": address_line2, "Email": email},
        "customer": {"company_name": customer, "address_line1": "N/A", "address_line2": "N/A", "Email": "N/A"},
        "invoice":  {"invoice_number": invoice_number, "invoice_date": invoice_date, "due_date": due_date, "amount_in_words": "N/A"},
        "items":    items,
        "totals":   {"total": round(total, 2), "VAT_rate": vat_rate, "VAT_amount": round(vat_amount, 2), "Total_with_Tax": round(total_with_tax, 2)},
    }


# ── Per-voucher worker ────────────────────────────────────────────────────────

def process_single_voucher(voucher: dict, user_id: str) -> dict:
    voucher_id = str(voucher["_id"])
    voucher_collection.update_one(
        {"_id": ObjectId(voucher_id)},
        {"$set": {"OCR": "processing", "ocr_started_at": datetime.utcnow()}},
    )

    results = []

    for file_obj in voucher.get("files", []):
        if not isinstance(file_obj, dict):
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

                invoice_data  = extract_invoice_data(text)
                ledger_result = ledger_collection.insert_one({
                    "user_id": user_id, "voucher_id": voucher_id,
                    "file_name": file_name, "data_type": "toon",
                    "toon_data": toon_data, "ocr_text": text,
                    "invoice_data": invoice_data,
                    "processing_status": "success",
                    "created_at": datetime.utcnow(),
                })
                ledger_id = str(ledger_result.inserted_id)
                # ── Tax Classification Layer ──────────────────────────────
                try:
                    _classifier.classify_ledger_entry(ledger_id, user_id)
                except Exception as ce:
                    print(f"[ClassificationLayer] ⚠️ toon entry {ledger_id}: {ce}")
                results.append({"file_name": file_name, "data_type": "toon", "ledger_id": ledger_id, "status": "success"})
            except Exception as e:
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
                invoice_data  = extract_invoice_data(cleaned_text)
                ledger_result = ledger_collection.insert_one({
                    "user_id": user_id, "voucher_id": voucher_id,
                    "file_url": file_url, "s3_key": s3_key,
                    "data_type": "file", "ocr_text": cleaned_text,
                    "invoice_data": invoice_data,
                    "processing_status": "success",
                    "created_at": datetime.utcnow(),
                })
                ledger_id = str(ledger_result.inserted_id)
                # ── Tax Classification Layer ──────────────────────────────
                try:
                    _classifier.classify_ledger_entry(ledger_id, user_id)
                except Exception as ce:
                    print(f"[ClassificationLayer] ⚠️ file entry {ledger_id}: {ce}")
                results.append({"file_url": file_url, "s3_key": s3_key, "data_type": "file", "ledger_id": ledger_id, "status": "success"})
                print(f"[OCR] ✅ Success: {s3_key}")
            except Exception as e:
                import traceback
                print(f"[OCR] ❌ Failed: {s3_key}\n{traceback.format_exc()}")
                results.append({"file_url": file_url, "s3_key": s3_key, "data_type": "file", "status": "failed", "error": str(e)})

    all_ok     = all(r["status"] == "success" for r in results)
    any_fail   = any(r["status"] == "failed"  for r in results)
    ocr_status = "done" if (all_ok and results) else "partial" if any_fail else "failed" if not results else "unknown"

    voucher_collection.update_one(
        {"_id": ObjectId(voucher_id)},
        {"$set": {"OCR": ocr_status, "ocr_completed_at": datetime.utcnow()}},
    )
    return {"voucher_id": voucher_id, "file_count": len(results), "ocr_status": ocr_status, "files": results}


# ── Background job ────────────────────────────────────────────────────────────

def process_vouchers_background(job_id: str, user_id: str, voucher_object_ids: list):
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
            futures = {executor.submit(process_single_voucher, v, user_id): v for v in vouchers}
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

    job    = ocr_jobs_collection.insert_one({"user_id": user_id, "voucher_ids": voucher_id_list, "status": "awaiting", "total_vouchers": count, "created_at": datetime.utcnow()})
    job_id = str(job.inserted_id)
    background_tasks.add_task(process_vouchers_background, job_id, user_id, voucher_object_ids)

    return {"message": "OCR processing started", "job_id": job_id, "user_id": user_id, "total_vouchers": count, "status": "awaiting", "check_status_url": f"/accounting/ocr/job/{job_id}"}


@router.get("/job/{job_id}")
async def get_ocr_job_status(job_id: str):
    job = ocr_jobs_collection.find_one({"_id": ObjectId(job_id)})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job["_id"] = str(job["_id"])
    return {"job_id": job_id, "status": job.get("status"), "user_id": job.get("user_id"), "total_vouchers": job.get("total_vouchers"), "created_at": job.get("created_at"), "started_at": job.get("started_at"), "completed_at": job.get("completed_at"), "failed_at": job.get("failed_at"), "error": job.get("error"), "results": job.get("results")}
