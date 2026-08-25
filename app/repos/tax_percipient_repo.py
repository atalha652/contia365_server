"""Mongo persistence for IRPF percipient (perceptor) lines."""

import os
from datetime import datetime
from typing import Optional

import certifi
from bson import ObjectId
from pymongo import ASCENDING, MongoClient, ReturnDocument


class TaxPercipientRepository:
    def __init__(self, collection=None):
        if collection is not None:
            self.rows = collection
            return
        client = MongoClient(os.getenv("MONGO_URI"), tlsCAFile=certifi.where())
        db = client[os.getenv("DB_NAME")]
        self.rows = db["tax_percipients"]
        self.rows.create_index(
            [("user_id", ASCENDING), ("year", ASCENDING), ("quarter", ASCENDING)],
            name="percipients_by_user_period",
        )

    def create(self, document: dict) -> dict:
        result = self.rows.insert_one(document)
        return self.get(str(result.inserted_id), document["user_id"])

    def get(self, row_id: str, user_id: str) -> Optional[dict]:
        if not ObjectId.is_valid(row_id):
            return None
        return self.rows.find_one({"_id": ObjectId(row_id), "user_id": str(user_id)})

    def list(
        self,
        user_id: str,
        year: Optional[int] = None,
        quarter: Optional[str] = None,
    ) -> list[dict]:
        query = {"user_id": str(user_id)}
        if year is not None:
            query["year"] = year
        if quarter:
            query["quarter"] = quarter
        return list(self.rows.find(query).sort([("full_name", 1)]))

    def update(self, row_id: str, user_id: str, values: dict) -> Optional[dict]:
        if not ObjectId.is_valid(row_id):
            return None
        values = {**values, "updated_at": datetime.utcnow()}
        return self.rows.find_one_and_update(
            {"_id": ObjectId(row_id), "user_id": str(user_id)},
            {"$set": values},
            return_document=ReturnDocument.AFTER,
        )

    def delete(self, row_id: str, user_id: str) -> bool:
        if not ObjectId.is_valid(row_id):
            return False
        result = self.rows.delete_one(
            {"_id": ObjectId(row_id), "user_id": str(user_id)}
        )
        return result.deleted_count == 1
