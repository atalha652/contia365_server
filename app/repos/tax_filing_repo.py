"""MongoDB persistence for first-class tax filings."""

import certifi
import os
from typing import Optional

from bson import ObjectId
from pymongo import ASCENDING, MongoClient, ReturnDocument


class TaxFilingRepository:
    def __init__(self):
        client = MongoClient(os.getenv("MONGO_URI"), tlsCAFile=certifi.where())
        self.db = client[os.getenv("DB_NAME")]
        self.filings = self.db["tax_filings"]
        self.filings.create_index(
            [
                ("user_id", ASCENDING),
                ("modelo", ASCENDING),
                ("year", ASCENDING),
                ("period_key", ASCENDING),
            ],
            unique=True,
            name="one_tax_filing_per_user_modelo_period",
        )
        self.filings.create_index(
            [("user_id", ASCENDING), ("status", ASCENDING)],
            name="tax_filings_by_user_status",
        )

    def create(self, document: dict) -> dict:
        result = self.filings.insert_one(document)
        return self.get_by_id(str(result.inserted_id), document["user_id"])

    def get_by_id(self, filing_id: str, user_id: str) -> Optional[dict]:
        if not ObjectId.is_valid(filing_id):
            return None
        return self.filings.find_one(
            {"_id": ObjectId(filing_id), "user_id": str(user_id)}
        )

    def get_by_period(
        self, user_id: str, modelo: str, year: int, period_key: str
    ) -> Optional[dict]:
        return self.filings.find_one({
            "user_id": str(user_id),
            "modelo": str(modelo),
            "year": year,
            "period_key": period_key,
        })

    def list(
        self,
        user_id: str,
        status: Optional[str] = None,
        year: Optional[int] = None,
        modelo: Optional[str] = None,
    ) -> list[dict]:
        query = {"user_id": str(user_id)}
        if status:
            query["status"] = status
        if year is not None:
            query["year"] = year
        if modelo:
            query["modelo"] = modelo
        return list(self.filings.find(query).sort([("year", -1), ("period_key", 1)]))

    def update(self, filing_id: str, user_id: str, update: dict) -> Optional[dict]:
        if not ObjectId.is_valid(filing_id):
            return None
        return self.filings.find_one_and_update(
            {"_id": ObjectId(filing_id), "user_id": str(user_id)},
            update,
            return_document=ReturnDocument.AFTER,
        )

