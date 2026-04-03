"""
Modelo Mapping Service
Automatically maps ledger entries and transactions to tax forms (modelos)
"""

from typing import List, Optional, Dict, Any
from datetime import datetime, date
from decimal import Decimal
from pymongo.database import Database
from bson import ObjectId
import logging

from app.models.tax_models import (
    VATType, IRPFType, ModeloMapping, TaxPeriod
)

logger = logging.getLogger(__name__)


class ModeloMappingService:
    """Service for automatic modelo mapping"""
    
    def __init__(self, db: Database):
        self.db = db
        self.modelo_mappings = db["modelo_mappings"]
        self.tax_transactions = db["tax_transactions"]
        self.ledger = db["ledger"]
        self.modelos = db["modelos"]
        self._create_indexes()
        self._ensure_default_mappings()
    
    def _create_indexes(self):
        """Create database indexes"""
        try:
            self.modelo_mappings.create_index("modelo_id", unique=True)
            self.modelo_mappings.create_index("modelo_no")
        except Exception as e:
            logger.warning(f"Index creation warning: {e}")
    
    def _ensure_default_mappings(self):
        """Ensure default modelo mappings exist"""
        try:
            # Check if mappings already exist
            if self.modelo_mappings.count_documents({}) > 0:
                return
            
            # Get modelos
            modelo_303 = self.modelos.find_one({"modelo_no": "303"})
            modelo_130 = self.modelos.find_one({"modelo_no": "130"})
            
            default_mappings = []
            
            # Modelo 303 - VAT
            if modelo_303:
                default_mappings.append({
                    "modelo_id": str(modelo_303["_id"]),
                    "modelo_no": "303",
                    "account_codes": ["472", "477"],  # VAT accounts
                    "account_types": [],
                    "vat_types": [VATType.INPUT.value, VATType.OUTPUT.value],
                    "irpf_types": [],
                    "period_type": TaxPeriod.MONTHLY.value,
                    "auto_map": True,
                    "created_at": datetime.utcnow()
                })
            
            # Modelo 130 - IRPF
            if modelo_130:
                default_mappings.append({
                    "modelo_id": str(modelo_130["_id"]),
                    "modelo_no": "130",
                    "account_codes": ["4", "5", "6", "475"],  # Revenue and expense accounts
                    "account_types": ["revenue", "expense"],
                    "vat_types": [],
                    "irpf_types": [IRPFType.INCOME.value, IRPFType.EXPENSE.value, IRPFType.RETENTION.value],
                    "period_type": TaxPeriod.QUARTERLY.value,
                    "auto_map": True,
                    "created_at": datetime.utcnow()
                })
            
            if default_mappings:
                self.modelo_mappings.insert_many(default_mappings)
                logger.info(f"Created {len(default_mappings)} default modelo mappings")
                
        except Exception as e:
            logger.error(f"Error ensuring default mappings: {e}")
    
    def auto_map_ledger_entry(
        self,
        ledger_entry_id: str,
        organization_id: str
    ) -> List[str]:
        """
        Automatically map a ledger entry to applicable modelos
        Returns list of modelo IDs that were mapped
        """
        try:
            ledger_entry = self.ledger.find_one({"_id": ObjectId(ledger_entry_id)})
            
            if not ledger_entry:
                logger.warning(f"Ledger entry {ledger_entry_id} not found")
                return []
            
            account_code = ledger_entry.get("account_code", "")
            account_type = ledger_entry.get("account_type", "")
            
            # Find applicable mappings
            applicable_modelos = []
            
            mappings = self.modelo_mappings.find({"auto_map": True})
            
            for mapping in mappings:
                is_applicable = False
                
                # Check account code match (prefix match)
                for code_prefix in mapping.get("account_codes", []):
                    if account_code.startswith(code_prefix):
                        is_applicable = True
                        break
                
                # Check account type match
                if not is_applicable and account_type in mapping.get("account_types", []):
                    is_applicable = True
                
                if is_applicable:
                    applicable_modelos.append(mapping["modelo_id"])
            
            # Update ledger entry with modelo mappings
            if applicable_modelos:
                self.ledger.update_one(
                    {"_id": ObjectId(ledger_entry_id)},
                    {
                        "$set": {
                            "modelo_ids": applicable_modelos,
                            "mapped_at": datetime.utcnow()
                        }
                    }
                )
                
                logger.info(f"Mapped ledger entry {ledger_entry_id} to {len(applicable_modelos)} modelos")
            
            return applicable_modelos
            
        except Exception as e:
            logger.error(f"Error auto-mapping ledger entry: {e}")
            return []
    
    def auto_map_tax_transaction(
        self,
        tax_transaction_id: str
    ) -> List[str]:
        """
        Automatically map a tax transaction to applicable modelos
        Returns list of modelo IDs that were mapped
        """
        try:
            tax_transaction = self.tax_transactions.find_one({"_id": ObjectId(tax_transaction_id)})
            
            if not tax_transaction:
                logger.warning(f"Tax transaction {tax_transaction_id} not found")
                return []
            
            vat_type = tax_transaction.get("vat_type")
            irpf_type = tax_transaction.get("irpf_type")
            
            # Find applicable mappings
            applicable_modelos = []
            
            mappings = self.modelo_mappings.find({"auto_map": True})
            
            for mapping in mappings:
                is_applicable = False
                
                # Check VAT type match
                if vat_type and vat_type in mapping.get("vat_types", []):
                    is_applicable = True
                
                # Check IRPF type match
                if irpf_type and irpf_type in mapping.get("irpf_types", []):
                    is_applicable = True
                
                if is_applicable:
                    applicable_modelos.append(mapping["modelo_id"])
            
            # Update tax transaction with modelo mappings
            if applicable_modelos:
                self.tax_transactions.update_one(
                    {"_id": ObjectId(tax_transaction_id)},
                    {
                        "$set": {
                            "modelo_ids": applicable_modelos,
                            "mapped_at": datetime.utcnow()
                        }
                    }
                )
                
                logger.info(f"Mapped tax transaction {tax_transaction_id} to {len(applicable_modelos)} modelos")
            
            return applicable_modelos
            
        except Exception as e:
            logger.error(f"Error auto-mapping tax transaction: {e}")
            return []
    
    def bulk_auto_map(
        self,
        organization_id: str,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        force_remap: bool = False
    ) -> Dict[str, int]:
        """
        Bulk auto-map ledger entries and tax transactions
        Returns statistics about mapping operations
        """
        try:
            stats = {
                "ledger_entries_processed": 0,
                "ledger_entries_mapped": 0,
                "tax_transactions_processed": 0,
                "tax_transactions_mapped": 0
            }
            
            # Build query filter
            query_filter = {"organization_id": organization_id}
            
            if not force_remap:
                query_filter["$or"] = [
                    {"modelo_ids": {"$exists": False}},
                    {"modelo_ids": {"$size": 0}}
                ]
            
            if start_date and end_date:
                start_dt = datetime.combine(start_date, datetime.min.time())
                end_dt = datetime.combine(end_date, datetime.max.time())
                query_filter["transaction_date"] = {"$gte": start_dt, "$lte": end_dt}
            
            # Map ledger entries
            ledger_entries = self.ledger.find(query_filter)
            
            for entry in ledger_entries:
                stats["ledger_entries_processed"] += 1
                mapped_modelos = self.auto_map_ledger_entry(
                    str(entry["_id"]),
                    organization_id
                )
                if mapped_modelos:
                    stats["ledger_entries_mapped"] += 1
            
            # Map tax transactions
            tax_transactions = self.tax_transactions.find(query_filter)
            
            for transaction in tax_transactions:
                stats["tax_transactions_processed"] += 1
                mapped_modelos = self.auto_map_tax_transaction(str(transaction["_id"]))
                if mapped_modelos:
                    stats["tax_transactions_mapped"] += 1
            
            logger.info(f"Bulk auto-mapping completed: {stats}")
            return stats
            
        except Exception as e:
            logger.error(f"Error in bulk auto-mapping: {e}")
            raise
    
    def get_transactions_by_modelo(
        self,
        organization_id: str,
        modelo_id: str,
        start_date: date,
        end_date: date
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Get all transactions mapped to a specific modelo for a period
        """
        try:
            start_dt = datetime.combine(start_date, datetime.min.time())
            end_dt = datetime.combine(end_date, datetime.max.time())
            
            # Get ledger entries
            ledger_entries = list(self.ledger.find({
                "organization_id": organization_id,
                "modelo_ids": modelo_id,
                "transaction_date": {"$gte": start_dt, "$lte": end_dt}
            }))
            
            # Get tax transactions
            tax_transactions = list(self.tax_transactions.find({
                "organization_id": organization_id,
                "modelo_ids": modelo_id,
                "transaction_date": {"$gte": start_dt, "$lte": end_dt}
            }))
            
            # Convert ObjectId to string
            for entry in ledger_entries:
                entry["_id"] = str(entry["_id"])
            
            for transaction in tax_transactions:
                transaction["_id"] = str(transaction["_id"])
            
            return {
                "ledger_entries": ledger_entries,
                "tax_transactions": tax_transactions
            }
            
        except Exception as e:
            logger.error(f"Error getting transactions by modelo: {e}")
            raise
    
    def create_modelo_mapping(
        self,
        modelo_id: str,
        modelo_no: str,
        account_codes: List[str],
        account_types: List[str],
        vat_types: List[str],
        irpf_types: List[str],
        period_type: str,
        auto_map: bool = True
    ) -> str:
        """Create a new modelo mapping configuration"""
        try:
            mapping = {
                "modelo_id": modelo_id,
                "modelo_no": modelo_no,
                "account_codes": account_codes,
                "account_types": account_types,
                "vat_types": vat_types,
                "irpf_types": irpf_types,
                "period_type": period_type,
                "auto_map": auto_map,
                "created_at": datetime.utcnow()
            }
            
            result = self.modelo_mappings.insert_one(mapping)
            logger.info(f"Created modelo mapping for {modelo_no}")
            return str(result.inserted_id)
            
        except Exception as e:
            logger.error(f"Error creating modelo mapping: {e}")
            raise
    
    def get_modelo_mappings(self) -> List[Dict[str, Any]]:
        """Get all modelo mappings"""
        try:
            mappings = list(self.modelo_mappings.find())
            
            for mapping in mappings:
                mapping["_id"] = str(mapping["_id"])
            
            return mappings
            
        except Exception as e:
            logger.error(f"Error getting modelo mappings: {e}")
            return []
