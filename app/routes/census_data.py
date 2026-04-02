"""
Census Data Routes for Contia365
Upload PDF/Word Certificado de Situación Censal → extract → store in MongoDB
"""

import os
from datetime import datetime
from typing import List

import certifi
from bson import ObjectId
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pymongo import MongoClient

from app.models.census_data import (
    CensusDataCreate,
    PlatformVerification,
)
from app.routes.auth import get_current_user
from app.services.census_data_service import (
    build_ocr_confidence,
    extract_text_from_file,
    parse_census_data_from_text,
)

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")

client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = client[DB_NAME]
census_collection = db["census_data"]

router = APIRouter(prefix="/census-data", tags=["Census Data"])

ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
}
MAX_FILE_SIZE_MB = 10


@router.post("/upload", status_code=201)
async def upload_census_document(
    file: UploadFile = File(..., description="PDF or Word (.docx) Certificado de Situación Censal"),
    current_user: dict = Depends(get_current_user),
):
    """
    Upload a PDF or Word census certificate, extract structured data using AI,
    and store the result in the census_data collection.
    """
    # --- Validate file type ---
    if file.content_type not in ALLOWED_CONTENT_TYPES and not file.filename.lower().endswith(
        (".pdf", ".docx", ".doc")
    ):
        raise HTTPException(
            status_code=415,
            detail="Unsupported file type. Please upload a PDF or Word (.docx) document.",
        )

    # --- Read and size-check ---
    file_bytes = await file.read()
    size_mb = len(file_bytes) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Maximum allowed size is {MAX_FILE_SIZE_MB} MB.",
        )

    # --- Extract text ---
    try:
        raw_text = extract_text_from_file(file_bytes, file.content_type, file.filename)
    except ValueError as e:
        raise HTTPException(status_code=415, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Failed to read file: {str(e)}")

    if not raw_text.strip():
        raise HTTPException(
            status_code=422,
            detail="Could not extract any text from the document. The file may be scanned or corrupted.",
        )

    # --- Parse document ---
    try:
        extracted = parse_census_data_from_text(raw_text)
    except Exception as e:
        raise HTTPException(
            status_code=422,
            detail=f"Document parsing failed: {str(e)}",
        )

    # --- Build platform verification metadata ---
    confidence = build_ocr_confidence(raw_text)
    platform_verification = PlatformVerification(
        verification_status="APPROVED",
        verified_at=datetime.utcnow(),
        needs_renewal_at=None,
    )

    # --- Assemble and validate the full model ---
    try:
        census_record = CensusDataCreate(
            **extracted,
            user_id=str(current_user["_id"]),
            organization_id=str(current_user.get("organization_id", current_user["_id"])),
            platform_verification=platform_verification,
        )
        doc = census_record.model_dump(exclude_none=False)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            "CensusDataCreate validation warning (storing raw): %s", str(e)
        )
        doc = {
            **extracted,
            "user_id": str(current_user["_id"]),
            "organization_id": str(current_user.get("organization_id", current_user["_id"])),
            "platform_verification": platform_verification.model_dump(),
        }

    # --- Persist to MongoDB ---
    now = datetime.utcnow()
    doc["created_at"] = now
    doc["updated_at"] = now

    result = census_collection.insert_one(doc)

    taxpayer = extracted.get("taxpayer_identity", {})
    return {
        "message": "Census data extracted and stored successfully.",
        "id": str(result.inserted_id),
        "ocr_confidence_score": confidence,
        "nif_nie": taxpayer.get("nif_nie"),
        "full_name": taxpayer.get("full_name"),
        "document_type": extracted.get("document_metadata", {}).get("document_type"),
        "activities_count": len(
            (extracted.get("professional_registration") or {}).get("economic_activities", [])
        ),
        "obligations_count": len(extracted.get("periodic_tax_obligations", [])),
    }

@router.get("/", response_model=List[dict])
async def list_census_records(current_user: dict = Depends(get_current_user)):
    """
    List all census data records belonging to the current user.
    """
    user_id = str(current_user["_id"])
    records = list(census_collection.find({"user_id": user_id}))
    for r in records:
        r["_id"] = str(r["_id"])
    return records


@router.get("/{record_id}", response_model=dict)
async def get_census_record(
    record_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Retrieve a single census data record by ID.
    """
    if not ObjectId.is_valid(record_id):
        raise HTTPException(status_code=400, detail="Invalid record ID.")

    record = census_collection.find_one(
        {"_id": ObjectId(record_id), "user_id": str(current_user["_id"])}
    )
    if not record:
        raise HTTPException(status_code=404, detail="Census record not found.")

    record["_id"] = str(record["_id"])
    return record


@router.delete("/{record_id}", status_code=200)
async def delete_census_record(
    record_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Delete a census data record by ID.
    """
    if not ObjectId.is_valid(record_id):
        raise HTTPException(status_code=400, detail="Invalid record ID.")

    result = census_collection.delete_one(
        {"_id": ObjectId(record_id), "user_id": str(current_user["_id"])}
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Census record not found.")

    return {"message": "Census record deleted successfully."}
