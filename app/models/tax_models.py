"""
Tax Models
Models for tax calculations, VAT tracking, and IRPF computations
"""

from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List, Dict, Any
from datetime import datetime, date
from decimal import Decimal
from enum import Enum


class VATType(str, Enum):
    """VAT transaction type"""
    INPUT = "input"  # VAT paid on purchases (deductible)
    OUTPUT = "output"  # VAT charged on sales (payable)


class IRPFType(str, Enum):
    """IRPF transaction type"""
    INCOME = "income"  # Income subject to IRPF
    EXPENSE = "expense"  # Deductible expenses
    RETENTION = "retention"  # IRPF withheld


class TaxPeriod(str, Enum):
    """Tax reporting period"""
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"


class VATRate(str, Enum):
    """Spanish VAT rates"""
    STANDARD = "21"  # General rate
    REDUCED = "10"  # Reduced rate (food, transport, hotels)
    SUPER_REDUCED = "4"  # Super reduced (basic necessities)
    EXEMPT = "0"  # Exempt transactions


class TaxTransaction(BaseModel):
    """Individual tax transaction"""
    id: Optional[str] = Field(None, alias="_id")
    organization_id: str
    transaction_date: datetime
    
    # Ledger linkage
    ledger_entry_id: Optional[str] = None
    voucher_id: Optional[str] = None
    journal_entry_id: Optional[str] = None
    
    # Tax details
    vat_type: Optional[VATType] = None
    vat_rate: Optional[Decimal] = Field(None, description="VAT rate percentage")
    vat_base: Optional[Decimal] = Field(None, description="Base amount before VAT")
    vat_amount: Optional[Decimal] = Field(None, description="VAT amount")
    
    irpf_type: Optional[IRPFType] = None
    irpf_rate: Optional[Decimal] = Field(None, description="IRPF rate percentage")
    irpf_base: Optional[Decimal] = Field(None, description="Base amount for IRPF")
    irpf_amount: Optional[Decimal] = Field(None, description="IRPF amount")
    
    # Modelo mapping
    modelo_ids: List[str] = Field(default_factory=list, description="Applicable modelos")
    
    # Metadata
    description: Optional[str] = None
    counterparty: Optional[str] = None
    invoice_number: Optional[str] = None
    
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = None
    
    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True
    )


class VATSummary(BaseModel):
    """VAT summary for a period"""
    period_start: date
    period_end: date
    
    # Output VAT (sales)
    output_vat_base: Decimal = Field(default=Decimal("0"))
    output_vat_amount: Decimal = Field(default=Decimal("0"))
    output_transactions_count: int = 0
    
    # Input VAT (purchases)
    input_vat_base: Decimal = Field(default=Decimal("0"))
    input_vat_amount: Decimal = Field(default=Decimal("0"))
    input_transactions_count: int = 0
    
    # Net VAT
    vat_payable: Decimal = Field(default=Decimal("0"), description="Output VAT - Input VAT")
    
    # Breakdown by rate
    vat_by_rate: Dict[str, Dict[str, Decimal]] = Field(default_factory=dict)
    
    model_config = ConfigDict(arbitrary_types_allowed=True)


class IRPFSummary(BaseModel):
    """IRPF summary for a period (Modelo 130)"""
    period_start: date
    period_end: date
    quarter: int = Field(..., ge=1, le=4, description="Quarter number (1-4)")
    
    # Income
    gross_income: Decimal = Field(default=Decimal("0"))
    
    # Deductible expenses
    deductible_expenses: Decimal = Field(default=Decimal("0"))
    
    # Net income
    net_income: Decimal = Field(default=Decimal("0"), description="Gross income - Deductible expenses")
    
    # IRPF calculation (20% for professionals, 15% for business)
    irpf_rate: Decimal = Field(default=Decimal("20"))
    irpf_payable: Decimal = Field(default=Decimal("0"))
    
    # Previous quarters (for cumulative calculation)
    previous_quarters_income: Decimal = Field(default=Decimal("0"))
    previous_quarters_irpf: Decimal = Field(default=Decimal("0"))
    
    # Final payment
    irpf_to_pay: Decimal = Field(default=Decimal("0"), description="IRPF for this quarter minus previous payments")
    
    model_config = ConfigDict(arbitrary_types_allowed=True)


class ModeloMapping(BaseModel):
    """Mapping configuration for modelos"""
    id: Optional[str] = Field(None, alias="_id")
    modelo_id: str
    modelo_no: str
    
    # Account mapping rules
    account_codes: List[str] = Field(default_factory=list, description="Account codes that map to this modelo")
    account_types: List[str] = Field(default_factory=list, description="Account types that map to this modelo")
    
    # Transaction type rules
    vat_types: List[VATType] = Field(default_factory=list)
    irpf_types: List[IRPFType] = Field(default_factory=list)
    
    # Period configuration
    period_type: TaxPeriod
    
    # Auto-mapping enabled
    auto_map: bool = Field(default=True, description="Automatically map transactions")
    
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = None
    
    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True
    )


class ModeloCalculation(BaseModel):
    """Calculated values for a specific modelo"""
    modelo_id: str
    modelo_no: str
    modelo_name: str
    
    period_start: date
    period_end: date
    period_type: TaxPeriod
    
    # Calculated values
    vat_summary: Optional[VATSummary] = None
    irpf_summary: Optional[IRPFSummary] = None
    
    # Transaction details
    transaction_ids: List[str] = Field(default_factory=list)
    transaction_count: int = 0
    
    # Status
    is_filed: bool = False
    filed_date: Optional[datetime] = None
    
    calculated_at: datetime = Field(default_factory=datetime.utcnow)
    
    model_config = ConfigDict(arbitrary_types_allowed=True)


class TaxPeriodRequest(BaseModel):
    """Request model for tax period calculations"""
    start_date: date
    end_date: date
    modelo_no: Optional[str] = None
    include_details: bool = Field(default=False, description="Include transaction details")


class AutoMapRequest(BaseModel):
    """Request to auto-map transactions to modelos"""
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    force_remap: bool = Field(default=False, description="Remap already mapped transactions")
