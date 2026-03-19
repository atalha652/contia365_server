"""
Census Data Models for Contia365
Covers: Certificado de Situación Censal, Modelo 100 (IRPF), Census Tax Declaration
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


# ===== Personal / Taxpayer Info =====

class TaxpayerIdentity(BaseModel):
    nif_nie: Optional[str] = None
    full_name: Optional[str] = None
    fiscal_address: Optional[FiscalAddress] = None


# ===== Income Section (Modelo 100) =====

class IncomeData(BaseModel):
    gross_salary: Optional[float] = None          # Rendimientos del Trabajo
    withholdings: Optional[float] = None          # Retenciones


# ===== Deductions Section (Modelo 100) =====

class DeductionItem(BaseModel):
    concept: str
    amount: float


class DeductionsData(BaseModel):
    items: List[DeductionItem] = []
    total_deductions: Optional[float] = None


# ===== Tax Calculation (Modelo 100) =====

class TaxCalculation(BaseModel):
    taxable_base: Optional[float] = None          # Base Imponible
    tax_quota: Optional[float] = None             # Cuota Íntegra
    final_tax: Optional[float] = None             # Cuota Líquida
    withholdings_paid: Optional[float] = None
    result_amount: Optional[float] = None         # positive = to pay, negative = refund
    result_type: Optional[str] = None             # "Refund" or "Payment"


# ===== Household Members (Census Tax Declaration) =====

class HouseholdMember(BaseModel):
    name: Optional[str] = None
    age: Optional[int] = None
    occupation: Optional[str] = None
    annual_income: Optional[float] = None


# ===== Household Tax Summary (Census Tax Declaration) =====

class HouseholdTaxSummary(BaseModel):
    total_household_income: Optional[float] = None
    total_deductions: Optional[float] = None
    taxable_income: Optional[float] = None
    estimated_tax_liability: Optional[float] = None
    tax_paid: Optional[float] = None
    balance_due: Optional[float] = None


# ===== Document Metadata =====

class DocumentMetadata(BaseModel):
    document_type: Optional[str] = None          # e.g. "Modelo 100", "Census Tax Declaration"
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

    # Modelo 100 / IRPF fields
    income: Optional[IncomeData] = None
    deductions: Optional[DeductionsData] = None
    tax_calculation: Optional[TaxCalculation] = None

    # Census Tax Declaration fields
    household_members: List[HouseholdMember] = []
    household_tax_summary: Optional[HouseholdTaxSummary] = None

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
    income: Optional[IncomeData] = None
    deductions: Optional[DeductionsData] = None
    tax_calculation: Optional[TaxCalculation] = None
    household_members: Optional[List[HouseholdMember]] = None
    household_tax_summary: Optional[HouseholdTaxSummary] = None
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
