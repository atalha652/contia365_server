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
)
from app.routes.auth import get_current_user
from app.services.onboarding_status import persist_computed_onboarding

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
        existing_type = current_user.get("user_type_selection")

        # New users can only pick types from GET /user-types.
        # Existing advisor accounts may keep (or re-save) advisor; do not map to white_label.
        if selected_type not in SELECTABLE_USER_TYPES:
            if not (
                selected_type == UserTypeSelection.ADVISOR
                and existing_type == UserTypeSelection.ADVISOR.value
            ):
                raise HTTPException(
                    status_code=400,
                    detail="Invalid user type. Choose freelancer or company.",
                )

        user_config = USER_TYPE_CONFIGS.get(selected_type, {})

        update_data = {
            "user_type_selection": selected_type.value,
            "updated_at": datetime.utcnow(),
            "user_config": user_config,
        }

        if selected_type == UserTypeSelection.COMPANY and current_user.get("organization_info"):
            update_data["organization_info.type"] = "company"
            update_data["type"] = "organization"
        elif selected_type == UserTypeSelection.FREELANCER:
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
        
        # Set default configuration (freelancer)
        default_config = USER_TYPE_CONFIGS[UserTypeSelection.FREELANCER]
        
        update_data = {
            "user_type_selection": UserTypeSelection.FREELANCER.value,
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
            {**current_user, "user_type_selection": UserTypeSelection.FREELANCER.value},
        )

        return {
            "message": "Onboarding skipped successfully",
            "user_type": UserTypeSelection.FREELANCER.value,
            "onboarding_completed": status["onboarding_completed"],
            "current_step": status["current_step"],
            "fiscal_profile_completed": status["fiscal_profile_completed"],
            "note": "Default freelancer configuration applied. Spain users still need the fiscal profile.",
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to skip onboarding: {str(e)}")


@router.get("/config")
async def get_user_config(current_user: dict = Depends(get_current_user)):
    """
    Get user's current configuration based on their selected type
    """
    user_type = current_user.get("user_type_selection")
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