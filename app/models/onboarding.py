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
    """Stored user types. Advisor is legacy: not offered to new users."""
    FREELANCER = "freelancer"  # Autónomo
    COMPANY = "company"        # Business entity
    ADVISOR = "advisor"        # Legacy Asesor accounts only (not White Label)


class CountrySelection(str, Enum):
    """Supported operating countries (ISO 3166-1 alpha-2)."""
    SPAIN = "ES"
    ITALY = "IT"


class OnboardingStep(str, Enum):
    """Onboarding process steps"""
    COUNTRY_SELECTION = "country_selection"
    USER_TYPE_SELECTION = "user_type_selection"
    FISCAL_PROFILE = "fiscal_profile"
    COMPLETED = "completed"


class UserTypeInfo(BaseModel):
    """User type information for frontend display"""
    id: str
    name: str
    subtitle: str
    description: str


# Shown in GET /user-types. Advisor is kept on the enum for existing accounts.
SELECTABLE_USER_TYPES = (UserTypeSelection.FREELANCER, UserTypeSelection.COMPANY)

USER_TYPE_CATALOG = {
    UserTypeSelection.FREELANCER: UserTypeInfo(
        id="freelancer",
        name="Freelancer",
        subtitle="Autónomo",
        description="Individual freelancer or self-employed professional managing their own invoices and taxes.",
    ),
    UserTypeSelection.COMPANY: UserTypeInfo(
        id="company",
        name="Company",
        subtitle="Empresa",
        description="Business entity with employees and complex accounting and invoicing needs.",
    ),
    UserTypeSelection.ADVISOR: UserTypeInfo(
        id="advisor",
        name="Advisor",
        subtitle="Asesor",
        description="Tax advisor or accountant managing finances and reports for multiple clients.",
    ),
}


class CountryInfo(BaseModel):
    """Country option for frontend display"""
    id: str
    name: str
    subtitle: str
    currency: str
    tax_authority: str


class OnboardingRequest(BaseModel):
    """Request model for user type selection"""
    user_type: UserTypeSelection
    additional_info: Optional[Dict[str, Any]] = {}


class CountrySelectRequest(BaseModel):
    """Request model for country selection"""
    country: CountrySelection


class OnboardingResponse(BaseModel):
    """Response model for user type selection"""
    message: str
    user_type: str
    onboarding_completed: bool
    current_step: Optional[str] = None
    fiscal_profile_completed: bool = False


class CountrySelectResponse(BaseModel):
    """Response after saving country"""
    message: str
    country: str
    country_name: str
    next_step: str


class OnboardingStatus(BaseModel):
    """Single onboarding-status payload (login and GET /status must match)."""
    user_id: str
    onboarding_completed: bool
    country_selected: Optional[str] = None
    user_type_selected: Optional[str] = None
    fiscal_profile_completed: bool = False
    census_data_uploaded: bool = False
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

COUNTRY_CONFIGS = {
    CountrySelection.SPAIN: {
        "id": "ES",
        "name": "Spain",
        "subtitle": "España",
        "currency": "EUR",
        "tax_authority": "AEAT",
        "invoice_format": "facturae",
    },
    CountrySelection.ITALY: {
        "id": "IT",
        "name": "Italy",
        "subtitle": "Italia",
        "currency": "EUR",
        "tax_authority": "AdE",
        "invoice_format": "fattura_pa",
    },
}