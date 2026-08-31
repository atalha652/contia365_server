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
    # Person (autónomo) path
    FISCAL_PROFILE = "fiscal_profile"
    CERTIFICATE_UPLOAD = "certificate_upload"   # Person: upload .p12 digital cert
    PERSON_AEAT_CONNECTION = "person_aeat_connection"  # Person: confirm apoderamiento
    # Business (empresa) path
    COMPANY_DETAILS = "company_details"
    REPRESENTATIVE = "representative"
    AEAT_CONNECTION = "aeat_connection"
    # Both paths end here
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


# ---------------------------------------------------------------------------
# Business onboarding data models
# ---------------------------------------------------------------------------

class TaxAddress(BaseModel):
    """Company tax address (domicilio fiscal)"""
    address_line: Optional[str] = None
    postal_code: Optional[str] = None
    city: Optional[str] = None
    province: Optional[str] = None


class BusinessProfile(BaseModel):
    """
    Company details collected during business onboarding step 2.
    The company (not the logged-in user) is the taxpayer.
    CIF goes into the SOAP ObligadoTributario field on AEAT submissions.
    """
    legal_name: Optional[str] = None        # Razón social
    cif: Optional[str] = None               # Company NIF e.g. B12345678
    company_type: Optional[str] = None      # S.L., S.A., S.C.P., C.B., etc.
    tax_address: Optional[TaxAddress] = None


class AuthorizedRepresentative(BaseModel):
    """
    The person authorized to act for the company before AEAT.
    Only one representative is needed — not every shareholder.
    Role options: administrador / representante_legal / apoderado
    """
    full_name: Optional[str] = None
    dni_nie: Optional[str] = None           # Representative's personal DNI/NIE
    role: Optional[str] = None              # administrador / representante_legal / apoderado
    connected_at: Optional[datetime] = None # When they authenticated to AEAT


class AeatConnection(BaseModel):
    """
    Tracks whether AEAT connection has been established for this account.
    Set once during onboarding. Not repeated unless requires_reauth = True.
    For business: the representative authenticates and grants apoderamiento.
    For person: the autónomo grants apoderamiento directly.
    """
    connected: bool = False
    connected_at: Optional[datetime] = None
    representative_nif: Optional[str] = None  # DNI/NIE of who completed the connection
    requires_reauth: bool = False
    last_sync_at: Optional[datetime] = None
    # Legal Audit Trail Fields
    representation_terms_version: Optional[str] = "v1.0-2026"
    consent_accepted_at: Optional[datetime] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    apoderamiento_code: Optional[str] = None


# ---------------------------------------------------------------------------
# Request models for business onboarding endpoints
# ---------------------------------------------------------------------------

class BusinessProfileRequest(BaseModel):
    """Body for POST /onboarding/business/company-details"""
    legal_name: str = Field(..., description="Company legal name (razón social)")
    cif: str = Field(..., description="Company NIF/CIF e.g. B12345678")
    company_type: str = Field(..., description="S.L. / S.A. / S.C.P. / C.B. / etc.")
    tax_address: Optional[TaxAddress] = None


class RepresentativeRequest(BaseModel):
    """Body for POST /onboarding/business/representative"""
    full_name: str = Field(..., description="Full name of the authorized representative")
    dni_nie: str = Field(..., description="DNI or NIE of the representative")
    role: str = Field(
        ...,
        description="Role: administrador / representante_legal / apoderado"
    )


class AeatConnectRequest(BaseModel):
    """
    Body for POST /onboarding/business/aeat-connect.
    The representative confirms they have authenticated on AEAT's portal
    and granted apoderamiento to Contia365.
    No certificate is stored — this only records the connection event.
    """
    representative_nif: str = Field(
        ..., description="DNI/NIE of the representative who completed the AEAT connection"
    )
    representation_terms_version: Optional[str] = Field(
        "v1.0-2026", description="Version of representation terms agreed to by user"
    )
    apoderamiento_code: Optional[str] = Field(
        None, description="Optional AEAT apoderamiento receipt or trámite reference code"
    )


# ---------------------------------------------------------------------------
# Person onboarding — certificate upload & AEAT connection
# ---------------------------------------------------------------------------

class PersonAeatConnectRequest(BaseModel):
    """
    Body for POST /onboarding/person/aeat-connect.
    The individual user confirms they have granted apoderamiento to Contia365
    on AEAT's portal using their digital certificate.
    """
    nif_nie: str = Field(
        ..., description="User's NIF or NIE — used to confirm identity"
    )
    representation_terms_version: Optional[str] = Field(
        "v1.0-2026", description="Version of representation terms agreed to by user"
    )
    apoderamiento_code: Optional[str] = Field(
        None, description="Optional AEAT apoderamiento receipt or trámite reference code"
    )



# ---------------------------------------------------------------------------
# Standard onboarding request/response models
# ---------------------------------------------------------------------------

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
    """
    Single onboarding-status payload.
    login response and GET /status must return the same shape.
    """
    user_id: str
    onboarding_completed: bool
    country_selected: Optional[str] = None
    user_type_selected: Optional[str] = None
    role: Optional[str] = None
    # Person path flags
    fiscal_profile_completed: bool = False
    census_data_uploaded: bool = False
    certificate_uploaded: bool = False          # Person: .p12 uploaded
    person_aeat_connected: bool = False         # Person: apoderamiento granted
    # Business path flags
    business_profile_completed: bool = False
    representative_completed: bool = False
    aeat_connected: bool = False
    # Common
    current_step: str
    completed_at: Optional[datetime] = None
    next_action: Optional[str] = None


# ---------------------------------------------------------------------------
# Configuration maps
# ---------------------------------------------------------------------------

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