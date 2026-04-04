"""
Onboarding Models for Contia365
Handles user type selection and onboarding flow
"""

from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field
from bson import ObjectId


class PyObjectId(str):
    """Custom ObjectId for MongoDB"""

    @classmethod
    def __get_validators__(cls):
        yield cls.validate

    @classmethod
    def validate(cls, v, field=None):
        if not ObjectId.is_valid(v):
            raise ValueError("Invalid ObjectId")
        return str(v)


class UserTypeSelection(str, Enum):
    """Available user types for onboarding"""
    FREELANCER = "freelancer"  # Autónomo
    COMPANY = "company"        # Business entity
    ADVISOR = "advisor"        # Tax/Financial advisor


class OnboardingStep(str, Enum):
    """Onboarding process steps"""
    USER_TYPE_SELECTION = "user_type_selection"
    COMPLETED = "completed"


class UserTypeInfo(BaseModel):
    """User type information for frontend display"""
    id: str
    name: str
    subtitle: str
    description: str


class OnboardingRequest(BaseModel):
    """Request model for user type selection"""
    user_type: UserTypeSelection
    additional_info: Optional[Dict[str, Any]] = {}


class OnboardingResponse(BaseModel):
    """Response model for onboarding completion"""
    message: str
    user_type: str
    onboarding_completed: bool


class OnboardingStatus(BaseModel):
    """Model for checking onboarding status"""
    user_id: str
    onboarding_completed: bool
    user_type_selected: Optional[str] = None
    current_step: str
    completed_at: Optional[datetime] = None
    next_action: Optional[str] = None


# Configuration for each user type
USER_TYPE_CONFIGS = {
    UserTypeSelection.FREELANCER: {
        "dashboard_layout": "freelancer",
        "default_features": ["invoicing", "expenses", "tax_reports"],
        "chart_of_accounts": "freelancer_coa",
        "tax_regime": "autonomo"
    },
    UserTypeSelection.COMPANY: {
        "dashboard_layout": "company", 
        "default_features": ["invoicing", "expenses", "payroll", "tax_reports", "bank_reconciliation"],
        "chart_of_accounts": "company_coa",
        "tax_regime": "company"
    },
    UserTypeSelection.ADVISOR: {
        "dashboard_layout": "advisor",
        "default_features": ["client_management", "multi_company", "tax_reports", "advisory_tools"],
        "chart_of_accounts": "advisor_coa", 
        "tax_regime": "advisor"
    }
}