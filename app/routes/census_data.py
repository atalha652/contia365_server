"""
Census Data Routes for Contia365
Upload PDF/Word Certificado de Situación Censal → extract → store in MongoDB
"""

import os
from datetime import datetime
from typing import List, Optional

import certifi
from bson import ObjectId
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from gridfs import GridFS
from pymongo import MongoClient

from app.models.census_data import (
    CensusDataBase,
    CensusDataCreate,
    CensusDataUpdate,
    PlatformVerification,
)
from app.routes.auth import get_current_user
from app.services.census_data_service import (
    build_ocr_confidence,
    extract_text_from_file,
    parse_census_data_from_text,
)
from app.services.onboarding_status import persist_computed_onboarding
from app.services.fiscal_profile_service import (
    canonical_profile_updates,
    derive_periodic_tax_obligations,
    get_canonical_fiscal_profile,
    merge_profile_data,
    set_canonical_profile_id,
)

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")

client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = client[DB_NAME]
census_collection = db["census_data"]
users_collection = db["users"]
document_store = GridFS(db, collection="fiscal_documents")

# Compound index for duplicate detection (NIF/NIE + issue date across all users)
census_collection.create_index(
    [("taxpayer_identity.nif_nie", 1), ("document_metadata.issue_date", 1)],
    name="nif_issue_date_idx",
    sparse=True,
)
census_collection.create_index(
    [("user_id", 1), ("is_canonical", 1)],
    name="one_canonical_fiscal_profile_per_user",
    unique=True,
    partialFilterExpression={"is_canonical": True},
)

router = APIRouter(prefix="/census-data", tags=["Census Data"])

ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
}
MAX_FILE_SIZE_MB = 10


def _record_response(doc: dict) -> dict:
    doc["_id"] = str(doc["_id"])
    return doc


def _sync_user_tax_id(user_id, taxpayer_identity: Optional[dict]):
    nif_nie = (taxpayer_identity or {}).get("nif_nie")
    if nif_nie:
        users_collection.update_one(
            {"_id": user_id},
            {"$set": {"tax_id": nif_nie, "dni_nie": nif_nie, "updated_at": datetime.utcnow()}},
        )


def _refresh_onboarding(current_user: dict) -> dict:
    fresh_user = users_collection.find_one({"_id": current_user["_id"]}) or current_user
    return persist_computed_onboarding(users_collection, census_collection, fresh_user)


def _canonical_profile(current_user: dict) -> Optional[dict]:
    return get_canonical_fiscal_profile(
        users_collection, census_collection, current_user["_id"]
    )


def _save_canonical_profile(current_user: dict, profile: dict) -> dict:
    now = datetime.utcnow()
    existing_id = profile.pop("_id", None)
    profile["country"] = current_user.get("country") or profile.get("country")
    profile["user_type"] = (
        current_user.get("user_type_selection") or profile.get("user_type")
    )
    profile.update(canonical_profile_updates(profile))
    profile["user_id"] = str(current_user["_id"])
    profile["organization_id"] = str(
        current_user.get("organization_id", current_user["_id"])
    )
    profile["updated_at"] = now

    if existing_id:
        census_collection.replace_one({"_id": existing_id}, profile)
        profile_id = existing_id
    else:
        profile["created_at"] = now
        profile_id = census_collection.insert_one(profile).inserted_id

    set_canonical_profile_id(users_collection, current_user["_id"], profile_id)
    saved = census_collection.find_one({"_id": profile_id})
    # Profile changes can alter applicable modelos; refresh existing ledger tags.
    try:
        from app.services.tax_classification_service import TaxClassificationService
        TaxClassificationService().backfill_user(str(current_user["_id"]))
    except Exception:
        # The profile save remains authoritative even if a downstream refresh
        # is temporarily unavailable; /tax-engine/backfill can retry it.
        import logging
        logging.getLogger(__name__).exception(
            "Could not refresh tax classifications after fiscal profile update"
        )
    return saved


@router.post("/", status_code=201)
async def save_census_profile(
    payload: CensusDataBase,
    current_user: dict = Depends(get_current_user),
):
    """Save Spain fiscal profile from the form (no file required)."""
    existing = _canonical_profile(current_user) or {}
    fields = payload.model_dump(
        exclude={"platform_verification"}, exclude_unset=True, mode="json"
    )
    doc = merge_profile_data(existing, fields, incoming_wins=True)
    doc.setdefault(
        "platform_verification",
        PlatformVerification(verification_status="PENDING").model_dump(mode="json"),
    )
    doc["source"] = "form+upload" if doc.get("documents") else "form"
    saved = _save_canonical_profile(current_user, doc)
    _sync_user_tax_id(current_user["_id"], saved.get("taxpayer_identity"))
    status = _refresh_onboarding(current_user)
    return {
        "message": "Fiscal profile saved successfully.",
        **_record_response(saved),
        "onboarding_completed": status["onboarding_completed"],
        "fiscal_profile_completed": status["fiscal_profile_completed"],
        "current_step": status["current_step"],
    }


@router.patch("/{record_id}")
async def update_census_profile(
    record_id: str,
    payload: CensusDataUpdate,
    current_user: dict = Depends(get_current_user),
):
    """Update fiscal fields after upload or after a previous form save."""
    if not ObjectId.is_valid(record_id):
        raise HTTPException(status_code=400, detail="Invalid record ID.")

    existing = census_collection.find_one(
        {"_id": ObjectId(record_id), "user_id": str(current_user["_id"])}
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Census record not found.")
    canonical = _canonical_profile(current_user)
    if canonical and canonical["_id"] != existing["_id"]:
        raise HTTPException(
            status_code=409,
            detail="Update the canonical fiscal profile returned by GET /census-data/me.",
        )

    updates = payload.model_dump(exclude_unset=True, mode="json")
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update.")

    merged = merge_profile_data(existing, updates, incoming_wins=True)
    merged["source"] = "form+upload" if merged.get("documents") else "form"
    saved = _save_canonical_profile(current_user, merged)
    _sync_user_tax_id(current_user["_id"], saved.get("taxpayer_identity"))
    status = _refresh_onboarding(current_user)
    return {
        "message": "Fiscal profile updated successfully.",
        **_record_response(saved),
        "onboarding_completed": status["onboarding_completed"],
        "fiscal_profile_completed": status["fiscal_profile_completed"],
        "current_step": status["current_step"],
    }


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

    # --- Duplicate check: NIF/NIE + issue date must be unique across all users ---
    nif_nie = (extracted.get("taxpayer_identity") or {}).get("nif_nie")
    issue_date = (extracted.get("document_metadata") or {}).get("issue_date")

    if nif_nie and issue_date:
        existing = census_collection.find_one({
            "taxpayer_identity.nif_nie": nif_nie,
            "document_metadata.issue_date": issue_date,
        })
        if existing:
            existing_user_id = str(existing.get("user_id", ""))
            current_user_id = str(current_user["_id"])
            if existing_user_id != current_user_id:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"This document (NIF: {nif_nie}, issued: {issue_date}) "
                        "has already been uploaded by another user."
                    ),
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

    # --- Store original document and update the one canonical profile ---
    now = datetime.utcnow()
    file_id = document_store.put(
        file_bytes,
        filename=file.filename,
        content_type=file.content_type,
        user_id=str(current_user["_id"]),
        uploaded_at=now,
    )
    document_link = {
        "file_id": str(file_id),
        "filename": file.filename,
        "content_type": file.content_type,
        "uploaded_at": now,
        "ocr_confidence_score": confidence,
    }

    existing_profile = _canonical_profile(current_user) or {}
    # Existing reviewed form values win; OCR fills missing fields.
    merged = merge_profile_data(existing_profile, doc, incoming_wins=False)
    merged["periodic_tax_obligations"] = derive_periodic_tax_obligations(
        merged,
        list(existing_profile.get("periodic_tax_obligations") or [])
        + list(doc.get("periodic_tax_obligations") or []),
    )
    documents = list(existing_profile.get("documents") or [])
    documents.append(document_link)
    merged["documents"] = documents
    merged["source"] = "form+upload" if existing_profile else "upload"
    saved = _save_canonical_profile(current_user, merged)
    _sync_user_tax_id(current_user["_id"], saved.get("taxpayer_identity"))
    status = _refresh_onboarding(current_user)

    taxpayer = saved.get("taxpayer_identity", {})
    return {
        "message": "Census document linked and fiscal profile updated successfully.",
        "id": str(saved["_id"]),
        "document_file_id": str(file_id),
        "ocr_confidence_score": confidence,
        "nif_nie": taxpayer.get("nif_nie"),
        "full_name": taxpayer.get("full_name"),
        "document_type": (saved.get("document_metadata") or {}).get("document_type"),
        "activities_count": len(
            (saved.get("professional_registration") or {}).get("economic_activities", [])
        ),
        "obligations_count": len(saved.get("periodic_tax_obligations", [])),
        "onboarding_completed": status["onboarding_completed"],
        "fiscal_profile_completed": status["fiscal_profile_completed"],
        "current_step": status["current_step"],
    }


@router.get("/me", response_model=dict)
async def get_my_fiscal_profile(current_user: dict = Depends(get_current_user)):
    """Return the current user's one canonical fiscal profile."""
    profile = _canonical_profile(current_user)
    if not profile:
        raise HTTPException(status_code=404, detail="Fiscal profile not found.")
    return _record_response(profile)


@router.get("/documents/{file_id}")
async def download_census_document(
    file_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Download a document linked to the authenticated user's fiscal profile."""
    if not ObjectId.is_valid(file_id):
        raise HTTPException(status_code=400, detail="Invalid document ID.")
    stored = document_store.find_one({
        "_id": ObjectId(file_id),
        "user_id": str(current_user["_id"]),
    })
    if not stored:
        raise HTTPException(status_code=404, detail="Fiscal document not found.")
    return Response(
        content=stored.read(),
        media_type=stored.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{stored.filename}"'
        },
    )


@router.get("/", response_model=List[dict])
async def list_census_records(current_user: dict = Depends(get_current_user)):
    """
    List all census data records belonging to the current user.
    """
    user_id = str(current_user["_id"])
    canonical = _canonical_profile(current_user)
    records = [canonical] if canonical else []
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

    record = census_collection.find_one(
        {"_id": ObjectId(record_id), "user_id": str(current_user["_id"])}
    )
    if not record:
        raise HTTPException(status_code=404, detail="Census record not found.")
    for document in record.get("documents") or []:
        file_id = document.get("file_id")
        if file_id and ObjectId.is_valid(str(file_id)):
            try:
                document_store.delete(ObjectId(str(file_id)))
            except Exception:
                pass
    result = census_collection.delete_one({"_id": record["_id"]})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Census record not found.")

    users_collection.update_one(
        {"_id": current_user["_id"], "fiscal_profile_id": record_id},
        {"$unset": {"fiscal_profile_id": ""}},
    )
    status = _refresh_onboarding(current_user)
    return {
        "message": "Census record deleted successfully.",
        "onboarding_completed": status["onboarding_completed"],
        "fiscal_profile_completed": status["fiscal_profile_completed"],
        "current_step": status["current_step"],
    }
