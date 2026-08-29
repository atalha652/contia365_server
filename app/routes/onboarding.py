"""
Onboarding Routes for Contia365
Handles user type selection and onboarding completion
"""

from fastapi import APIRouter, Depends, HTTPException
from pymongo import MongoClient
from bson import ObjectId
import os
from datetime import datetime
from typing import List, Dict, Any
import certifi
from dotenv import load_dotenv

from app.models.onboarding import (
    UserTypeSelection, UserTypeInfo, OnboardingRequest,
    OnboardingResponse, OnboardingStatus, USER_TYPE_CONFIGS,
    CountrySelection, CountryInfo, CountrySelectRequest,
    CountrySelectResponse, COUNTRY_CONFIGS,
    SELECTABLE_USER_TYPES, USER_TYPE_CATALOG,
    BusinessProfileRequest, RepresentativeRequest, AeatConnectRequest,
)
from app.routes.auth import get_current_user
from app.services.onboarding_status import persist_computed_onboarding
from app.services.user_type_vocab import ADVISOR, canonicalize_user_type

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

@router.post("/business/aeat-connect")
async def aeat_connect(
    request: AeatConnectRequest,
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

    if not (current_user.get("authorized_representative") or {}).get("dni_nie"):
        raise HTTPException(
            status_code=400,
            detail="Complete representative details before connecting to AEAT.",
        )

    now = datetime.utcnow()
    rep_nif = str(request.representative_nif or "").replace(" ", "").upper()

    aeat_connection = {
        "connected": True,
        "connected_at": now,
        "representative_nif": rep_nif,
        "requires_reauth": False,
        "last_sync_at": now,
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
        "message": "AEAT connection established. Onboarding is complete.",
        "aeat_connected": True,
        "connected_at": now.isoformat(),
        "current_step": status["current_step"],
        "onboarding_completed": status["onboarding_completed"],
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
