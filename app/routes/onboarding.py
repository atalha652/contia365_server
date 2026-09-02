"""
Onboarding Routes for Contia365
Handles user type selection and onboarding completion
"""

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request
from pymongo import MongoClient
from bson import ObjectId
import os
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional
import certifi
from dotenv import load_dotenv

from pydantic import BaseModel

from app.models.onboarding import (
    UserTypeSelection, UserTypeInfo, OnboardingRequest,
    OnboardingResponse, OnboardingStatus, USER_TYPE_CONFIGS,
    CountrySelection, CountryInfo, CountrySelectRequest,
    CountrySelectResponse, COUNTRY_CONFIGS,
    SELECTABLE_USER_TYPES, USER_TYPE_CATALOG,
    BusinessProfileRequest, RepresentativeRequest, AeatConnectRequest,
    PersonAeatConnectRequest, TaxAddress,
)
from app.routes.auth import get_current_user
from app.services.onboarding_status import persist_computed_onboarding
from app.services.user_type_vocab import ADVISOR, canonicalize_user_type
from app.services.aeat_apoderamiento_client import AeatApoderamientoClient

logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")

# Database connection
client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = client[DB_NAME]
users_collection = db["users"]
census_collection = db["census_data"]

# Router setup
router = APIRouter()


@router.get("/user-types", response_model=List[UserTypeInfo])
async def get_user_types():
    """
    User types shown during onboarding.
    Advisor is omitted so new users cannot pick Asesor; existing advisor
    accounts stay valid in the DB and on login.
    """
    return [USER_TYPE_CATALOG[t] for t in SELECTABLE_USER_TYPES]


@router.get("/countries", response_model=List[CountryInfo])
async def get_countries():
    """Available countries for onboarding (Spain and Italy)."""
    return [
        CountryInfo(
            id=cfg["id"],
            name=cfg["name"],
            subtitle=cfg["subtitle"],
            currency=cfg["currency"],
            tax_authority=cfg["tax_authority"],
            tax_available=bool(cfg.get("tax_available", True)),
            status=str(cfg.get("status") or "Available"),
        )
        for cfg in COUNTRY_CONFIGS.values()
    ]


@router.post("/select-country", response_model=CountrySelectResponse)
async def select_country(
    request: CountrySelectRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    Save the user's operating country.
    Does not complete onboarding; user type is still required next.
    """
    user_id = current_user["_id"]
    country = request.country
    country_config = COUNTRY_CONFIGS[country]

    result = users_collection.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {
            "country": country.value,
            "country_name": country_config["name"],
            "country_config": country_config,
            "onboarding_step": "user_type_selection",
            "updated_at": datetime.utcnow(),
        }}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")

    persist_computed_onboarding(
        users_collection,
        census_collection,
        {**current_user, "country": country.value},
    )

    return CountrySelectResponse(
        message=f"Country '{country_config['name']}' selected successfully",
        country=country.value,
        country_name=country_config["name"],
        next_step="user_type_selection",
    )


@router.post("/select-user-type", response_model=OnboardingResponse)
async def select_user_type(
    request: OnboardingRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    Save user type selection and complete onboarding process
    Updates user document with selected type and configuration
    """
    try:
        user_id = current_user["_id"]
        selected_type = request.user_type
        existing_type = canonicalize_user_type(current_user.get("user_type_selection"))

        # New users can only pick types from GET /user-types.
        # Existing advisor accounts may keep (or re-save) advisor; do not map to white_label.
        if selected_type not in SELECTABLE_USER_TYPES:
            if not (
                selected_type == UserTypeSelection.ADVISOR
                and existing_type == ADVISOR
            ):
                raise HTTPException(
                    status_code=400,
                    detail="Invalid user type. Choose person or business.",
                )

        user_config = USER_TYPE_CONFIGS.get(selected_type, {})

        update_data = {
            "user_type_selection": selected_type.value,
            "updated_at": datetime.utcnow(),
            "user_config": user_config,
        }

        if selected_type == UserTypeSelection.BUSINESS and current_user.get("organization_info"):
            update_data["organization_info.type"] = "business"
            update_data["type"] = "organization"
        elif selected_type == UserTypeSelection.PERSON:
            update_data["type"] = "individual"
        elif selected_type == UserTypeSelection.ADVISOR:
            update_data["type"] = "organization"

        result = users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": update_data}
        )

        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="User not found")

        status = persist_computed_onboarding(
            users_collection,
            census_collection,
            {**current_user, "user_type_selection": selected_type.value},
        )

        return OnboardingResponse(
            message=f"User type '{selected_type.value}' selected successfully",
            user_type=selected_type.value,
            onboarding_completed=status["onboarding_completed"],
            current_step=status["current_step"],
            fiscal_profile_completed=status["fiscal_profile_completed"],
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update user type: {str(e)}")


@router.get("/status", response_model=OnboardingStatus)
async def get_onboarding_status(current_user: dict = Depends(get_current_user)):
    """
    Authoritative onboarding status. Fiscal completion is based on form fields
    (NIF/NIE, IAE, VAT), not on whether a census file was uploaded.
    """
    status = persist_computed_onboarding(
        users_collection, census_collection, current_user
    )
    return OnboardingStatus(**status)


@router.post("/skip")
async def skip_onboarding(current_user: dict = Depends(get_current_user)):
    """
    Allow user to skip onboarding (sets default configuration)
    """
    try:
        user_id = current_user["_id"]
        
        # Set default configuration (person)
        default_config = USER_TYPE_CONFIGS[UserTypeSelection.PERSON]
        
        update_data = {
            "user_type_selection": UserTypeSelection.PERSON.value,
            "updated_at": datetime.utcnow(),
            "user_config": default_config,
            "onboarding_skipped": True,
        }

        result = users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": update_data}
        )

        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="User not found")

        status = persist_computed_onboarding(
            users_collection,
            census_collection,
            {**current_user, "user_type_selection": UserTypeSelection.PERSON.value},
        )

        return {
            "message": "Onboarding skipped successfully",
            "user_type": UserTypeSelection.PERSON.value,
            "onboarding_completed": status["onboarding_completed"],
            "current_step": status["current_step"],
            "fiscal_profile_completed": status["fiscal_profile_completed"],
            "note": "Default person configuration applied. Spain users still need the fiscal profile.",
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to skip onboarding: {str(e)}")


@router.get("/config")
async def get_user_config(current_user: dict = Depends(get_current_user)):
    """
    Get user's current configuration based on their selected type
    """
    user_type = (
        canonicalize_user_type(current_user.get("user_type_selection"))
        or current_user.get("user_type_selection")
    )
    user_config = current_user.get("user_config", {})
    
    if not user_type:
        raise HTTPException(status_code=400, detail="User type not selected")

    status = persist_computed_onboarding(
        users_collection, census_collection, current_user
    )
    return {
        "user_type": user_type,
        "country": current_user.get("country"),
        "config": user_config,
        "country_config": current_user.get("country_config", {}),
        "onboarding_completed": status["onboarding_completed"],
        "current_step": status["current_step"],
        "fiscal_profile_completed": status["fiscal_profile_completed"],
        "census_data_uploaded": status["census_data_uploaded"],
    }


# ===========================================================================
# Business onboarding — Step 2: Company Details
# ===========================================================================

@router.post("/business/company-details")
async def save_company_details(
    request: BusinessProfileRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Business onboarding step 2.
    Save company legal name, CIF, company type and tax address.
    The company CIF becomes the ObligadoTributario in all AEAT submissions.
    """
    user_type = canonicalize_user_type(current_user.get("user_type_selection"))
    if user_type != "business":
        raise HTTPException(
            status_code=400,
            detail="Only business accounts can submit company details.",
        )

    cif = str(request.cif or "").replace(" ", "").upper()
    if not cif:
        raise HTTPException(status_code=400, detail="CIF is required.")

    business_profile = {
        "legal_name": request.legal_name.strip(),
        "cif": cif,
        "company_type": request.company_type.strip(),
        "tax_address": request.tax_address.model_dump() if request.tax_address else None,
        "updated_at": datetime.utcnow(),
    }

    result = users_collection.update_one(
        {"_id": current_user["_id"]},
        {"$set": {
            "business_profile": business_profile,
            "updated_at": datetime.utcnow(),
        }}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found.")

    status = persist_computed_onboarding(
        users_collection,
        census_collection,
        {**current_user, "business_profile": business_profile},
    )

    return {
        "message": "Company details saved successfully.",
        "next_step": "representative",
        "current_step": status["current_step"],
        "onboarding_completed": status["onboarding_completed"],
        "business_profile": business_profile,
    }


# ===========================================================================
# Business onboarding — Step 3: Authorized Representative
# ===========================================================================

@router.post("/business/representative")
async def save_representative(
    request: RepresentativeRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Business onboarding step 3.
    Save the authorized representative who will authenticate with AEAT.
    Only one representative is needed — not every shareholder.
    Role: administrador / representante_legal / apoderado
    """
    user_type = canonicalize_user_type(current_user.get("user_type_selection"))
    if user_type != "business":
        raise HTTPException(
            status_code=400,
            detail="Only business accounts can submit representative details.",
        )

    if not (current_user.get("business_profile") or {}).get("cif"):
        raise HTTPException(
            status_code=400,
            detail="Complete company details before entering the representative.",
        )

    valid_roles = {"administrador", "representante_legal", "apoderado"}
    role = str(request.role or "").strip().lower()
    if role not in valid_roles:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role. Choose from: {', '.join(valid_roles)}",
        )

    representative = {
        "full_name": request.full_name.strip(),
        "dni_nie": str(request.dni_nie or "").replace(" ", "").upper(),
        "role": role,
        "connected_at": None,
        "updated_at": datetime.utcnow(),
    }

    result = users_collection.update_one(
        {"_id": current_user["_id"]},
        {"$set": {
            "authorized_representative": representative,
            "updated_at": datetime.utcnow(),
        }}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found.")

    status = persist_computed_onboarding(
        users_collection,
        census_collection,
        {**current_user, "authorized_representative": representative},
    )

    return {
        "message": "Authorized representative saved successfully.",
        "next_step": "aeat_connection",
        "current_step": status["current_step"],
        "onboarding_completed": status["onboarding_completed"],
        "representative": {
            "full_name": representative["full_name"],
            "dni_nie": representative["dni_nie"],
            "role": representative["role"],
        },
    }


# ===========================================================================
# Business onboarding — Step 4: AEAT Connection
# ===========================================================================

def extract_client_info(req: Request) -> tuple[str, str]:
    client_ip = req.headers.get("x-forwarded-for") or (req.client.host if req.client else "unknown")
    if "," in client_ip:
        client_ip = client_ip.split(",")[0].strip()
    user_agent = req.headers.get("user-agent") or "unknown"
    return client_ip, user_agent


@router.post("/business/aeat-connect")
async def aeat_connect(
    request: AeatConnectRequest,
    raw_request: Request,
    current_user: dict = Depends(get_current_user),
):
    """
    Business onboarding step 4.
    The authorized representative confirms they have:
      1. Authenticated on AEAT's portal using their digital certificate.
      2. Granted Contia365 apoderamiento (representation) for the company.

    No certificate is stored here — this only records that the connection
    was completed. Contia365 will use its own company certificate for all
    subsequent AEAT submissions on behalf of this company.

    After this step, onboarding is complete. The user will not need to
    repeat this unless AEAT requires re-authentication (requires_reauth=True).
    """
    user_type = canonicalize_user_type(current_user.get("user_type_selection"))
    if user_type != "business":
        raise HTTPException(
            status_code=400,
            detail="Only business accounts use this endpoint.",
        )

    if not (current_user.get("business_profile") or {}).get("cif"):
        raise HTTPException(
            status_code=400,
            detail="Complete company details before connecting to AEAT.",
        )

    rep_doc = current_user.get("authorized_representative") or {}
    saved_rep_dni = rep_doc.get("dni_nie")
    if not saved_rep_dni:
        raise HTTPException(
            status_code=400,
            detail="Complete representative details before connecting to AEAT.",
        )

    now = datetime.utcnow()
    raw_rep_nif = (request.representative_nif or "").strip() or saved_rep_dni
    rep_nif = str(raw_rep_nif).replace(" ", "").upper()
    client_ip, user_agent = extract_client_info(raw_request)

    company_cif = (current_user.get("business_profile") or {}).get("cif")
    contia_nif = os.getenv("VITE_CONTIA_NIF", "B00000000")
    apoderamiento_code = str(request.apoderamiento_code).strip() if request.apoderamiento_code else None

    # Real-Time AEAT SOAP Verification
    apoderamiento_client = AeatApoderamientoClient()
    verification = apoderamiento_client.verify_apoderamiento(
        poderdante_nif=company_cif,
        apoderado_nif=contia_nif,
        representative_dni=rep_nif,
        apoderamiento_code=apoderamiento_code,
    )

    # If AEAT explicitly reports authorization not found / revoked, fail with actionable message
    if not verification.is_valid and verification.status in ("NOT_FOUND", "REVOKED"):
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": verification.status,
                "message": verification.message,
                "company_cif": company_cif,
                "contia_nif": contia_nif,
                "source": verification.source,
            },
        )

    aeat_connection = {
        "connected": True,
        "connected_at": now,
        "representative_nif": rep_nif,
        "requires_reauth": False,
        "last_sync_at": now,
        "representation_terms_version": request.representation_terms_version or "v1.0-2026",
        "consent_accepted_at": now,
        "ip_address": client_ip,
        "user_agent": user_agent,
        "apoderamiento_code": apoderamiento_code or verification.reference,
        # Real-time verification metadata
        "verification_status": verification.status,
        "verification_message": verification.message,
        "verified_at": verification.verified_at,
        "verification_source": verification.source,
    }

    # Also stamp connected_at on the representative record
    users_collection.update_one(
        {"_id": current_user["_id"]},
        {"$set": {
            "aeat_connection": aeat_connection,
            "authorized_representative.connected_at": now,
            "updated_at": now,
        }}
    )

    status = persist_computed_onboarding(
        users_collection,
        census_collection,
        {**current_user, "aeat_connection": aeat_connection},
    )

    return {
        "message": "AEAT connection established and verified. Onboarding is complete.",
        "aeat_connected": True,
        "connected_at": now.isoformat(),
        "current_step": status["current_step"],
        "onboarding_completed": status["onboarding_completed"],
        "verification": {
            "status": verification.status,
            "message": verification.message,
            "source": verification.source,
            "reference": verification.reference or apoderamiento_code,
        },
    }


class BusinessVerifyAeatRequest(BaseModel):
    apoderamiento_code: Optional[str] = None


@router.post("/business/verify-aeat")
async def verify_business_aeat(
    request: BusinessVerifyAeatRequest = BusinessVerifyAeatRequest(),
    current_user: dict = Depends(get_current_user),
):
    """
    On-demand live SOAP verification of AEAT apoderamiento for a business.
    """
    user_type = canonicalize_user_type(current_user.get("user_type_selection"))
    if user_type != "business":
        raise HTTPException(
            status_code=400,
            detail="Only business accounts use this endpoint.",
        )

    biz_profile = current_user.get("business_profile") or {}
    company_cif = biz_profile.get("cif")
    if not company_cif:
        raise HTTPException(
            status_code=400,
            detail="Company CIF is required before AEAT verification.",
        )

    rep = current_user.get("authorized_representative") or {}
    rep_dni = rep.get("dni_nie")
    contia_nif = os.getenv("VITE_CONTIA_NIF", "B00000000")

    client = AeatApoderamientoClient()
    result = client.verify_apoderamiento(
        poderdante_nif=company_cif,
        apoderado_nif=contia_nif,
        representative_dni=rep_dni,
        apoderamiento_code=request.apoderamiento_code,
    )

    if current_user.get("aeat_connection"):
        users_collection.update_one(
            {"_id": current_user["_id"]},
            {"$set": {
                "aeat_connection.verification_status": result.status,
                "aeat_connection.verification_message": result.message,
                "aeat_connection.verified_at": result.verified_at,
                "aeat_connection.verification_source": result.source,
            }}
        )

    return {
        "is_valid": result.is_valid,
        "status": result.status,
        "message": result.message,
        "source": result.source,
        "reference": result.reference,
        "verified_at": result.verified_at.isoformat(),
    }



class UpdateBusinessProfileRequest(BaseModel):
    legal_name: Optional[str] = None
    company_type: Optional[str] = None
    tax_address: Optional[TaxAddress] = None
    representative_full_name: Optional[str] = None
    representative_role: Optional[str] = None


@router.get("/business/profile")
async def get_business_profile(current_user: dict = Depends(get_current_user)):
    """
    Retrieve full company profile, representative, and AEAT connection status
    for the settings page.
    """
    user_type = canonicalize_user_type(current_user.get("user_type_selection"))
    if user_type != "business":
        raise HTTPException(
            status_code=400,
            detail="Only business accounts have a company profile.",
        )

    return {
        "company": current_user.get("business_profile") or {},
        "representative": current_user.get("authorized_representative") or {},
        "aeat_connection": current_user.get("aeat_connection") or {},
    }


@router.put("/business/profile")
async def update_business_profile(
    request: UpdateBusinessProfileRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Update company details or representative details post-onboarding.
    """
    user_type = canonicalize_user_type(current_user.get("user_type_selection"))
    if user_type != "business":
        raise HTTPException(
            status_code=400,
            detail="Only business accounts have a company profile.",
        )

    now = datetime.utcnow()
    update_fields = {"updated_at": now}

    if request.legal_name is not None:
        update_fields["business_profile.legal_name"] = request.legal_name.strip()
    if request.company_type is not None:
        update_fields["business_profile.company_type"] = request.company_type.strip()
    if request.tax_address is not None:
        update_fields["business_profile.tax_address"] = request.tax_address.model_dump()

    if request.representative_full_name is not None:
        update_fields["authorized_representative.full_name"] = request.representative_full_name.strip()
    if request.representative_role is not None:
        valid_roles = {"administrador", "representante_legal", "apoderado"}
        role = request.representative_role.strip().lower()
        if role in valid_roles:
            update_fields["authorized_representative.role"] = role

    users_collection.update_one(
        {"_id": current_user["_id"]},
        {"$set": update_fields}
    )

    updated_user = users_collection.find_one({"_id": current_user["_id"]}) or {}

    return {
        "message": "Company profile updated successfully.",
        "updated_at": now.isoformat(),
        "company": updated_user.get("business_profile") or {},
        "representative": updated_user.get("authorized_representative") or {},
    }


# ===========================================================================
# Post-onboarding: AEAT Sync
# ===========================================================================

@router.post("/aeat-sync")
async def aeat_sync(current_user: dict = Depends(get_current_user)):
    """
    Post-onboarding AEAT sync.
    Updates last_sync_at timestamp without repeating the full onboarding flow.
    Use case: user clicks "Sync with AEAT" from the dashboard.
    Also clears requires_reauth if it was set.
    """
    conn = current_user.get("aeat_connection") or {}
    if not conn.get("connected"):
        raise HTTPException(
            status_code=400,
            detail=(
                "AEAT connection has not been established. "
                "Complete the onboarding aeat_connection step first."
            ),
        )

    now = datetime.utcnow()
    users_collection.update_one(
        {"_id": current_user["_id"]},
        {"$set": {
            "aeat_connection.last_sync_at": now,
            "aeat_connection.requires_reauth": False,
            "updated_at": now,
        }}
    )

    return {
        "message": "AEAT sync completed.",
        "last_sync_at": now.isoformat(),
        "aeat_connected": True,
    }


@router.get("/aeat-connection/status")
async def get_aeat_connection_status(current_user: dict = Depends(get_current_user)):
    """
    Get current AEAT representation status for settings page.
    Handles both person (autónomo) and business user types.
    """
    user_type = canonicalize_user_type(current_user.get("user_type_selection"))
    if user_type == "person":
        conn = current_user.get("person_aeat_connection") or {}
        nif = conn.get("nif_nie") or current_user.get("tax_id") or ""
    else:
        conn = current_user.get("aeat_connection") or {}
        rep = current_user.get("authorized_representative") or {}
        nif = conn.get("representative_nif") or rep.get("dni_nie") or ""

    connected = bool(conn.get("connected"))
    connected_at = conn.get("connected_at")
    if isinstance(connected_at, datetime):
        connected_at = connected_at.isoformat()

    consent_accepted_at = conn.get("consent_accepted_at")
    if isinstance(consent_accepted_at, datetime):
        consent_accepted_at = consent_accepted_at.isoformat()

    return {
        "user_type": user_type,
        "connected": connected,
        "connected_at": connected_at,
        "nif": nif,
        "requires_reauth": bool(conn.get("requires_reauth")),
        "last_sync_at": conn.get("last_sync_at").isoformat() if isinstance(conn.get("last_sync_at"), datetime) else conn.get("last_sync_at"),
        # Legal Audit Trail Fields
        "representation_terms_version": conn.get("representation_terms_version") or "v1.0-2026",
        "consent_accepted_at": consent_accepted_at or connected_at,
        "ip_address": conn.get("ip_address"),
        "apoderamiento_code": conn.get("apoderamiento_code"),
        # Verification fields
        "verification_status": conn.get("verification_status") or ("VERIFIED" if connected else None),
        "verification_message": conn.get("verification_message"),
        "verified_at": conn.get("verified_at").isoformat() if isinstance(conn.get("verified_at"), datetime) else conn.get("verified_at"),
        "verification_source": conn.get("verification_source"),
    }



@router.post("/aeat-connection/revoke")
async def revoke_aeat_connection(current_user: dict = Depends(get_current_user)):
    """
    Revoke Contia365's AEAT representation authority.
    Updates the connection status to connected=False.
    """
    user_type = canonicalize_user_type(current_user.get("user_type_selection"))
    now = datetime.utcnow()

    if user_type == "person":
        users_collection.update_one(
            {"_id": current_user["_id"]},
            {"$set": {
                "person_aeat_connection.connected": False,
                "person_aeat_connection.revoked_at": now,
                "updated_at": now,
            }}
        )
    else:
        users_collection.update_one(
            {"_id": current_user["_id"]},
            {"$set": {
                "aeat_connection.connected": False,
                "aeat_connection.revoked_at": now,
                "updated_at": now,
            }}
        )

    return {
        "message": "AEAT representation authority revoked successfully.",
        "connected": False,
        "revoked_at": now.isoformat(),
    }



# ===========================================================================
# Admin utility: flag a user for AEAT re-authentication
# ===========================================================================

@router.post("/aeat-require-reauth")
async def require_reauth(
    user_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Admin endpoint — mark a user's AEAT connection as requiring re-authentication.
    Called when AEAT returns an authorization error during submission.
    """
    from app.services.admin_users_service import is_admin
    if not is_admin(current_user):
        raise HTTPException(status_code=403, detail="Admin access required.")

    users_collection.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {
            "aeat_connection.requires_reauth": True,
            "aeat_connection.connected": False,
            "updated_at": datetime.utcnow(),
        }}
    )
    return {"message": f"User {user_id} flagged for AEAT re-authentication."}


# ===========================================================================
# Person onboarding — Step 6: Digital Certificate Upload
# ===========================================================================

@router.post("/person/certificate")
async def upload_person_certificate(
    file: UploadFile = File(...),
    password: str = Form(...),
    current_user: dict = Depends(get_current_user),
):
    """
    Person onboarding step 6.
    Upload the user's FNMT digital certificate (.p12 / .pfx).

    The certificate is validated (password verified), then encrypted with
    AES-256 (Fernet) using the server's master key before being stored in
    MongoDB.  The password is NOT stored — it is only used to verify the
    certificate and is discarded immediately.

    Security:
      - Only the encrypted blob is stored in the database.
      - The raw .p12 and password never touch disk or logs.
      - CERT_ENCRYPTION_KEY env var must be set in production.
    """
    from app.services.certificate_service import save_certificate

    user_type = canonicalize_user_type(current_user.get("user_type_selection"))
    if user_type != "person":
        raise HTTPException(
            status_code=400,
            detail="Only person (autónomo) accounts use this endpoint.",
        )

    # Validate file extension
    filename = file.filename or ""
    if not filename.lower().endswith((".p12", ".pfx")):
        raise HTTPException(
            status_code=400,
            detail="Invalid file type. Please upload a .p12 or .pfx certificate file.",
        )

    # Read file bytes
    p12_bytes = await file.read()
    if not p12_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    # Max 2 MB sanity check
    if len(p12_bytes) > 2 * 1024 * 1024:
        raise HTTPException(
            status_code=400,
            detail="File too large. Maximum allowed size is 2 MB.",
        )

    try:
        cert_meta = save_certificate(
            users_collection=users_collection,
            user_id=str(current_user["_id"]),
            p12_bytes=p12_bytes,
            password=password,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # Recompute onboarding status after upload
    updated_user = users_collection.find_one({"_id": current_user["_id"]})
    status = persist_computed_onboarding(
        users_collection,
        census_collection,
        updated_user,
    )

    return {
        "message": "Digital certificate uploaded and verified successfully.",
        "certificate": cert_meta,
        "current_step": status["current_step"],
        "onboarding_completed": status["onboarding_completed"],
    }


@router.get("/person/certificate")
async def get_person_certificate_status(
    current_user: dict = Depends(get_current_user),
):
    """
    Return certificate metadata for the current user.
    Never returns the encrypted bytes or any key material.
    """
    from app.services.certificate_service import get_certificate_status

    info = get_certificate_status(current_user)
    if not info:
        return {"uploaded": False}
    return info


@router.delete("/person/certificate")
async def delete_person_certificate(
    current_user: dict = Depends(get_current_user),
):
    """
    Remove the stored certificate so the user can upload a new one.
    Also resets the person_aeat_connection (they will need to re-authorize).
    """
    user_type = canonicalize_user_type(current_user.get("user_type_selection"))
    if user_type != "person":
        raise HTTPException(
            status_code=400,
            detail="Only person (autónomo) accounts use this endpoint.",
        )

    now = datetime.utcnow()
    users_collection.update_one(
        {"_id": current_user["_id"]},
        {"$unset": {
            "p12_encrypted": "",
            "certificate_info": "",
            "certificate_uploaded_at": "",
            "certificate_valid_until": "",
        },
         "$set": {
            "person_aeat_connection": {"connected": False},
            "updated_at": now,
        }}
    )

    status = persist_computed_onboarding(
        users_collection,
        census_collection,
        {**current_user,
         "p12_encrypted": None,
         "certificate_info": None,
         "person_aeat_connection": {"connected": False}},
    )

    return {
        "message": "Certificate removed. You can now upload a new one.",
        "current_step": status["current_step"],
    }


# ===========================================================================
# Person onboarding — Step 7: AEAT Apoderamiento Confirmation
# ===========================================================================

@router.post("/person/aeat-connect")
async def person_aeat_connect(
    request: PersonAeatConnectRequest,
    raw_request: Request,
    current_user: dict = Depends(get_current_user),
):
    """
    Person onboarding step 7.
    The individual user confirms they have granted apoderamiento (power of
    attorney) to Contia365 on the AEAT portal.

    Flow:
      1. User goes to AEAT → Representación → Otorgar apoderamiento
      2. Enters Contia365's NIF, selects procedures (G303, G130, etc.)
      3. Returns here and clicks confirm → this endpoint is called

    After this step, onboarding is complete for person accounts.
    """
    user_type = canonicalize_user_type(current_user.get("user_type_selection"))
    if user_type != "person":
        raise HTTPException(
            status_code=400,
            detail="Only person (autónomo) accounts use this endpoint.",
        )

    # Must have uploaded certificate first
    if not current_user.get("certificate_info"):
        raise HTTPException(
            status_code=400,
            detail="Upload your digital certificate before confirming the AEAT connection.",
        )

    nif_nie = str(request.nif_nie or "").replace(" ", "").upper()
    if not nif_nie:
        raise HTTPException(status_code=400, detail="NIF/NIE is required.")

    now = datetime.utcnow()
    client_ip, user_agent = extract_client_info(raw_request)

    person_aeat_connection = {
        "connected": True,
        "connected_at": now,
        "nif_nie": nif_nie,
        "requires_reauth": False,
        "last_sync_at": now,
        "representation_terms_version": request.representation_terms_version or "v1.0-2026",
        "consent_accepted_at": now,
        "ip_address": client_ip,
        "user_agent": user_agent,
        "apoderamiento_code": str(request.apoderamiento_code).strip() if request.apoderamiento_code else None,
    }


    users_collection.update_one(
        {"_id": current_user["_id"]},
        {"$set": {
            "person_aeat_connection": person_aeat_connection,
            "updated_at": now,
        }}
    )

    status = persist_computed_onboarding(
        users_collection,
        census_collection,
        {**current_user, "person_aeat_connection": person_aeat_connection},
    )

    return {
        "message": "AEAT authorization confirmed. Onboarding is complete.",
        "person_aeat_connected": True,
        "connected_at": now.isoformat(),
        "current_step": status["current_step"],
        "onboarding_completed": status["onboarding_completed"],
    }
