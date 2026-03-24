from pydantic import BaseModel, Field, validator, ConfigDict
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum
from decimal import Decimal
from bson import ObjectId


class AccountType(str, Enum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    INCOME = "income"
    EXPENSE = "expense"


class AccountSubType(str, Enum):
    # Asset subtypes
    CURRENT_ASSET = "current_asset"
    FIXED_ASSET = "fixed_asset"
    INTANGIBLE_ASSET = "intangible_asset"
    
    # Liability subtypes
    CURRENT_LIABILITY = "current_liability"
    LONG_TERM_LIABILITY = "long_term_liability"
    
    # Equity subtypes
    SHARE_CAPITAL = "share_capital"
    RETAINED_EARNINGS = "retained_earnings"
    
    # Income subtypes
    OPERATING_INCOME = "operating_income"
    NON_OPERATING_INCOME = "non_operating_income"
    
    # Expense subtypes
    OPERATING_EXPENSE = "operating_expense"
    NON_OPERATING_EXPENSE = "non_operating_expense"


class VoucherStatus(str, Enum):
    DRAFT = "draft"
    POSTED = "posted"
    REVERSED = "reversed"


class JournalEntryType(str, Enum):
    DEBIT = "debit"
    CREDIT = "credit"


# ==================== CHART OF ACCOUNTS ====================

class Account(BaseModel):
    id: Optional[str] = Field(None, alias="_id")
    organization_id: str
    account_code: str
    account_name: str
    account_type: AccountType
    account_subtype: Optional[AccountSubType] = None
    parent_account_id: Optional[str] = None
    is_active: bool = True
    is_system_account: bool = False
    description: Optional[str] = None
    tax_account: bool = False
    currency: str = "EUR"
    opening_balance: Decimal = Decimal("0.00")
    current_balance: Decimal = Decimal("0.00")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    
    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str, Decimal: float},
        arbitrary_types_allowed=True,
    )

    @validator('account_code')
    def validate_account_code(cls, v):
        if not v or len(v.strip()) == 0:
            raise ValueError('Account code cannot be empty')
        return v.strip().upper()


class AccountCreate(BaseModel):
    account_code: str
    account_name: str
    account_type: AccountType
    account_subtype: Optional[AccountSubType] = None
    parent_account_id: Optional[str] = None
    description: Optional[str] = None
    tax_account: bool = False
    currency: str = "EUR"
    opening_balance: Decimal = Decimal("0.00")


class AccountUpdate(BaseModel):
    account_name: Optional[str] = None
    account_subtype: Optional[AccountSubType] = None
    parent_account_id: Optional[str] = None
    is_active: Optional[bool] = None
    description: Optional[str] = None
    tax_account: Optional[bool] = None


# ==================== JOURNAL ENTRIES ====================

class JournalEntry(BaseModel):
    id: Optional[str] = Field(None, alias="_id")
    organization_id: str
    account_id: str
    account_code: str
    account_name: str
    entry_type: JournalEntryType
    amount: Decimal
    description: str
    reference: Optional[str] = None
    voucher_id: str
    journal_id: str
    transaction_date: datetime
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str, Decimal: float},
        arbitrary_types_allowed=True,
    )

    @validator('amount')
    def validate_amount(cls, v):
        if v <= 0:
            raise ValueError('Amount must be positive')
        return v


class JournalEntryCreate(BaseModel):
    account_id: str
    entry_type: JournalEntryType
    amount: Decimal
    description: str
    reference: Optional[str] = None


# ==================== JOURNALS ====================

class Journal(BaseModel):
    id: Optional[str] = Field(None, alias="_id")
    organization_id: str
    journal_code: str
    journal_name: str
    journal_type: str  # sales, purchase, cash, bank, general
    description: Optional[str] = None
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    
    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str},
        arbitrary_types_allowed=True,
    )


class JournalCreate(BaseModel):
    journal_code: str
    journal_name: str
    journal_type: str
    description: Optional[str] = None


# ==================== VOUCHERS ====================

class Voucher(BaseModel):
    id: Optional[str] = Field(None, alias="_id")
    organization_id: str
    voucher_number: str
    journal_id: str
    journal_code: str
    voucher_date: datetime
    description: str
    reference: Optional[str] = None
    source_document: Optional[str] = None  # invoice_id, expense_id, etc.
    source_type: Optional[str] = None  # invoice, expense, payment, etc.
    status: VoucherStatus = VoucherStatus.DRAFT
    total_debit: Decimal = Decimal("0.00")
    total_credit: Decimal = Decimal("0.00")
    currency: str = "EUR"
    exchange_rate: Decimal = Decimal("1.00")
    posted_by: Optional[str] = None
    posted_at: Optional[datetime] = None
    reversed_by: Optional[str] = None
    reversed_at: Optional[datetime] = None
    reversal_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    
    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str, Decimal: float},
        arbitrary_types_allowed=True,
    )

    @validator('total_debit', 'total_credit')
    def validate_totals(cls, v):
        if v < 0:
            raise ValueError('Totals cannot be negative')
        return v


class VoucherCreate(BaseModel):
    journal_id: str
    voucher_date: datetime
    description: str
    reference: Optional[str] = None
    source_document: Optional[str] = None
    source_type: Optional[str] = None
    entries: List[JournalEntryCreate]
    
    @validator('entries')
    def validate_entries(cls, v):
        if len(v) < 2:
            raise ValueError('Voucher must have at least 2 entries')
        
        total_debit = sum(entry.amount for entry in v if entry.entry_type == JournalEntryType.DEBIT)
        total_credit = sum(entry.amount for entry in v if entry.entry_type == JournalEntryType.CREDIT)
        
        if abs(total_debit - total_credit) > Decimal("0.01"):
            raise ValueError('Total debits must equal total credits')
        
        return v


class VoucherUpdate(BaseModel):
    description: Optional[str] = None
    reference: Optional[str] = None
    voucher_date: Optional[datetime] = None


# ==================== LEDGER ====================

class LedgerEntry(BaseModel):
    id: Optional[str] = Field(None, alias="_id")
    organization_id: str
    account_id: str
    account_code: str
    account_name: str
    voucher_id: str
    voucher_number: str
    journal_id: str
    journal_code: str
    entry_type: JournalEntryType
    amount: Decimal
    running_balance: Decimal
    description: str
    reference: Optional[str] = None
    modelo_id: Optional[str] = None
    transaction_date: datetime
    posted_at: datetime
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str, Decimal: float},
        arbitrary_types_allowed=True,
    )


# ==================== POSTING RULES ====================

class PostingRuleCondition(BaseModel):
    field: str
    operator: str  # equals, contains, greater_than, etc.
    value: Any


class PostingRuleEntry(BaseModel):
    account_code: str
    entry_type: JournalEntryType
    amount_field: str  # field name from source data
    description_template: str


class PostingRule(BaseModel):
    id: Optional[str] = Field(None, alias="_id")
    organization_id: str
    rule_name: str
    event_type: str  # invoice.created, expense.approved, payment.received
    conditions: List[PostingRuleCondition]
    journal_code: str
    entries: List[PostingRuleEntry]
    is_active: bool = True
    priority: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    
    model_config = ConfigDict(
        populate_by_name=True,
        json_encoders={ObjectId: str},
        arbitrary_types_allowed=True,
    )


class PostingRuleCreate(BaseModel):
    rule_name: str
    event_type: str
    conditions: List[PostingRuleCondition]
    journal_code: str
    entries: List[PostingRuleEntry]
    priority: int = 0


# ==================== TRIAL BALANCE ====================

class TrialBalanceEntry(BaseModel):
    account_code: str
    account_name: str
    account_type: AccountType
    debit_balance: Decimal = Decimal("0.00")
    credit_balance: Decimal = Decimal("0.00")
    
    class Config:
        json_encoders = {Decimal: float}


class TrialBalance(BaseModel):
    organization_id: str
    period_start: datetime
    period_end: datetime
    entries: List[TrialBalanceEntry]
    total_debits: Decimal = Decimal("0.00")
    total_credits: Decimal = Decimal("0.00")
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    
    class Config:
        json_encoders = {Decimal: float}


# ==================== ACCOUNT BALANCE ====================

class AccountBalance(BaseModel):
    account_id: str
    account_code: str
    account_name: str
    account_type: AccountType
    opening_balance: Decimal
    total_debits: Decimal
    total_credits: Decimal
    closing_balance: Decimal
    as_of_date: datetime
    
    class Config:
        json_encoders = {Decimal: float}