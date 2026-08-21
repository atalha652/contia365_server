"""
Invoice Repository
MongoDB data access layer for invoices and invoice counters.
"""

from typing import List, Optional
from datetime import datetime
from decimal import Decimal
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.collection import Collection
from pymongo.database import Database
from bson import ObjectId
import certifi
import os
import logging
from dotenv import load_dotenv

from app.models.invoice import Invoice, InvoiceCreate, InvoiceStatus, InvoiceTotals

load_dotenv()
logger = logging.getLogger(__name__)


class InvoiceRepository:
    def __init__(self, db: Optional[Database] = None):
        if db is not None:
            self.db = db
        else:
            mongo_uri = os.getenv("MONGO_URI")
            db_name = os.getenv("DB_NAME")
            client = MongoClient(mongo_uri, tlsCAFile=certifi.where())
            self.db = client[db_name]

        self.invoices: Collection = self.db["invoices"]
        self.counters: Collection = self.db["invoice_counters"]
        self._create_indexes()

    def _create_indexes(self):
        try:
            # Drop the old sparse index if it exists (created without partialFilterExpression)
            try:
                self.invoices.drop_index("organization_id_1_invoice_number_1")
            except Exception:
                pass  # index didn't exist — that's fine

            # Unique invoice number per org — only enforced when invoice_number is assigned
            # (partial filter excludes drafts where invoice_number is null)
            self.invoices.create_index(
                [("organization_id", ASCENDING), ("invoice_number", ASCENDING)],
                unique=True,
                partialFilterExpression={"invoice_number": {"$type": "string"}},
            )
            self.invoices.create_index([("organization_id", ASCENDING), ("status", ASCENDING)])
            self.invoices.create_index([("organization_id", ASCENDING), ("source_voucher_id", ASCENDING)])
            self.invoices.create_index([("organization_id", ASCENDING), ("issued_at", DESCENDING)])
            # Counters: unique per org + series
            self.counters.create_index(
                [("organization_id", ASCENDING), ("series", ASCENDING)],
                unique=True,
            )
        except Exception as e:
            logger.warning(f"Index creation warning: {e}")

    # ==================== HELPERS ====================

    def _to_doc(self, invoice: Invoice) -> dict:
        """Convert Invoice model to MongoDB-safe dict."""
        doc = invoice.dict(by_alias=True, exclude_none=False)
        # Remove _id so MongoDB generates it (or keep if updating)
        doc.pop("_id", None)
        # Convert Decimals to float
        return self._decimals_to_float(doc)

    def _decimals_to_float(self, data):
        if isinstance(data, dict):
            return {k: self._decimals_to_float(v) for k, v in data.items()}
        if isinstance(data, list):
            return [self._decimals_to_float(i) for i in data]
        if isinstance(data, Decimal):
            return float(data)
        return data

    def _from_doc(self, doc: dict) -> Optional[Invoice]:
        if doc is None:
            return None
        doc["_id"] = str(doc["_id"])
        return Invoice(**doc)

    # ==================== COUNTER (atomic, gap-free) ====================

    def next_invoice_number(self, organization_id: str, series: str) -> str:
        """
        Atomically increment the counter for (org, series) and return
        the formatted invoice number, e.g. A-000001.
        Uses findOneAndUpdate with upsert=True for atomicity.
        """
        result = self.counters.find_one_and_update(
            {"organization_id": organization_id, "series": series},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=True,  # return the updated document
        )
        seq: int = result["seq"]
        return f"{series}-{seq:06d}"

    # ==================== CRUD ====================

    def create(self, invoice: Invoice) -> Invoice:
        doc = self._to_doc(invoice)
        doc["created_at"] = datetime.utcnow()
        doc["updated_at"] = datetime.utcnow()
        result = self.invoices.insert_one(doc)
        doc["_id"] = str(result.inserted_id)
        return Invoice(**doc)

    def get_by_id(self, organization_id: str, invoice_id: str) -> Optional[Invoice]:
        doc = self.invoices.find_one({
            "_id": ObjectId(invoice_id),
            "organization_id": organization_id,
        })
        return self._from_doc(doc)

    def get_by_voucher(self, organization_id: str, voucher_id: str) -> Optional[Invoice]:
        doc = self.invoices.find_one({
            "organization_id": organization_id,
            "source_voucher_id": voucher_id,
        })
        return self._from_doc(doc)

    def list_by_org(
        self,
        organization_id: str,
        status: Optional[InvoiceStatus] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Invoice]:
        query = {"organization_id": organization_id}
        if status:
            query["status"] = status.value
        cursor = (
            self.invoices.find(query)
            .sort("issued_at", DESCENDING)
            .skip(offset)
            .limit(limit)
        )
        return [self._from_doc(doc) for doc in cursor]

    def get_last_fingerprint(self, organization_id: str) -> str:
        """
        Return the fingerprint of the most recently issued invoice for this org.
        Returns the genesis constant "0" if no issued invoices exist yet.
        """
        doc = self.invoices.find_one(
            {
                "organization_id": organization_id,
                "status": InvoiceStatus.ISSUED.value,
                "fingerprint": {"$exists": True, "$ne": None},
            },
            sort=[("issued_at", DESCENDING)],
        )
        if doc and doc.get("fingerprint"):
            return doc["fingerprint"]
        return "0"  # genesis constant — first invoice in the chain

    def issue(
        self,
        organization_id: str,
        invoice_id: str,
        invoice_number: str,
        totals: InvoiceTotals,
        ledger_entry_id: str,
        fingerprint: str,
        previous_fingerprint: str,
        issued_at: datetime,
    ) -> Optional[Invoice]:
        """
        Atomically transition a DRAFT invoice to ISSUED.
        Only succeeds if current status is 'draft' (optimistic lock).
        """
        update = {
            "$set": {
                "status": InvoiceStatus.ISSUED.value,
                "invoice_number": invoice_number,
                "totals.subtotal": float(totals.subtotal),
                "totals.total_vat": float(totals.total_vat),
                "totals.total_amount": float(totals.total_amount),
                "ledger_entry_id": ledger_entry_id,
                "fingerprint": fingerprint,
                "previous_fingerprint": previous_fingerprint,
                "issued_at": issued_at,
                "updated_at": datetime.utcnow(),
            }
        }
        doc = self.invoices.find_one_and_update(
            {
                "_id": ObjectId(invoice_id),
                "organization_id": organization_id,
                "status": InvoiceStatus.DRAFT.value,  # guard: only draft can be issued
            },
            update,
            return_document=True,
        )
        return self._from_doc(doc)

    def update_draft(
        self,
        organization_id: str,
        invoice_id: str,
        fields: dict,
    ) -> Optional[Invoice]:
        """
        Update mutable fields on a DRAFT invoice.
        The status guard ensures issued/cancelled invoices are never touched.
        """
        fields["updated_at"] = datetime.utcnow()
        doc = self.invoices.find_one_and_update(
            {
                "_id": ObjectId(invoice_id),
                "organization_id": organization_id,
                "status": InvoiceStatus.DRAFT.value,
            },
            {"$set": fields},
            return_document=True,
        )
        return self._from_doc(doc)

    def get_chain_for_verification(self, organization_id: str) -> List[dict]:
        """
        Return all issued invoices ordered by issued_at ASC for chain verification.
        Only returns the fields needed for hash recomputation.
        """
        cursor = self.invoices.find(
            {
                "organization_id": organization_id,
                "status": InvoiceStatus.ISSUED.value,
            },
            {
                "_id": 1,
                "invoice_number": 1,
                "totals": 1,
                "customer": 1,
                "issued_at": 1,
                "fingerprint": 1,
                "previous_fingerprint": 1,
            },
        ).sort("issued_at", ASCENDING)
        return list(cursor)

    def cancel(
        self,
        organization_id: str,
        invoice_id: str,
        reason: str,
    ) -> Optional[Invoice]:
        """Cancel an issued invoice (creates audit trail, never deletes)."""
        doc = self.invoices.find_one_and_update(
            {
                "_id": ObjectId(invoice_id),
                "organization_id": organization_id,
                "status": InvoiceStatus.ISSUED.value,
            },
            {
                "$set": {
                    "status": InvoiceStatus.CANCELLED.value,
                    "cancellation_reason": reason,
                    "cancelled_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow(),
                }
            },
            return_document=True,
        )
        return self._from_doc(doc)
