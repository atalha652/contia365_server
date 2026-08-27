"""
Onboarding Models for Contia365
Handles user type selection and onboarding flow
"""

from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field, field_validator
from bson import ObjectId

from app.services.user_type_vocab import canonicalize_user_type


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
    PERSON = "person"
    BUSINESS = "business"
    ADVISOR = "advisor"

    @classmethod
    def _missing_(cls, value):
        mapped = canonicalize_user_type(value)
        if mapped is None:
            return None
        return cls(mapped)


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
SELECTABLE_USER_TYPES = (UserTypeSelection.PERSON, UserTypeSelection.BUSINESS)

USER_TYPE_CATALOG = {
    UserTypeSelection.PERSON: UserTypeInfo(
        id="person",
        name="Person",
        subtitle="Autónomo",
        description="Individual professional managing their own invoices and taxes.",
    ),
    UserTypeSelection.BUSINESS: UserTypeInfo(
        id="business",
        name="Business",
        subtitle="Empresa",
        description="Company with employees, accounting, and invoicing needs.",
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
    tax_available: bool = True
    status: str = "Available"


class OnboardingRequest(BaseModel):
    """Request model for user type selection"""
    user_type: UserTypeSelection
    additional_info: Optional[Dict[str, Any]] = {}

    @field_validator("user_type", mode="before")
    @classmethod
    def coerce_legacy_user_type(cls, value):
        if isinstance(value, UserTypeSelection):
            return value
        mapped = canonicalize_user_type(value)
        if mapped is None:
            raise ValueError("Invalid user type. Choose person or business.")
        return mapped


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
    role: Optional[str] = None
    fiscal_profile_completed: bool = False
    census_data_uploaded: bool = False
    current_step: str
    completed_at: Optional[datetime] = None
    next_action: Optional[str] = None


# Configuration for each user type
USER_TYPE_CONFIGS = {
    UserTypeSelection.PERSON: {
        "dashboard_layout": "person",
        "default_features": ["invoicing", "expenses", "tax_reports"],
        "chart_of_accounts": "person_coa",
        "tax_regime": "autonomo"
    },
    UserTypeSelection.BUSINESS: {
        "dashboard_layout": "business",
        "default_features": ["invoicing", "expenses", "payroll", "tax_reports", "bank_reconciliation"],
        "chart_of_accounts": "business_coa",
        "tax_regime": "business"
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
        "tax_available": True,
        "status": "Available",
    },
    CountrySelection.ITALY: {
        "id": "IT",
        "name": "Italy",
        "subtitle": "Italia",
        "currency": "EUR",
        "tax_authority": "AdE",
        "invoice_format": "fattura_pa",
        "tax_available": False,
        "status": "Coming soon",
    },
}