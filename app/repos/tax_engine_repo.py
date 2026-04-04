"""
Tax Engine Repository
Handles tax_reports collection and ledger entry queries for tax calculation.
"""

import os
import certifi
import logging
from datetime import datetime
from typing import Optional, List
from bson import ObjectId
from pymongo import MongoClient, ASCENDING, DESCENDING
from dotenv import load_dotenv

from app.models.tax_engine import TaxReport, TaxReportStatus, Quarter

load_dotenv()
logger = logging.getLogger(__name__)


class TaxEngineRepository:
    def __init__(self):
        self.client = MongoClient(os.getenv("MONGO_URI"), tlsCAFile=certifi.where())
        self.db = self.client[os.getenv("DB_NAME")]
        self.tax_reports = self.db["tax_reports"]
        # OCR-processed invoices live in "ledger" (written by ocr.py)
        self.ledger = self.db["ledger"]
        # Double-entry accounting entries live in "ledger_entries" (written by accounting_repo.py)
        self.ledger_entries = self.db["ledger_entries"]
        self._create_indexes()

    def _create_indexes(self):
        try:
            self.tax_reports.create_index(
                [("user_id", ASCENDING), ("modelo", ASCENDING),
                 ("year", ASCENDING), ("quarter", ASCENDING)],
                unique=True,
                name="unique_tax_report"
            )
            self.tax_reports.create_index([("user_id", ASCENDING), ("status", ASCENDING)])
        except Exception as e:
            logger.warning(f"Index creation warning: {e}")

    # ─────────────────── ledger queries ─────────────────────────────────────

    def get_classified_entries_for_modelo(
        self, user_id: str, modelo_id: str,
        start_date: datetime, end_date: datetime
    ) -> List[dict]:
        """
        Fetch ledger entries that the Classification Layer has tagged
        with this specific modelo_id, within the quarter window.
        """
        return list(self.ledger.find({
            "user_id": user_id,
            "processing_status": "success",
            "tax_classification.modelo_ids": modelo_id,
            "created_at": {"$gte": start_date, "$lte": end_date},
        }))

    def get_ocr_ledger_entries_for_period(
        self, user_id: str, start_date: datetime, end_date: datetime
    ) -> List[dict]:
        """
        Fetch all successfully processed OCR ledger entries for a period.
        Used as fallback when no modelo_id filter is needed.
        """
        return list(self.ledger.find({
            "user_id": user_id,
            "processing_status": "success",
            "created_at": {"$gte": start_date, "$lte": end_date},
        }))

    def get_accounting_ledger_entries_for_period(
        self, organization_id: str, start_date: datetime, end_date: datetime
    ) -> List[dict]:
        """
        Fetch double-entry ledger entries from 'ledger_entries' collection.
        Used as a secondary source when accounting module is in use.
        """
        return list(self.ledger_entries.find({
            "organization_id": organization_id,
            "transaction_date": {"$gte": start_date, "$lte": end_date},
        }))

    # ─────────────────── tax_reports CRUD ───────────────────────────────────

    def upsert_tax_report(self, report: TaxReport) -> str:
        """Insert or replace a tax report for the same user/modelo/year/quarter."""
        doc = report.model_dump(by_alias=False, exclude={"id"})
        doc["calculated_at"] = datetime.utcnow()

        result = self.tax_reports.find_one_and_update(
            {
                "user_id": report.user_id,
                "modelo": report.modelo,
                "year": report.year,
                "quarter": report.quarter,
            },
            {"$set": doc},
            upsert=True,
            return_document=True,
        )
        return str(result["_id"])

    def get_tax_report(
        self, user_id: str, modelo: str, year: int, quarter: Quarter
    ) -> Optional[dict]:
        doc = self.tax_reports.find_one({
            "user_id": user_id,
            "modelo": modelo,
            "year": year,
            "quarter": str(quarter),
        })
        if doc:
            doc["_id"] = str(doc["_id"])
        return doc

    def list_tax_reports(self, user_id: str, modelo: Optional[str] = None) -> List[dict]:
        query = {"user_id": user_id}
        if modelo:
            query["modelo"] = modelo
        docs = list(self.tax_reports.find(query).sort("year", DESCENDING))
        for d in docs:
            d["_id"] = str(d["_id"])
        return docs

    def list_tax_reports_by_modelo_no(
        self, user_id: str, modelo_no: str, year: int
    ) -> List[dict]:
        """Fetch all quarterly reports for a given modelo number and year."""
        docs = list(self.tax_reports.find({
            "user_id": user_id,
            "modelo": modelo_no,
            "year": year,
        }))
        for d in docs:
            d["_id"] = str(d["_id"])
        return docs

    def update_status(self, report_id: str, status: TaxReportStatus) -> bool:
        update = {"status": status}
        if status == TaxReportStatus.FINALIZED:
            update["finalized_at"] = datetime.utcnow()
        elif status == TaxReportStatus.FILED:
            update["filed_at"] = datetime.utcnow()
        result = self.tax_reports.update_one(
            {"_id": ObjectId(report_id)},
            {"$set": update}
        )
        return result.modified_count > 0
