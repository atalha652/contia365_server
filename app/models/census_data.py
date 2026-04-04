"""
Census Data Models for Contia365
Covers: Certificado de Situación Censal
"""

from datetime import date, datetime
from typing import Optional, List
from pydantic import BaseModel, Field, ConfigDict
from bson import ObjectId


# ===== Shared =====

class FiscalAddress(BaseModel):
    address_line: Optional[str] = None
    postal_code: Optional[str] = None
    city: Optional[str] = None
    province: Optional[str] = None


# ===== Taxpayer Identity =====

class TaxpayerIdentity(BaseModel):
    nif_nie: Optional[str] = None
    full_name: Optional[str] = None
    fiscal_address: Optional[FiscalAddress] = None
    resident_status: Optional[bool] = None


# ===== Professional Registration =====

class EconomicActivity(BaseModel):
    section: Optional[str] = None           # Empresarial / Profesional
    code: Optional[str] = None              # IAE code e.g. "967.2"
    description: Optional[str] = None
    start_date: Optional[date] = None
    activity_type_code: Optional[str] = None  # e.g. "A03", "A05"


class ProfessionalRegistration(BaseModel):
    vat_regime: Optional[str] = None           # e.g. "General"
    irpf_method: Optional[str] = None          # e.g. "Estimación directa simplificada"
    economic_activities: List[EconomicActivity] = []


# ===== Periodic Tax Obligations =====

class PeriodicTaxObligation(BaseModel):
    modelo: Optional[str] = None              # e.g. "303"
    description: Optional[str] = None
    periodicity: Optional[str] = None         # e.g. "TRIMESTRAL"


# ===== Income & Expenses Summary =====

class IncomeAndExpensesSummary(BaseModel):
    total_revenue_period: Optional[float] = None
    total_deductible_expenses: Optional[float] = None
    net_profit: Optional[float] = None
    accumulated_withholdings_received: Optional[float] = None


# ===== Tax Calculation =====

class TaxCalculation(BaseModel):
    taxable_base: Optional[float] = None
    tax_quota: Optional[float] = None
    final_tax: Optional[float] = None
    withholdings_paid: Optional[float] = None
    result_amount: Optional[float] = None
    result_type: Optional[str] = None        # "Refund" or "Payment"


# ===== Household Data =====

class HouseholdMember(BaseModel):
    name: Optional[str] = None
    age: Optional[int] = None
    occupation: Optional[str] = None
    annual_income: Optional[float] = None


class HouseholdData(BaseModel):
    members: List[HouseholdMember] = []
    total_household_income: Optional[float] = None


# ===== Document Metadata =====

class DocumentMetadata(BaseModel):
    document_type: Optional[str] = None
    official_name: Optional[str] = None
    issue_date: Optional[date] = None
    csv_code: Optional[str] = None
    aeat_reference: Optional[str] = None


# ===== Platform Verification =====

class PlatformVerification(BaseModel):
    verification_status: str = Field(default="PENDING")
    verified_at: Optional[datetime] = None
    needs_renewal_at: Optional[date] = None


# ===== Main Collection Model =====

class CensusDataBase(BaseModel):
    document_metadata: DocumentMetadata = Field(default_factory=DocumentMetadata)
    taxpayer_identity: TaxpayerIdentity = Field(default_factory=TaxpayerIdentity)
    professional_registration: Optional[ProfessionalRegistration] = None
    periodic_tax_obligations: List[PeriodicTaxObligation] = []
    income_and_expenses_summary: Optional[IncomeAndExpensesSummary] = None
    tax_calculation: Optional[TaxCalculation] = None
    household_data: Optional[HouseholdData] = None
    platform_verification: PlatformVerification = Field(default_factory=PlatformVerification)

    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
        json_encoders={ObjectId: str, datetime: lambda v: v.isoformat()}
    )


class CensusDataCreate(CensusDataBase):
    user_id: str
    organization_id: str


class CensusDataUpdate(BaseModel):
    document_metadata: Optional[DocumentMetadata] = None
    taxpayer_identity: Optional[TaxpayerIdentity] = None
    professional_registration: Optional[ProfessionalRegistration] = None
    periodic_tax_obligations: Optional[List[PeriodicTaxObligation]] = None
    income_and_expenses_summary: Optional[IncomeAndExpensesSummary] = None
    tax_calculation: Optional[TaxCalculation] = None
    household_data: Optional[HouseholdData] = None
    platform_verification: Optional[PlatformVerification] = None

    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
        json_encoders={ObjectId: str}
    )


class CensusDataResponse(CensusDataBase):
    id: str = Field(alias="_id")
    user_id: str
    organization_id: str
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
        json_encoders={ObjectId: str, datetime: lambda v: v.isoformat()}
    )
