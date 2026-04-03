"""
Tax Calculation Routes
API endpoints for VAT and IRPF calculations
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Optional
from datetime import date, datetime
from decimal import Decimal
import logging

from app.services.tax_calculation_service import TaxCalculationService
from app.services.modelo_mapping_service import ModeloMappingService
from app.models.tax_models import (
    VATSummary, IRPFSummary, ModeloCalculation,
    TaxPeriodRequest, AutoMapRequest
)
from app.routes.auth import get_current_user

# Database connection
from pymongo import MongoClient
import os
import certifi
from dotenv import load_dotenv

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")

client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = client[DB_NAME]

router = APIRouter(prefix="/tax", tags=["Tax Calculations"])
logger = logging.getLogger(__name__)


def get_tax_service() -> TaxCalculationService:
    """Get tax calculation service instance"""
    return TaxCalculationService(db)


def get_mapping_service() -> ModeloMappingService:
    """Get modelo mapping service instance"""
    return ModeloMappingService(db)


@router.get("/vat/summary", response_model=VATSummary)
def get_vat_summary(
    start_date: date = Query(..., description="Period start date"),
    end_date: date = Query(..., description="Period end date"),
    current_user: dict = Depends(get_current_user),
    tax_service: TaxCalculationService = Depends(get_tax_service)
):
    """
    Get VAT summary for a period (Modelo 303)
    
    Calculates:
    - Output VAT (VAT charged on sales)
    - Input VAT (VAT paid on purchases)
    - VAT Payable = Output VAT - Input VAT
    - Breakdown by VAT rate (21%, 10%, 4%)
    """
    try:
        organization_id = current_user.get("organization_id") or current_user["_id"]
        
        summary = tax_service.calculate_vat_summary(
            organization_id=organization_id,
            start_date=start_date,
            end_date=end_date
        )
        
        return summary
        
    except Exception as e:
        logger.error(f"Error getting VAT summary: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/irpf/summary", response_model=IRPFSummary)
def get_irpf_summary(
    start_date: date = Query(..., description="Quarter start date"),
    end_date: date = Query(..., description="Quarter end date"),
    quarter: int = Query(..., ge=1, le=4, description="Quarter number (1-4)"),
    irpf_rate: Optional[Decimal] = Query(Decimal("20"), description="IRPF rate percentage"),
    current_user: dict = Depends(get_current_user),
    tax_service: TaxCalculationService = Depends(get_tax_service)
):
    """
    Get IRPF summary for a quarter (Modelo 130)
    
    Calculates:
    - Gross Income
    - Deductible Expenses
    - Net Income = Gross Income - Deductible Expenses
    - IRPF Payable = Net Income * IRPF Rate
    - IRPF to Pay (considering previous quarters)
    """
    try:
        organization_id = current_user.get("organization_id") or current_user["_id"]
        
        summary = tax_service.calculate_irpf_summary(
            organization_id=organization_id,
            start_date=start_date,
            end_date=end_date,
            quarter=quarter,
            irpf_rate=irpf_rate
        )
        
        return summary
        
    except Exception as e:
        logger.error(f"Error getting IRPF summary: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/modelo/{modelo_no}/calculation", response_model=ModeloCalculation)
def get_modelo_calculation(
    modelo_no: str,
    start_date: date = Query(..., description="Period start date"),
    end_date: date = Query(..., description="Period end date"),
    current_user: dict = Depends(get_current_user),
    tax_service: TaxCalculationService = Depends(get_tax_service)
):
    """
    Get calculated values for a specific modelo
    
    Supported modelos:
    - 303: VAT (monthly/quarterly)
    - 130: IRPF (quarterly)
    """
    try:
        organization_id = current_user.get("organization_id") or current_user["_id"]
        
        calculation = tax_service.get_modelo_calculation(
            organization_id=organization_id,
            modelo_no=modelo_no,
            start_date=start_date,
            end_date=end_date
        )
        
        if not calculation:
            raise HTTPException(
                status_code=404,
                detail=f"Modelo {modelo_no} not found or not supported"
            )
        
        return calculation
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting modelo calculation: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ledger-entry/{entry_id}/create-tax-transaction")
def create_tax_transaction_from_ledger(
    entry_id: str,
    current_user: dict = Depends(get_current_user),
    tax_service: TaxCalculationService = Depends(get_tax_service)
):
    """
    Create a tax transaction from a ledger entry
    
    Automatically detects:
    - VAT type (input/output) based on account code
    - IRPF type (income/expense/retention) based on account code
    - Applicable modelos
    """
    try:
        organization_id = current_user.get("organization_id") or current_user["_id"]
        
        tax_transaction_id = tax_service.create_tax_transaction_from_ledger(
            ledger_entry_id=entry_id,
            organization_id=organization_id
        )
        
        if not tax_transaction_id:
            return {
                "message": "No tax transaction created (entry not tax-relevant)",
                "tax_transaction_id": None
            }
        
        return {
            "message": "Tax transaction created successfully",
            "tax_transaction_id": tax_transaction_id
        }
        
    except Exception as e:
        logger.error(f"Error creating tax transaction: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/auto-map")
def auto_map_transactions(
    request: AutoMapRequest,
    current_user: dict = Depends(get_current_user),
    mapping_service: ModeloMappingService = Depends(get_mapping_service)
):
    """
    Automatically map ledger entries and tax transactions to modelos
    
    Maps transactions based on:
    - Account codes
    - Account types
    - VAT types
    - IRPF types
    """
    try:
        organization_id = current_user.get("organization_id") or current_user["_id"]
        
        stats = mapping_service.bulk_auto_map(
            organization_id=organization_id,
            start_date=request.start_date,
            end_date=request.end_date,
            force_remap=request.force_remap
        )
        
        return {
            "message": "Auto-mapping completed",
            "stats": stats
        }
        
    except Exception as e:
        logger.error(f"Error in auto-mapping: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/modelo/{modelo_id}/transactions")
def get_modelo_transactions(
    modelo_id: str,
    start_date: date = Query(..., description="Period start date"),
    end_date: date = Query(..., description="Period end date"),
    current_user: dict = Depends(get_current_user),
    mapping_service: ModeloMappingService = Depends(get_mapping_service)
):
    """
    Get all transactions mapped to a specific modelo for a period
    
    Returns:
    - Ledger entries
    - Tax transactions
    """
    try:
        organization_id = current_user.get("organization_id") or current_user["_id"]
        
        transactions = mapping_service.get_transactions_by_modelo(
            organization_id=organization_id,
            modelo_id=modelo_id,
            start_date=start_date,
            end_date=end_date
        )
        
        return transactions
        
    except Exception as e:
        logger.error(f"Error getting modelo transactions: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/modelo-mappings")
def get_modelo_mappings(
    current_user: dict = Depends(get_current_user),
    mapping_service: ModeloMappingService = Depends(get_mapping_service)
):
    """
    Get all modelo mapping configurations
    
    Shows how transactions are automatically mapped to modelos
    """
    try:
        mappings = mapping_service.get_modelo_mappings()
        return {"mappings": mappings}
        
    except Exception as e:
        logger.error(f"Error getting modelo mappings: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sync-all")
def sync_all_tax_data(
    start_date: Optional[date] = Query(None, description="Start date for sync"),
    end_date: Optional[date] = Query(None, description="End date for sync"),
    current_user: dict = Depends(get_current_user),
    tax_service: TaxCalculationService = Depends(get_tax_service),
    mapping_service: ModeloMappingService = Depends(get_mapping_service)
):
    """
    Sync all tax data: create tax transactions from ledger entries and auto-map to modelos
    
    This is a comprehensive operation that:
    1. Creates tax transactions from all ledger entries
    2. Auto-maps all transactions to applicable modelos
    """
    try:
        organization_id = current_user.get("organization_id") or current_user["_id"]
        
        # Build query filter
        query_filter = {"organization_id": organization_id}
        
        if start_date and end_date:
            start_dt = datetime.combine(start_date, datetime.min.time())
            end_dt = datetime.combine(end_date, datetime.max.time())
            query_filter["transaction_date"] = {"$gte": start_dt, "$lte": end_dt}
        
        # Get all ledger entries
        ledger_entries = list(db["ledger"].find(query_filter))
        
        tax_transactions_created = 0
        
        # Create tax transactions
        for entry in ledger_entries:
            tax_tx_id = tax_service.create_tax_transaction_from_ledger(
                ledger_entry_id=str(entry["_id"]),
                organization_id=organization_id
            )
            if tax_tx_id:
                tax_transactions_created += 1
        
        # Auto-map all transactions
        mapping_stats = mapping_service.bulk_auto_map(
            organization_id=organization_id,
            start_date=start_date,
            end_date=end_date,
            force_remap=False
        )
        
        return {
            "message": "Tax data sync completed",
            "tax_transactions_created": tax_transactions_created,
            "mapping_stats": mapping_stats
        }
        
    except Exception as e:
        logger.error(f"Error syncing tax data: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health")
def health_check():
    """Health check for tax calculation service"""
    return {
        "status": "healthy",
        "service": "tax_calculation",
        "features": [
            "VAT calculation (Modelo 303)",
            "IRPF calculation (Modelo 130)",
            "Automatic modelo mapping",
            "Tax transaction tracking"
        ]
    }
