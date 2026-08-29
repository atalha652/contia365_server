
from fastapi import APIRouter, File, UploadFile, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from PIL import Image
import pytesseract
import io
import re
import tempfile
from google import genai
from google.genai import types
import os
import re
import xml.etree.ElementTree as ET
from openai import OpenAI
import bcrypt
from datetime import datetime, timedelta
import certifi
from app.routes.auth import get_current_user
from fastapi import FastAPI, HTTPException
from pymongo import MongoClient
from fastapi.security import OAuth2PasswordBearer
import certifi
from dotenv import load_dotenv
from pydantic import BaseModel, EmailStr, Field
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, File, UploadFile, Depends, Form, HTTPException
from bson import ObjectId
from fastapi import APIRouter, File, Form, Depends, HTTPException, UploadFile
import boto3
import json
from PIL import Image
import io
import os
from openai import OpenAI
from fastapi import Form
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, 
    PageBreak
)
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.mime.text import MIMEText
from urllib.parse import unquote
from app.utils.ledger_display import (
    format_bank_ledger_for_display,
    is_invoice_issue_ledger_entry,
)

load_dotenv()
# Set Tesseract path (Windows)
AWS_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")
s3 = boto3.client(
    "s3",
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    region_name="eu-north-1"
)

bucket_name = "ai-auto-invoice"



client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = client[DB_NAME]
# Replace 'db' with your actual DB object
voucher_collection = db["voucher"]
ocr_collection = db["ocr"] 
users_collection = db["users"]
ledger_collection = db["ledger"]
ocr_jobs_collection = db["ocr_jobs"]



router = APIRouter(prefix="/accounting/ledgers", tags=["Ledgers"])


# Pydantic Model
class LedgerUpdateRequest(BaseModel):
    invoice_data: Dict[str, Any] = Field(..., description="Complete invoice data to update")


@router.get("/user/{user_id}")
async def get_ledger_by_user(
    user_id: str
):
    """
    Get all ledger entries for a specific user.
    Fetches from 'ledger' (OCR/manual) and bank postings in 'ledger_entries'.
    Issued-invoice documents in ledger_entries are excluded (they duplicate the voucher row).
    Example: GET /accounting/ledgers/user/123
    """
    try:
        # Get user's organization_id
        user = users_collection.find_one({"_id": ObjectId(user_id)})
        organization_id = str(user.get("organization_id", user_id)) if user else user_id
        # Query 1: Fetch from old 'ledger' collection (OCR-based ledger)
        query_ocr = {"user_id": user_id}
        ocr_ledger_entries = list(ledger_collection.find(query_ocr).sort("created_at", -1))

        # Format OCR entries - keep original format
        for entry in ocr_ledger_entries:
            entry["_id"] = str(entry["_id"])
            if isinstance(entry.get("created_at"), datetime):
                entry["created_at"] = entry["created_at"].strftime("%Y-%m-%d %H:%M:%S")

        # Query 2: Bank postings only — skip invoice-issue rows (those have invoice_id)
        ledger_entries_collection = db["ledger_entries"]
        query_accounting = {
            "organization_id": organization_id,
            "invoice_id": {"$exists": False},
        }
        accounting_ledger_entries = list(ledger_entries_collection.find(query_accounting).sort("created_at", -1))

        formatted_accounting_entries = [
            format_bank_ledger_for_display(entry, user_id)
            for entry in accounting_ledger_entries
            if not is_invoice_issue_ledger_entry(entry)
        ]

        # Combine both lists
        all_entries = ocr_ledger_entries + formatted_accounting_entries

        # Enrich entries with modelo details if modelo_id exists
        for entry in all_entries:
            if entry.get("modelo_id"):
                try:
                    modelo = db["modelos"].find_one({"_id": ObjectId(entry["modelo_id"])})
                    if modelo:
                        entry["modelo"] = {
                            "_id": str(modelo["_id"]),
                            "modelo_no": modelo.get("modelo_no"),
                            "name": modelo.get("name"),
                            "periodicity": modelo.get("periodicity"),
                            "deadline": modelo.get("deadline")
                        }
                except Exception as e:
                    # If lookup fails, just continue without modelo details
                    pass

        # Sort combined list by created_at (newest first)
        all_entries.sort(key=lambda x: x.get("created_at", ""), reverse=True)

        total_count = len(all_entries)

        if total_count == 0:
            return {
                "user_id": user_id,
                "entries": [],
                "total_count": 0,
                "message": "No ledger entries found for this user"
            }

        # Return in original format (backward compatible)
        return {
            "user_id": user_id,
            "entries": all_entries,
            "total_count": total_count
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving ledger entries: {str(e)}")


@router.get("/user/{user_id}/export-pdf")
async def export_ledger_pdf(
    user_id: str,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    entry_type: Optional[str] = "all",
    ids: Optional[str] = None
):
    """
    Export ledger entries as PDF file (direct download)

    Query Parameters:
    - from_date (optional): Filter entries from this date (YYYY-MM-DD)
    - to_date (optional): Filter entries up to this date (YYYY-MM-DD)
    - entry_type (optional): Filter by type - 'bank_transaction', 'toon', or 'all' (default: 'all')
    - ids (optional): Comma-separated list of specific entry IDs to export (e.g., "id1,id2,id3")
                     When provided, only these specific entries will be exported

    Returns: PDF file as direct download
    """
    try:
        from app.utils.pdf_generator import generate_ledger_pdf

        # Get user's organization_id
        user = users_collection.find_one({"_id": ObjectId(user_id)})
        organization_id = str(user.get("organization_id", user_id)) if user else user_id

        # Parse specific IDs if provided
        specific_ids = []
        if ids:
            specific_ids = [id.strip() for id in ids.split(",") if id.strip()]

        # Query 1: Fetch from old 'ledger' collection (OCR-based ledger)
        if specific_ids:
            # Fetch specific entries by IDs
            ocr_ids = [ObjectId(id) for id in specific_ids if ObjectId.is_valid(id)]
            query_ocr = {"_id": {"$in": ocr_ids}, "user_id": user_id}
        else:
            # Fetch all entries for user
            query_ocr = {"user_id": user_id}

        ocr_ledger_entries = list(ledger_collection.find(query_ocr).sort("created_at", -1))

        # Format OCR entries - keep original format
        for entry in ocr_ledger_entries:
            entry["_id"] = str(entry["_id"])
            if isinstance(entry.get("created_at"), datetime):
                entry["created_at"] = entry["created_at"].strftime("%Y-%m-%d %H:%M:%S")

        # Query 2: Bank postings only — skip invoice-issue rows (those have invoice_id)
        ledger_entries_collection = db["ledger_entries"]
        query_accounting = {
            "organization_id": organization_id,
            "invoice_id": {"$exists": False},
        }
        if specific_ids:
            accounting_ids = [ObjectId(id) for id in specific_ids if ObjectId.is_valid(id)]
            query_accounting["_id"] = {"$in": accounting_ids}

        accounting_ledger_entries = list(ledger_entries_collection.find(query_accounting).sort("created_at", -1))

        formatted_accounting_entries = [
            format_bank_ledger_for_display(entry, user_id)
            for entry in accounting_ledger_entries
            if not is_invoice_issue_ledger_entry(entry)
        ]

        # Combine both lists
        all_entries = ocr_ledger_entries + formatted_accounting_entries

        # Apply filters
        filtered_entries = []
        for entry in all_entries:
            # Filter by entry type
            if entry_type != "all":
                if entry.get("data_type") != entry_type:
                    continue

            # Filter by date range
            entry_date_str = entry.get("created_at", "")
            if from_date or to_date:
                try:
                    # Parse entry date
                    entry_date = datetime.strptime(entry_date_str[:10], "%Y-%m-%d")

                    if from_date:
                        from_dt = datetime.strptime(from_date, "%Y-%m-%d")
                        if entry_date < from_dt:
                            continue

                    if to_date:
                        to_dt = datetime.strptime(to_date, "%Y-%m-%d")
                        if entry_date > to_dt:
                            continue
                except:
                    # If date parsing fails, include the entry
                    pass

            filtered_entries.append(entry)

        # Sort by created_at (newest first)
        filtered_entries.sort(key=lambda x: x.get("created_at", ""), reverse=True)

        if not filtered_entries:
            raise HTTPException(status_code=404, detail="No ledger entries found matching the criteria")

        # Prepare user info and filters for PDF
        user_info = {
            "user_id": user_id,
            "organization_id": organization_id
        }

        filters = {
            "from_date": from_date,
            "to_date": to_date,
            "entry_type": entry_type
        }

        # Generate PDF
        pdf_buffer = generate_ledger_pdf(filtered_entries, user_info, filters)

        # Create filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"ledger_export_{user_id}_{timestamp}.pdf"

        # Return PDF as streaming response with download headers
        return StreamingResponse(
            pdf_buffer,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f"attachment; filename={filename}"
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generating PDF: {str(e)}")


@router.put("/{ledger_id}")
async def update_ledger_entry(
    ledger_id: str,
    update_data: LedgerUpdateRequest
):
    """
    Update only the invoice_data field of a ledger entry.
    Example: PUT /accounting/ledgers/69083ec9be8d0f81ff44275b
    Body: {
        "invoice_data": {
            "supplier": {
                "business_name": "Your Company Inc.",
                "address_line1": "1234 Company St",
                "address_line2": "Company Town, ST 12345",
                "Email": "support@example.com"
            },
            "customer": {
                "company_name": "Customer Name",
                "address_line1": "1234 Customer St",
                "address_line2": "Customer Town, ST 12345",
                "Email": ""
            },
            "invoice": {
                "invoice_number": "0000457",
                "invoice_date": "11-04-2025",
                "due_date": "",
                "amount_in_words": "Two hundred fifty-two dollars"
            },
            "items": [
                {
                    "description": "Product A",
                    "qty": 2,
                    "unit_price": 45.00,
                    "subtotal": 90.00
                }
            ],
            "totals": {
                "total": 240.00,
                "VAT_rate": 21.0,
                "VAT_amount": 50.40,
                "Total_with_Tax": 290.40
            }
        }
    }
    """
    try:
        # Check if ledger entry exists
        ledger_entry = ledger_collection.find_one({"_id": ObjectId(ledger_id)})
        if not ledger_entry:
            raise HTTPException(status_code=404, detail="Ledger entry not found")
        
        # Update only invoice_data field
        update_doc = {
            "invoice_data": update_data.invoice_data,
            "updated_at": datetime.utcnow()
        }
        
        # Update the ledger entry
        result = ledger_collection.update_one(
            {"_id": ObjectId(ledger_id)},
            {"$set": update_doc}
        )
        
        if result.modified_count == 0:
            raise HTTPException(status_code=500, detail="Failed to update ledger entry")
        
        user_id = str(ledger_entry.get("user_id") or "")
        if user_id:
            try:
                from app.services.tax_classification_service import TaxClassificationService
                TaxClassificationService().classify_ledger_entry(ledger_id, user_id)
            except Exception:
                pass

        # Get updated entry
        updated_entry = ledger_collection.find_one({"_id": ObjectId(ledger_id)})
        updated_entry["_id"] = str(updated_entry["_id"])
        if isinstance(updated_entry.get("created_at"), datetime):
            updated_entry["created_at"] = updated_entry["created_at"].strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(updated_entry.get("updated_at"), datetime):
            updated_entry["updated_at"] = updated_entry["updated_at"].strftime("%Y-%m-%d %H:%M:%S")
        
        return {
            "message": "Invoice data updated successfully",
            "ledger_id": ledger_id,
            "updated_entry": updated_entry
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error updating ledger entry: {str(e)}")


@router.put("/{entry_id}/modelo")
async def update_ledger_modelo(
    entry_id: str,
    modelo_id: str = None,
    user_id: str = None
):
    """
    Update ledger entry with a modelo by its _id.
    Assigns the modelo_id to the ledger entry.
    Checks both 'ledger' and 'ledger_entries' collections.
    """
    try:
        # Validate entry exists
        if not ObjectId.is_valid(entry_id):
            raise HTTPException(status_code=400, detail="Invalid entry ID format")
        
        # Check both collections
        ledger_entries_collection = db["ledger_entries"]
        entry = ledger_entries_collection.find_one({"_id": ObjectId(entry_id)})
        collection_used = "ledger_entries"
        
        # If not in ledger_entries, check ledger
        if not entry:
            entry = ledger_collection.find_one({"_id": ObjectId(entry_id)})
            collection_used = "ledger"
        
        if not entry:
            raise HTTPException(status_code=404, detail="Ledger entry not found in either collection")
        
        # Validate modelo exists if provided
        if modelo_id:
            if not ObjectId.is_valid(modelo_id):
                raise HTTPException(status_code=400, detail="Invalid modelo ID format")
            
            modelo = db["modelos"].find_one({"_id": ObjectId(modelo_id)})
            if not modelo:
                raise HTTPException(status_code=404, detail="Modelo not found")
        
            # Update in the collection where entry was found
            if collection_used == "ledger_entries":
                result = ledger_entries_collection.update_one(
                    {"_id": ObjectId(entry_id)},
                    {"$set": {
                        "modelo_id": modelo_id,
                        "updated_at": datetime.utcnow()
                    }}
                )
            else:
                result = ledger_collection.update_one(
                    {"_id": ObjectId(entry_id)},
                    {"$set": {
                        "modelo_id": modelo_id,
                        "updated_at": datetime.utcnow()
                    }}
                )
            
            if result.modified_count == 0:
                raise HTTPException(status_code=500, detail="Failed to update ledger entry")
            
            return {
                "message": "Modelo assigned successfully",
                "entry_id": entry_id,
                "collection": collection_used,
                "modelo_id": modelo_id,
                "modelo_no": modelo.get("modelo_no"),
                "modelo_name": modelo.get("name")
            }
        
        raise HTTPException(status_code=400, detail="modelo_id is required")
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{ledger_id}")
async def delete_ledger_entry(
    ledger_id: str
):
    """
    Delete a ledger entry by its ID.
    Example: DELETE /accounting/ledgers/69083ec9be8d0f81ff44275b
    """
    try:
        # Check if ledger entry exists
        ledger_entry = ledger_collection.find_one({"_id": ObjectId(ledger_id)})
        if not ledger_entry:
            raise HTTPException(status_code=404, detail="Ledger entry not found")
        
        # Delete the ledger entry
        result = ledger_collection.delete_one({"_id": ObjectId(ledger_id)})
        
        if result.deleted_count == 0:
            raise HTTPException(status_code=500, detail="Failed to delete ledger entry")
        
        return {
            "message": "Ledger entry deleted successfully",
            "ledger_id": ledger_id,
            "deleted_entry": {
                "user_id": ledger_entry.get("user_id"),
                "voucher_id": ledger_entry.get("voucher_id"),
                "processing_status": ledger_entry.get("processing_status")
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error deleting ledger entry: {str(e)}")

