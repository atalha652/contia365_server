"""
Tax Calculation Service
Handles VAT and IRPF calculations, aggregations, and modelo computations
"""

from typing import List, Optional, Dict, Any
from datetime import datetime, date
from decimal import Decimal
from pymongo.database import Database
from bson import ObjectId
import logging

from app.models.tax_models import (
    VATType, IRPFType, TaxPeriod, VATSummary, IRPFSummary,
    TaxTransaction, ModeloCalculation
)
from app.services.fiscal_profile_service import get_canonical_fiscal_profile
from app.services.irpf_130 import modelo_130_payable, resolve_modelo_130_rate_percent

logger = logging.getLogger(__name__)


class TaxCalculationService:
    """Service for tax calculations and aggregations"""
    
    def __init__(self, db: Database):
        self.db = db
        self.tax_transactions = db["tax_transactions"]
        self.ledger = db["ledger"]
        self.vouchers = db["voucher"]
        self.modelos = db["modelos"]
        self._create_indexes()
    
    def _create_indexes(self):
        """Create database indexes for performance"""
        try:
            self.tax_transactions.create_index([
                ("organization_id", 1),
                ("transaction_date", -1)
            ])
            self.tax_transactions.create_index([
                ("organization_id", 1),
                ("vat_type", 1),
                ("transaction_date", -1)
            ])
            self.tax_transactions.create_index([
                ("organization_id", 1),
                ("irpf_type", 1),
                ("transaction_date", -1)
            ])
            self.tax_transactions.create_index("ledger_entry_id")
            self.tax_transactions.create_index("voucher_id")
        except Exception as e:
            logger.warning(f"Index creation warning: {e}")
    
    def calculate_vat_summary(
        self,
        organization_id: str,
        start_date: date,
        end_date: date
    ) -> VATSummary:
        """
        Calculate VAT summary for a period (Modelo 303)
        Output VAT - Input VAT = VAT Payable
        """
        try:
            # Convert dates to datetime for MongoDB query
            start_dt = datetime.combine(start_date, datetime.min.time())
            end_dt = datetime.combine(end_date, datetime.max.time())
            
            # Aggregate output VAT (sales)
            output_pipeline = [
                {
                    "$match": {
                        "organization_id": organization_id,
                        "vat_type": VATType.OUTPUT.value,
                        "transaction_date": {"$gte": start_dt, "$lte": end_dt}
                    }
                },
                {
                    "$group": {
                        "_id": None,
                        "total_base": {"$sum": "$vat_base"},
                        "total_vat": {"$sum": "$vat_amount"},
                        "count": {"$sum": 1}
                    }
                }
            ]
            
            output_result = list(self.tax_transactions.aggregate(output_pipeline))
            output_data = output_result[0] if output_result else {}
            
            # Aggregate input VAT (purchases)
            input_pipeline = [
                {
                    "$match": {
                        "organization_id": organization_id,
                        "vat_type": VATType.INPUT.value,
                        "transaction_date": {"$gte": start_dt, "$lte": end_dt}
                    }
                },
                {
                    "$group": {
                        "_id": None,
                        "total_base": {"$sum": "$vat_base"},
                        "total_vat": {"$sum": "$vat_amount"},
                        "count": {"$sum": 1}
                    }
                }
            ]
            
            input_result = list(self.tax_transactions.aggregate(input_pipeline))
            input_data = input_result[0] if input_result else {}
            
            # Aggregate by VAT rate
            rate_pipeline = [
                {
                    "$match": {
                        "organization_id": organization_id,
                        "transaction_date": {"$gte": start_dt, "$lte": end_dt},
                        "vat_rate": {"$ne": None}
                    }
                },
                {
                    "$group": {
                        "_id": {
                            "rate": "$vat_rate",
                            "type": "$vat_type"
                        },
                        "total_base": {"$sum": "$vat_base"},
                        "total_vat": {"$sum": "$vat_amount"}
                    }
                }
            ]
            
            rate_results = list(self.tax_transactions.aggregate(rate_pipeline))
            vat_by_rate = {}
            
            for item in rate_results:
                rate = str(item["_id"]["rate"])
                vat_type = item["_id"]["type"]
                
                if rate not in vat_by_rate:
                    vat_by_rate[rate] = {
                        "output_base": Decimal("0"),
                        "output_vat": Decimal("0"),
                        "input_base": Decimal("0"),
                        "input_vat": Decimal("0")
                    }
                
                if vat_type == VATType.OUTPUT.value:
                    vat_by_rate[rate]["output_base"] = Decimal(str(item["total_base"]))
                    vat_by_rate[rate]["output_vat"] = Decimal(str(item["total_vat"]))
                else:
                    vat_by_rate[rate]["input_base"] = Decimal(str(item["total_base"]))
                    vat_by_rate[rate]["input_vat"] = Decimal(str(item["total_vat"]))
            
            # Create summary
            output_vat_base = Decimal(str(output_data.get("total_base", 0)))
            output_vat_amount = Decimal(str(output_data.get("total_vat", 0)))
            input_vat_base = Decimal(str(input_data.get("total_base", 0)))
            input_vat_amount = Decimal(str(input_data.get("total_vat", 0)))
            
            vat_payable = output_vat_amount - input_vat_amount
            
            summary = VATSummary(
                period_start=start_date,
                period_end=end_date,
                output_vat_base=output_vat_base,
                output_vat_amount=output_vat_amount,
                output_transactions_count=output_data.get("count", 0),
                input_vat_base=input_vat_base,
                input_vat_amount=input_vat_amount,
                input_transactions_count=input_data.get("count", 0),
                vat_payable=vat_payable,
                vat_by_rate=vat_by_rate
            )
            
            logger.info(f"VAT summary calculated: {vat_payable} payable for period {start_date} to {end_date}")
            return summary
            
        except Exception as e:
            logger.error(f"Error calculating VAT summary: {e}")
            raise
    
    def _sum_irpf_base(
        self,
        organization_id: str,
        irpf_type: str,
        start_dt: datetime,
        end_dt: datetime,
        *,
        end_exclusive: bool = False,
    ) -> Decimal:
        end_op = "$lt" if end_exclusive else "$lte"
        result = list(self.tax_transactions.aggregate([
            {
                "$match": {
                    "organization_id": organization_id,
                    "irpf_type": irpf_type,
                    "transaction_date": {"$gte": start_dt, end_op: end_dt},
                }
            },
            {"$group": {"_id": None, "total": {"$sum": "$irpf_base"}}},
        ]))
        return Decimal(str(result[0]["total"])) if result else Decimal("0")

    def _prior_130_payments(
        self,
        user_id: str,
        organization_id: str,
        year: int,
        quarter: int,
    ) -> Decimal:
        """Sum earlier 130 irpf_payable amounts already stored this year."""
        if quarter <= 1:
            return Decimal("0")
        ids = {str(user_id), str(organization_id)}
        prior = Decimal("0")
        for report in self.db["tax_reports"].find({
            "modelo": "130",
            "year": year,
            "user_id": {"$in": list(ids)},
        }):
            report_q = str(report.get("quarter") or "")
            qn = 0
            if report_q.startswith("Q") and report_q[1:].isdigit():
                qn = int(report_q[1:])
            elif report_q.isdigit():
                qn = int(report_q)
            if 1 <= qn < quarter:
                prior += Decimal(str((report.get("results") or {}).get("irpf_payable") or 0))
        return prior.quantize(Decimal("0.01"))

    def calculate_irpf_summary(
        self,
        organization_id: str,
        start_date: date,
        end_date: date,
        quarter: int,
        irpf_rate: Optional[Decimal] = None,
        user_id: Optional[str] = None,
    ) -> IRPFSummary:
        """
        Modelo 130 for a quarter using year-to-date income/expenses.
        IRPF to pay = max(0, YTD net × régimen rate − prior 130 payments)
        """
        try:
            profile_id = user_id or organization_id
            try:
                profile = get_canonical_fiscal_profile(
                    self.db["users"], self.db["census_data"], profile_id
                )
            except Exception:
                profile = None
            if irpf_rate is None:
                irpf_rate = Decimal(str(resolve_modelo_130_rate_percent(profile, start_date.year)))

            year_start = date(start_date.year, 1, 1)
            ytd_start = datetime.combine(year_start, datetime.min.time())
            end_dt = datetime.combine(end_date, datetime.max.time())

            gross_income = self._sum_irpf_base(
                organization_id, IRPFType.INCOME.value, ytd_start, end_dt
            )
            deductible_expenses = self._sum_irpf_base(
                organization_id, IRPFType.EXPENSE.value, ytd_start, end_dt
            )
            net_income = gross_income - deductible_expenses

            previous_income = Decimal("0")
            previous_irpf = self._prior_130_payments(
                profile_id, organization_id, start_date.year, quarter
            )
            if quarter > 1:
                prev_end_dt = datetime.combine(start_date, datetime.min.time())
                previous_income = self._sum_irpf_base(
                    organization_id, IRPFType.INCOME.value, ytd_start, prev_end_dt,
                    end_exclusive=True,
                )
                if previous_irpf == 0:
                    prev_expenses = self._sum_irpf_base(
                        organization_id, IRPFType.EXPENSE.value, ytd_start, prev_end_dt,
                        end_exclusive=True,
                    )
                    prev_net = previous_income - prev_expenses
                    previous_irpf = Decimal(str(modelo_130_payable(
                        float(prev_net), float(irpf_rate) / 100.0
                    ))).quantize(Decimal("0.01"))

            irpf_to_pay = Decimal(str(modelo_130_payable(
                float(net_income),
                float(irpf_rate) / 100.0,
                0.0,
                float(previous_irpf),
            ))).quantize(Decimal("0.01"))

            summary = IRPFSummary(
                period_start=start_date,
                period_end=end_date,
                quarter=quarter,
                gross_income=gross_income,
                deductible_expenses=deductible_expenses,
                net_income=net_income,
                irpf_rate=irpf_rate,
                irpf_payable=irpf_to_pay,
                previous_quarters_income=previous_income,
                previous_quarters_irpf=previous_irpf,
                irpf_to_pay=irpf_to_pay,
            )

            logger.info(f"IRPF summary calculated: {irpf_to_pay} to pay for Q{quarter}")
            return summary

        except Exception as e:
            logger.error(f"Error calculating IRPF summary: {e}")
            raise
    
    def create_tax_transaction_from_ledger(
        self,
        ledger_entry_id: str,
        organization_id: str
    ) -> Optional[str]:
        """
        Create a tax transaction from a ledger entry
        Automatically detects VAT and IRPF based on account codes
        """
        try:
            ledger_entry = self.ledger.find_one({"_id": ObjectId(ledger_entry_id)})
            
            if not ledger_entry:
                logger.warning(f"Ledger entry {ledger_entry_id} not found")
                return None
            
            account_code = ledger_entry.get("account_code", "")
            amount = Decimal(str(ledger_entry.get("amount", 0)))
            entry_type = ledger_entry.get("entry_type", "")
            
            # Get AI-determined Modelo ID from ledger entry
            ai_modelo_id = ledger_entry.get("ai_modelo_id")
            ai_modelo_confidence = ledger_entry.get("ai_modelo_confidence", 0.0)
            
            tax_transaction = {
                "organization_id": organization_id,
                "transaction_date": ledger_entry.get("transaction_date", datetime.utcnow()),
                "ledger_entry_id": ledger_entry_id,
                "voucher_id": ledger_entry.get("voucher_id"),
                "journal_entry_id": ledger_entry.get("journal_entry_id"),
                "description": ledger_entry.get("description"),
                "modelo_ids": [ai_modelo_id] if ai_modelo_id else [],
                "ai_modelo_id": ai_modelo_id,
                "ai_modelo_confidence": ai_modelo_confidence,
                "created_at": datetime.utcnow()
            }
            
            # VAT detection (account codes 4770-4779 for output, 4720-4729 for input)
            if account_code.startswith("477"):  # Output VAT (sales)
                tax_transaction["vat_type"] = VATType.OUTPUT.value
                tax_transaction["vat_amount"] = float(amount)
                vat_rate = self._detect_vat_rate(account_code)
                tax_transaction["vat_rate"] = float(vat_rate)
                tax_transaction["vat_base"] = float(self._calculate_vat_base(amount, vat_rate))
                
            elif account_code.startswith("472"):  # Input VAT (purchases)
                tax_transaction["vat_type"] = VATType.INPUT.value
                tax_transaction["vat_amount"] = float(amount)
                vat_rate = self._detect_vat_rate(account_code)
                tax_transaction["vat_rate"] = float(vat_rate)
                tax_transaction["vat_base"] = float(self._calculate_vat_base(amount, vat_rate))
            
            # IRPF detection (account codes 4750-4759)
            if account_code.startswith("475"):  # IRPF retention
                tax_transaction["irpf_type"] = IRPFType.RETENTION.value
                tax_transaction["irpf_amount"] = float(amount)
                irpf_rate = Decimal("15")  # Default IRPF rate
                tax_transaction["irpf_rate"] = float(irpf_rate)
                tax_transaction["irpf_base"] = float(self._calculate_irpf_base(amount, irpf_rate))
            
            # Income detection (revenue accounts 4000-4999)
            elif account_code.startswith("4") and entry_type == "credit":
                tax_transaction["irpf_type"] = IRPFType.INCOME.value
                tax_transaction["irpf_base"] = float(amount)
            
            # Expense detection (expense accounts 5000-6999)
            elif account_code.startswith(("5", "6")) and entry_type == "debit":
                tax_transaction["irpf_type"] = IRPFType.EXPENSE.value
                tax_transaction["irpf_base"] = float(amount)
            
            # Only create if tax-relevant
            if tax_transaction.get("vat_type") or tax_transaction.get("irpf_type"):
                existing_tx = self.tax_transactions.find_one({"ledger_entry_id": ledger_entry_id})
                if existing_tx:
                    update_fields = {
                        key: value for key, value in tax_transaction.items() if key != "created_at"
                    }
                    update_fields["updated_at"] = datetime.utcnow()
                    self.tax_transactions.update_one(
                        {"_id": existing_tx["_id"]},
                        {"$set": update_fields},
                    )
                    logger.info(
                        f"Updated tax transaction {existing_tx['_id']} from ledger entry {ledger_entry_id}"
                    )
                    return str(existing_tx["_id"])
                result = self.tax_transactions.insert_one(tax_transaction)
                logger.info(f"Created tax transaction {result.inserted_id} from ledger entry {ledger_entry_id}")
                return str(result.inserted_id)
            
            return None
            
        except Exception as e:
            logger.error(f"Error creating tax transaction: {e}")
            return None
    
    def _detect_vat_rate(self, account_code: str) -> Decimal:
        """Detect VAT rate from account code"""
        # Spanish chart of accounts convention
        # 4770 = 21%, 4771 = 10%, 4772 = 4%
        if account_code.endswith("0"):
            return Decimal("21")
        elif account_code.endswith("1"):
            return Decimal("10")
        elif account_code.endswith("2"):
            return Decimal("4")
        return Decimal("21")  # Default
    
    def _calculate_vat_base(self, vat_amount: Decimal, vat_rate: Decimal) -> Decimal:
        """Calculate base amount from VAT amount"""
        if vat_rate == 0:
            return Decimal("0")
        return (vat_amount * Decimal("100") / vat_rate).quantize(Decimal("0.01"))
    
    def _calculate_irpf_base(self, irpf_amount: Decimal, irpf_rate: Decimal) -> Decimal:
        """Calculate base amount from IRPF amount"""
        if irpf_rate == 0:
            return Decimal("0")
        return (irpf_amount * Decimal("100") / irpf_rate).quantize(Decimal("0.01"))
    
    def get_modelo_calculation(
        self,
        organization_id: str,
        modelo_no: str,
        start_date: date,
        end_date: date
    ) -> Optional[ModeloCalculation]:
        """Get calculated values for a specific modelo"""
        try:
            modelo = self.modelos.find_one({"modelo_no": modelo_no})
            
            if not modelo:
                logger.warning(f"Modelo {modelo_no} not found")
                return None
            
            calculation = ModeloCalculation(
                modelo_id=str(modelo["_id"]),
                modelo_no=modelo_no,
                modelo_name=modelo.get("name", ""),
                period_start=start_date,
                period_end=end_date,
                period_type=TaxPeriod.MONTHLY  # Default, should be from modelo config
            )
            
            # Modelo 303 - VAT
            if modelo_no == "303":
                calculation.vat_summary = self.calculate_vat_summary(
                    organization_id, start_date, end_date
                )
            
            # Modelo 130 - IRPF
            elif modelo_no == "130":
                quarter = self._get_quarter(end_date)
                calculation.irpf_summary = self.calculate_irpf_summary(
                    organization_id, start_date, end_date, quarter
                )
            
            return calculation
            
        except Exception as e:
            logger.error(f"Error getting modelo calculation: {e}")
            return None
    
    def _get_quarter(self, date_obj: date) -> int:
        """Get quarter number from date"""
        return (date_obj.month - 1) // 3 + 1
