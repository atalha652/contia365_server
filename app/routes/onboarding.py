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

# Load environment variables
load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")

# Database connection
client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = client[DB_NAME]
users_collection = db["users"]

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

        # Get configuration for selected user type
        user_config = USER_TYPE_CONFIGS.get(selected_type, {})
        
        # Update user document in database
        update_data = {
            "user_type_selection": selected_type.value,
            "onboarding_completed": True,
            "onboarding_completed_at": datetime.utcnow(),
            "onboarding_step": "completed",
            "updated_at": datetime.utcnow(),
            "user_config": user_config
        }
        
        # If user has company name, update organization type based on selection
        if selected_type == UserTypeSelection.COMPANY and current_user.get("organization_info"):
            update_data["organization_info.type"] = "company"
            update_data["type"] = "organization"
        elif selected_type == UserTypeSelection.FREELANCER:
            update_data["type"] = "individual"
        elif selected_type == UserTypeSelection.ADVISOR:
            update_data["type"] = "organization"  # Advisors are treated as organizations
            
        result = users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": update_data}
        )
        
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="User not found")
            
        # Determine redirect based on user type
        redirect_mapping = {
            UserTypeSelection.FREELANCER: "/dashboard/freelancer",
            UserTypeSelection.COMPANY: "/dashboard/company", 
            UserTypeSelection.ADVISOR: "/dashboard/advisor"
        }
        
        return OnboardingResponse(
            message=f"User type '{selected_type.value}' selected successfully",
            user_type=selected_type.value,
            onboarding_completed=True,
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update user type: {str(e)}")


@router.get("/status", response_model=OnboardingStatus)
async def get_onboarding_status(current_user: dict = Depends(get_current_user)):
    """
    Check current user's onboarding status
    Returns whether onboarding is completed and current step
    """
    user_id = current_user["_id"]
    onboarding_completed = current_user.get("onboarding_completed", False)
    country_selected = current_user.get("country")
    user_type_selected = current_user.get("user_type_selection")
    completed_at = current_user.get("onboarding_completed_at")

    if onboarding_completed:
        current_step = "completed"
        next_action = None
    elif not country_selected:
        current_step = "country_selection"
        next_action = "Select your country to continue"
    else:
        current_step = "user_type_selection"
        next_action = "Select your user type to continue"

    return OnboardingStatus(
        user_id=str(user_id),
        onboarding_completed=onboarding_completed,
        country_selected=country_selected,
        user_type_selected=user_type_selected,
        current_step=current_step,
        completed_at=completed_at,
        next_action=next_action
    )


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
            "onboarding_completed": True,
            "onboarding_completed_at": datetime.utcnow(),
            "onboarding_step": "completed",
            "updated_at": datetime.utcnow(),
            "user_config": default_config,
            "onboarding_skipped": True
        }
        
        result = users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": update_data}
        )
        
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="User not found")
            
        return {
            "message": "Onboarding skipped successfully",
            "user_type": UserTypeSelection.FREELANCER.value,
            "redirect_to": "/dashboard",
            "note": "Default freelancer configuration applied. You can change this later in settings."
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
        
    return {
        "user_type": user_type,
        "country": current_user.get("country"),
        "config": user_config,
        "country_config": current_user.get("country_config", {}),
        "onboarding_completed": current_user.get("onboarding_completed", False)
    }