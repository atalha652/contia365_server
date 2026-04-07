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
    OnboardingResponse, OnboardingStatus, USER_TYPE_CONFIGS
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
    Get available user types for onboarding selection
    Returns list of user types with descriptions and features
    """
    user_types = [
        UserTypeInfo(
            id="freelancer",
            name="Freelancer",
            subtitle="Autónomo",
            description="Individual freelancer or self-employed professional managing their own invoices and taxes.",
        ),
        UserTypeInfo(
            id="company", 
            name="Company",
            subtitle="Empresa",
            description="Business entity with employees and complex accounting and invoicing needs.",
        ),
        UserTypeInfo(
            id="advisor",
            name="Advisor", 
            subtitle="Asesor",
            description="Tax advisor or accountant managing finances and reports for multiple clients.",
        )
    ]
    
    return user_types


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
    user_type_selected = current_user.get("user_type_selection")
    completed_at = current_user.get("onboarding_completed_at")
    
    # Determine current step and next action
    if onboarding_completed:
        current_step = "completed"
        next_action = None
    else:
        current_step = "user_type_selection"
        next_action = "Select your user type to continue"
    
    return OnboardingStatus(
        user_id=str(user_id),
        onboarding_completed=onboarding_completed,
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
        "config": user_config,
        "onboarding_completed": current_user.get("onboarding_completed", False)
    }