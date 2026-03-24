from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, Form
from pydantic import BaseModel, Field, validator
from pymongo import MongoClient
from bson import ObjectId
from dotenv import load_dotenv
import os
import certifi
from datetime import datetime, date
from decimal import Decimal
from enum import Enum

# Load env variables
load_dotenv()

router = APIRouter(prefix="/accounting/ledger", tags=["ledger"])

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")
client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = client[DB_NAME]

# Collections
voucher_collection = db["voucher"]
journal_entries_collection = db["journal_entries"]
chart_of_accounts_collection = db["chart_of_accounts"]
ledger_collection = db["ledger"]
accruals_collection = db["accruals"]

# ==================== ENUMS ====================

class AccountType(str, Enum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"

class EntryType(str, Enum):
    DEBIT = "debit"
    CREDIT = "credit"

class JournalEntryStatus(str, Enum):
    DRAFT = "draft"
    POSTED = "posted"
    REVERSED = "reversed"

# ==================== PYDANTIC MODELS ====================

class Account(BaseModel):
    account_code: str = Field(..., description="Unique account code (e.g., 1000, 2000)")
    account_name: str = Field(..., description="Account name (e.g., Cash, Accounts Payable)")
    account_type: AccountType = Field(..., description="Type of account")
    parent_account: Optional[str] = Field(None, description="Parent account code for sub-accounts")
    is_active: bool = Field(True, description="Whether account is active")
    description: Optional[str] = Field(None, description="Account description")

class LedgerEntry(BaseModel):
    account_code: str = Field(..., description="Account code")
    account_name: str = Field(..., description="Account name")
    entry_type: EntryType = Field(..., description="Debit or Credit")
    amount: float = Field(..., gt=0, description="Entry amount (must be positive)")
    description: Optional[str] = Field(None, description="Entry description")
    modelo_id: Optional[str] = Field(None, description="Optional modelo _id reference")

    @validator('amount')
    def validate_amount(cls, v):
        if v <= 0:
            raise ValueError('Amount must be positive')
        return round(v, 2)

class JournalEntryCreate(BaseModel):
    reference_number: Optional[str] = Field(None, description="Reference number (auto-generated if not provided)")
    voucher_id: Optional[str] = Field(None, description="Related voucher ID")
    transaction_date: date = Field(..., description="Transaction date")
    description: str = Field(..., description="Journal entry description")
    entries: List[LedgerEntry] = Field(..., min_items=2, description="List of ledger entries (minimum 2)")
    
    @validator('entries')
    def validate_double_entry(cls, v):
        if len(v) < 2:
            raise ValueError('At least 2 entries required for double-entry bookkeeping')
        
        total_debits = sum(entry.amount for entry in v if entry.entry_type == EntryType.DEBIT)
        total_credits = sum(entry.amount for entry in v if entry.entry_type == EntryType.CREDIT)
        
        if abs(total_debits - total_credits) > 0.01:  # Allow for small rounding differences
            raise ValueError(f'Debits ({total_debits}) must equal Credits ({total_credits})')
        
        return v

class JournalEntry(BaseModel):
    id: Optional[str] = Field(None, alias="_id")
    reference_number: str
    voucher_id: Optional[str] = None
    transaction_date: date
    description: str
    entries: List[LedgerEntry]
    status: JournalEntryStatus = JournalEntryStatus.DRAFT
    total_amount: float
    created_by: Optional[str] = None
    created_at: datetime
    posted_at: Optional[datetime] = None
    
    model_config = {
        "populate_by_name": True
    }

class VoucherPostingRequest(BaseModel):
    voucher_id: str = Field(..., description="ID of approved voucher to post")
    account_mappings: Dict[str, str] = Field(..., description="Mapping of expense types to account codes")
    description: Optional[str] = Field(None, description="Custom description for journal entry")

class AccrualRequest(BaseModel):
    account_code: str = Field(..., description="Account code for accrual")
    amount: float = Field(..., gt=0, description="Accrual amount")
    accrual_date: date = Field(..., description="Date when accrual should be posted")
    reversal_date: date = Field(..., description="Date when accrual should be reversed")
    description: str = Field(..., description="Accrual description")

class LedgerFilter(BaseModel):
    account_code: Optional[str] = None
    account_type: Optional[AccountType] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    entry_type: Optional[EntryType] = None

# ==================== HELPER FUNCTIONS ====================

def generate_reference_number() -> str:
    """Generate unique reference number for journal entries"""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"JE{timestamp}"

def validate_account_exists(account_code: str) -> bool:
    """Check if account exists in chart of accounts"""
    account = chart_of_accounts_collection.find_one({"account_code": account_code, "is_active": True})
    return account is not None

def get_account_info(account_code: str) -> Dict[str, Any]:
    """Get account information"""
    account = chart_of_accounts_collection.find_one({"account_code": account_code})
    if not account:
        raise HTTPException(status_code=404, detail=f"Account {account_code} not found")
    return account

# ==================== API ENDPOINTS ====================

@router.post("/post")
async def post_voucher_to_ledger(
    posting_request: VoucherPostingRequest,
    user_id: str = Query(..., description="User ID posting the entry")
):
    """
    Convert approved voucher into journal entries and post to ledger.
    Implements auto-posting feature from the requirements table.
    """
    try:
        # 1. Validate voucher exists and is approved
        voucher = voucher_collection.find_one({"_id": ObjectId(posting_request.voucher_id)})
        if not voucher:
            raise HTTPException(status_code=404, detail="Voucher not found")
        
        if voucher.get("status") != "approved":
            raise HTTPException(status_code=400, detail="Only approved vouchers can be posted to ledger")
        
        # Check if already posted
        existing_entry = journal_entries_collection.find_one({"voucher_id": posting_request.voucher_id})
        if existing_entry:
            raise HTTPException(status_code=400, detail="Voucher already posted to ledger")
        
        # 2. Extract financial data from voucher (assuming OCR data contains amounts)
        ocr_data = db["ocr"].find_one({"voucher_id": posting_request.voucher_id})
        if not ocr_data:
            raise HTTPException(status_code=400, detail="No OCR data found for voucher")
        
        # Parse amounts from OCR data (this would need to be customized based on your OCR structure)
        total_amount = float(ocr_data.get("total_amount", 0))
        if total_amount <= 0:
            raise HTTPException(status_code=400, detail="Invalid amount in voucher")
        
        # 3. Validate account mappings
        for expense_type, account_code in posting_request.account_mappings.items():
            if not validate_account_exists(account_code):
                raise HTTPException(status_code=400, detail=f"Account {account_code} not found")
        
        # 4. Create journal entries based on document type
        document_type = voucher.get("document_type", "expense")
        entries = []
        
        if document_type == "supplier_invoice":
            # Debit: Expense Account, Credit: Accounts Payable
            expense_account = posting_request.account_mappings.get("expense", "5000")
            payable_account = posting_request.account_mappings.get("accounts_payable", "2000")
            
            expense_info = get_account_info(expense_account)
            payable_info = get_account_info(payable_account)
            
            entries = [
                LedgerEntry(
                    account_code=expense_account,
                    account_name=expense_info["account_name"],
                    entry_type=EntryType.DEBIT,
                    amount=total_amount,
                    description=f"Expense from voucher {posting_request.voucher_id}"
                ),
                LedgerEntry(
                    account_code=payable_account,
                    account_name=payable_info["account_name"],
                    entry_type=EntryType.CREDIT,
                    amount=total_amount,
                    description=f"Accounts payable from voucher {posting_request.voucher_id}"
                )
            ]
        
        elif document_type == "expense":
            # Debit: Expense Account, Credit: Cash/Bank
            expense_account = posting_request.account_mappings.get("expense", "5000")
            cash_account = posting_request.account_mappings.get("cash", "1000")
            
            expense_info = get_account_info(expense_account)
            cash_info = get_account_info(cash_account)
            
            entries = [
                LedgerEntry(
                    account_code=expense_account,
                    account_name=expense_info["account_name"],
                    entry_type=EntryType.DEBIT,
                    amount=total_amount,
                    description=f"Expense from voucher {posting_request.voucher_id}"
                ),
                LedgerEntry(
                    account_code=cash_account,
                    account_name=cash_info["account_name"],
                    entry_type=EntryType.CREDIT,
                    amount=total_amount,
                    description=f"Cash payment from voucher {posting_request.voucher_id}"
                )
            ]
        
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported document type: {document_type}")
        
        # 5. Create journal entry
        journal_entry_data = {
            "reference_number": generate_reference_number(),
            "voucher_id": posting_request.voucher_id,
            "transaction_date": datetime.now().date(),
            "description": posting_request.description or f"Auto-posting from voucher {posting_request.voucher_id}",
            "entries": [entry.dict() for entry in entries],
            "status": JournalEntryStatus.POSTED.value,
            "total_amount": total_amount,
            "created_by": user_id,
            "created_at": datetime.utcnow(),
            "posted_at": datetime.utcnow()
        }
        
        # 6. Insert journal entry
        result = journal_entries_collection.insert_one(journal_entry_data)
        journal_entry_id = str(result.inserted_id)
        
        # 7. Post to ledger (create individual ledger records)
        ledger_records = []
        for entry in entries:
            ledger_record = {
                "journal_entry_id": journal_entry_id,
                "reference_number": journal_entry_data["reference_number"],
                "account_code": entry.account_code,
                "account_name": entry.account_name,
                "transaction_date": journal_entry_data["transaction_date"],
                "entry_type": entry.entry_type.value,
                "amount": entry.amount,
                "description": entry.description,
                "voucher_id": posting_request.voucher_id,
                "created_at": datetime.utcnow()
            }
            ledger_records.append(ledger_record)
        
        ledger_collection.insert_many(ledger_records)
        
        # 8. Update voucher status
        voucher_collection.update_one(
            {"_id": ObjectId(posting_request.voucher_id)},
            {"$set": {"ledger_status": "posted", "journal_entry_id": journal_entry_id, "posted_at": datetime.utcnow()}}
        )
        
        return {
            "message": "Voucher posted to ledger successfully",
            "journal_entry_id": journal_entry_id,
            "reference_number": journal_entry_data["reference_number"],
            "total_amount": total_amount,
            "entries": len(entries)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error posting to ledger: {str(e)}")


@router.post("/manual")
async def create_manual_journal_entry(
    journal_entry: JournalEntryCreate,
    user_id: str = Query(..., description="User ID creating the entry")
):
    """
    Create manual journal entry.
    Implements manual journal entry feature from the requirements table.
    """
    try:
        # 1. Validate all accounts exist
        for entry in journal_entry.entries:
            if not validate_account_exists(entry.account_code):
                raise HTTPException(status_code=400, detail=f"Account {entry.account_code} not found")
        
        # 2. Generate reference number if not provided
        reference_number = journal_entry.reference_number or generate_reference_number()
        
        # 3. Calculate total amount
        total_amount = sum(entry.amount for entry in journal_entry.entries if entry.entry_type == EntryType.DEBIT)
        
        # 4. Enrich entries with account names
        enriched_entries = []
        for entry in journal_entry.entries:
            account_info = get_account_info(entry.account_code)
            enriched_entry = entry.dict()
            enriched_entry["account_name"] = account_info["account_name"]
            enriched_entries.append(enriched_entry)
        
        # 5. Create journal entry document
        journal_entry_data = {
            "reference_number": reference_number,
            "voucher_id": journal_entry.voucher_id,
            "transaction_date": journal_entry.transaction_date,
            "description": journal_entry.description,
            "entries": enriched_entries,
            "status": JournalEntryStatus.DRAFT.value,
            "total_amount": total_amount,
            "created_by": user_id,
            "created_at": datetime.utcnow(),
            "posted_at": None
        }
        
        # 6. Insert journal entry
        result = journal_entries_collection.insert_one(journal_entry_data)
        journal_entry_id = str(result.inserted_id)
        
        return {
            "message": "Manual journal entry created successfully",
            "journal_entry_id": journal_entry_id,
            "reference_number": reference_number,
            "status": "draft",
            "total_amount": total_amount
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creating journal entry: {str(e)}")


@router.post("/entries")
async def create_ledger_entry(
    entry_data: dict,
    user_id: str = Query(..., description="User ID creating the entry")
):
    """
    Create ledger entry directly (bypassing journal entry).
    Optionally include modelo_id in the entry_data.
    """
    try:
        entry_data["created_at"] = datetime.utcnow()
        entry_data["created_by"] = user_id
        
        # If modelo_id provided, validate it exists
        if entry_data.get("modelo_id"):
            modelo = db["modelos"].find_one({"_id": ObjectId(entry_data["modelo_id"])})
            if not modelo:
                raise HTTPException(status_code=404, detail="Modelo not found")
        
        result = ledger_collection.insert_one(entry_data)
        
        return {
            "message": "Ledger entry created successfully",
            "ledger_entry_id": str(result.inserted_id),
            "account_code": entry_data.get("account_code"),
            "amount": entry_data.get("amount"),
            "modelo_id": entry_data.get("modelo_id")
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creating ledger entry: {str(e)}")


@router.put("/entries/{entry_id}/modelo")
async def update_ledger_modelo(
    entry_id: str,
    modelo_id: str = Query(..., description="Modelo _id to assign"),
    user_id: str = Query(..., description="User ID")
):
    """
    Update ledger entry with a modelo by its _id.
    Assigns the modelo_id to the ledger entry.
    """
    try:
        # Validate entry exists
        if not ObjectId.is_valid(entry_id):
            raise HTTPException(status_code=400, detail="Invalid entry ID format")
        
        entry = ledger_collection.find_one({"_id": ObjectId(entry_id)})
        if not entry:
            raise HTTPException(status_code=404, detail="Ledger entry not found")
        
        # Validate modelo exists
        if not ObjectId.is_valid(modelo_id):
            raise HTTPException(status_code=400, detail="Invalid modelo ID format")
        
        modelo = db["modelos"].find_one({"_id": ObjectId(modelo_id)})
        if not modelo:
            raise HTTPException(status_code=404, detail="Modelo not found")
        
        # Update ledger entry with modelo
        result = ledger_collection.update_one(
            {"_id": ObjectId(entry_id)},
            {"$set": {
                "modelo_id": modelo_id,
                "updated_at": datetime.utcnow()
            }}
        )
        
        if result.modified_count == 0:
            raise HTTPException(status_code=500, detail="Failed to update ledger entry")
        
        return {
            "message": "Modelo assigned successfully",
            "entry_id": entry_id,
            "modelo_id": modelo_id,
            "modelo_no": modelo.get("modelo_no"),
            "modelo_name": modelo.get("name")
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/")
async def get_ledger_entries(
    account_code: Optional[str] = Query(None, description="Filter by account code"),
    account_type: Optional[AccountType] = Query(None, description="Filter by account type"),
    start_date: Optional[date] = Query(None, description="Start date filter"),
    end_date: Optional[date] = Query(None, description="End date filter"),
    entry_type: Optional[EntryType] = Query(None, description="Filter by debit/credit"),
    limit: int = Query(100, le=1000, description="Maximum number of records to return"),
    skip: int = Query(0, ge=0, description="Number of records to skip")
):
    """
    View ledger transactions with filtering capabilities.
    Implements ledger view feature from the requirements table.
    """
    try:
        # Build query
        query = {}
        
        if account_code:
            query["account_code"] = account_code
        
        if start_date:
            query.setdefault("transaction_date", {})["$gte"] = start_date
        
        if end_date:
            query.setdefault("transaction_date", {})["$lte"] = end_date
        
        if entry_type:
            query["entry_type"] = entry_type.value
        
        # If filtering by account type, we need to join with chart of accounts
        if account_type:
            # Get all accounts of the specified type
            accounts = list(chart_of_accounts_collection.find(
                {"account_type": account_type.value, "is_active": True},
                {"account_code": 1}
            ))
            account_codes = [acc["account_code"] for acc in accounts]
            query["account_code"] = {"$in": account_codes}
        
        # Execute query
        ledger_entries = list(ledger_collection.find(query)
                            .sort("transaction_date", -1)
                            .skip(skip)
                            .limit(limit))
        
        # Get total count
        total_count = ledger_collection.count_documents(query)
        
        # Format response
        for entry in ledger_entries:
            entry["_id"] = str(entry["_id"])
            if isinstance(entry.get("transaction_date"), datetime):
                entry["transaction_date"] = entry["transaction_date"].date()
        
        return {
            "entries": ledger_entries,
            "total_count": total_count,
            "returned_count": len(ledger_entries),
            "filters_applied": {
                "account_code": account_code,
                "account_type": account_type,
                "start_date": start_date,
                "end_date": end_date,
                "entry_type": entry_type
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving ledger entries: {str(e)}")


@router.post("/accrual")
async def create_accrual_entry(
    accrual: AccrualRequest,
    user_id: str = Query(..., description="User ID creating the accrual")
):
    """
    Schedule accrual adjustments for future periods.
    Implements accruals & reversals feature from the requirements table.
    """
    try:
        # 1. Validate account exists
        if not validate_account_exists(accrual.account_code):
            raise HTTPException(status_code=400, detail=f"Account {accrual.account_code} not found")
        
        # 2. Validate dates
        if accrual.reversal_date <= accrual.accrual_date:
            raise HTTPException(status_code=400, detail="Reversal date must be after accrual date")
        
        # 3. Get account info
        account_info = get_account_info(accrual.account_code)
        
        # 4. Create accrual record
        accrual_data = {
            "account_code": accrual.account_code,
            "account_name": account_info["account_name"],
            "amount": accrual.amount,
            "accrual_date": accrual.accrual_date,
            "reversal_date": accrual.reversal_date,
            "description": accrual.description,
            "status": "scheduled",
            "created_by": user_id,
            "created_at": datetime.utcnow(),
            "accrual_journal_entry_id": None,
            "reversal_journal_entry_id": None
        }
        
        result = accruals_collection.insert_one(accrual_data)
        accrual_id = str(result.inserted_id)
        
        return {
            "message": "Accrual scheduled successfully",
            "accrual_id": accrual_id,
            "accrual_date": accrual.accrual_date,
            "reversal_date": accrual.reversal_date,
            "amount": accrual.amount,
            "status": "scheduled"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creating accrual: {str(e)}")


@router.post("/journal-entry/{journal_entry_id}/post")
async def post_journal_entry(
    journal_entry_id: str,
    user_id: str = Query(..., description="User ID posting the entry")
):
    """
    Post a draft journal entry to the ledger.
    """
    try:
        # 1. Get journal entry
        journal_entry = journal_entries_collection.find_one({"_id": ObjectId(journal_entry_id)})
        if not journal_entry:
            raise HTTPException(status_code=404, detail="Journal entry not found")
        
        if journal_entry.get("status") != JournalEntryStatus.DRAFT.value:
            raise HTTPException(status_code=400, detail="Only draft entries can be posted")
        
        # 2. Update journal entry status
        journal_entries_collection.update_one(
            {"_id": ObjectId(journal_entry_id)},
            {"$set": {"status": JournalEntryStatus.POSTED.value, "posted_at": datetime.utcnow()}}
        )
        
        # 3. Create ledger records
        ledger_records = []
        for entry in journal_entry["entries"]:
            ledger_record = {
                "journal_entry_id": journal_entry_id,
                "reference_number": journal_entry["reference_number"],
                "account_code": entry["account_code"],
                "account_name": entry["account_name"],
                "transaction_date": journal_entry["transaction_date"],
                "entry_type": entry["entry_type"],
                "amount": entry["amount"],
                "description": entry["description"],
                "voucher_id": journal_entry.get("voucher_id"),
                "created_at": datetime.utcnow()
            }
            ledger_records.append(ledger_record)
        
        ledger_collection.insert_many(ledger_records)
        
        return {
            "message": "Journal entry posted successfully",
            "journal_entry_id": journal_entry_id,
            "reference_number": journal_entry["reference_number"],
            "ledger_entries_created": len(ledger_records)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error posting journal entry: {str(e)}")


@router.get("/trial-balance")
async def get_trial_balance(
    as_of_date: date = Query(..., description="Trial balance as of date"),
    account_type: Optional[AccountType] = Query(None, description="Filter by account type")
):
    """
    Generate trial balance report.
    """
    try:
        # Build query for ledger entries up to the specified date
        query = {"transaction_date": {"$lte": as_of_date}}
        
        # Get all ledger entries up to the date
        ledger_entries = list(ledger_collection.find(query))
        
        # Calculate balances by account
        account_balances = {}
        for entry in ledger_entries:
            account_code = entry["account_code"]
            amount = entry["amount"]
            entry_type = entry["entry_type"]
            
            if account_code not in account_balances:
                account_balances[account_code] = {
                    "account_code": account_code,
                    "account_name": entry["account_name"],
                    "debit_total": 0,
                    "credit_total": 0,
                    "balance": 0
                }
            
            if entry_type == EntryType.DEBIT.value:
                account_balances[account_code]["debit_total"] += amount
            else:
                account_balances[account_code]["credit_total"] += amount
        
        # Calculate net balances and get account types
        trial_balance = []
        total_debits = 0
        total_credits = 0
        
        for account_code, balance_data in account_balances.items():
            # Get account type from chart of accounts
            account_info = chart_of_accounts_collection.find_one({"account_code": account_code})
            if not account_info:
                continue
            
            account_type_value = account_info.get("account_type")
            
            # Filter by account type if specified
            if account_type and account_type_value != account_type.value:
                continue
            
            debit_total = balance_data["debit_total"]
            credit_total = balance_data["credit_total"]
            
            # Calculate balance based on account type
            if account_type_value in [AccountType.ASSET.value, AccountType.EXPENSE.value]:
                # Normal debit balance accounts
                balance = debit_total - credit_total
            else:
                # Normal credit balance accounts
                balance = credit_total - debit_total
            
            balance_data["account_type"] = account_type_value
            balance_data["balance"] = balance
            
            if balance > 0:
                if account_type_value in [AccountType.ASSET.value, AccountType.EXPENSE.value]:
                    total_debits += balance
                else:
                    total_credits += balance
            elif balance < 0:
                if account_type_value in [AccountType.ASSET.value, AccountType.EXPENSE.value]:
                    total_credits += abs(balance)
                else:
                    total_debits += abs(balance)
            
            trial_balance.append(balance_data)
        
        # Sort by account code
        trial_balance.sort(key=lambda x: x["account_code"])
        
        return {
            "as_of_date": as_of_date,
            "trial_balance": trial_balance,
            "total_debits": round(total_debits, 2),
            "total_credits": round(total_credits, 2),
            "is_balanced": abs(total_debits - total_credits) < 0.01,
            "account_count": len(trial_balance)
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generating trial balance: {str(e)}")


# ==================== CHART OF ACCOUNTS MANAGEMENT ====================

@router.post("/accounts")
async def create_account(
    account: Account,
    user_id: str = Query(..., description="User ID creating the account")
):
    """Create new account in chart of accounts."""
    try:
        # Check if account code already exists
        existing = chart_of_accounts_collection.find_one({"account_code": account.account_code})
        if existing:
            raise HTTPException(status_code=400, detail=f"Account code {account.account_code} already exists")
        
        # Validate parent account if specified
        if account.parent_account:
            parent = chart_of_accounts_collection.find_one({"account_code": account.parent_account})
            if not parent:
                raise HTTPException(status_code=400, detail=f"Parent account {account.parent_account} not found")
        
        account_data = account.dict()
        account_data.update({
            "created_by": user_id,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        })
        
        result = chart_of_accounts_collection.insert_one(account_data)
        
        return {
            "message": "Account created successfully",
            "account_id": str(result.inserted_id),
            "account_code": account.account_code,
            "account_name": account.account_name
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creating account: {str(e)}")


@router.get("/accounts")
async def get_chart_of_accounts(
    account_type: Optional[AccountType] = Query(None, description="Filter by account type"),
    is_active: bool = Query(True, description="Filter by active status"),
    parent_account: Optional[str] = Query(None, description="Filter by parent account")
):
    """Get chart of accounts with filtering."""
    try:
        query = {"is_active": is_active}
        
        if account_type:
            query["account_type"] = account_type.value
        
        if parent_account:
            query["parent_account"] = parent_account
        
        accounts = list(chart_of_accounts_collection.find(query).sort("account_code", 1))
        
        for account in accounts:
            account["_id"] = str(account["_id"])
        
        return {
            "accounts": accounts,
            "count": len(accounts)
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving accounts: {str(e)}")