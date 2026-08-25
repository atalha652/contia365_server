"""CRUD for employee / professional percipient lines (111 / 190)."""

from datetime import datetime
from typing import Optional

from app.models.tax_engine import Quarter
from app.repos.tax_percipient_repo import TaxPercipientRepository


def serialize_percipient(document: dict) -> dict:
    result = dict(document)
    result["id"] = str(result.pop("_id"))
    for key, value in list(result.items()):
        if isinstance(value, datetime):
            result[key] = value.isoformat()
    return result


class TaxPercipientService:
    def __init__(self, repo=None):
        self.repo = repo if repo is not None else TaxPercipientRepository()

    def create(self, user: dict, body) -> dict:
        now = datetime.utcnow()
        user_id = str(user["_id"])
        payload = body.model_dump() if hasattr(body, "model_dump") else dict(body)
        payload["nif"] = str(payload.get("nif") or "").replace(" ", "").upper()
        payload["perception_key"] = str(payload.get("perception_key") or "G").upper()[:1]
        if payload.get("quarter"):
            payload["quarter"] = Quarter(str(payload["quarter"]).upper()).value
        document = {
            **payload,
            "user_id": user_id,
            "organization_id": str(user.get("organization_id", user_id)),
            "created_at": now,
            "updated_at": now,
        }
        return serialize_percipient(self.repo.create(document))

    def list(
        self,
        user: dict,
        year: Optional[int] = None,
        quarter: Optional[str] = None,
    ) -> list[dict]:
        if quarter:
            quarter = Quarter(str(quarter).upper()).value
        return [
            serialize_percipient(row)
            for row in self.repo.list(str(user["_id"]), year, quarter)
        ]

    def update(self, row_id: str, user: dict, body) -> dict:
        values = body.model_dump(exclude_unset=True) if hasattr(body, "model_dump") else dict(body)
        if "nif" in values and values["nif"]:
            values["nif"] = str(values["nif"]).replace(" ", "").upper()
        if values.get("perception_key"):
            values["perception_key"] = str(values["perception_key"]).upper()[:1]
        if values.get("quarter"):
            values["quarter"] = Quarter(str(values["quarter"]).upper()).value
        updated = self.repo.update(row_id, str(user["_id"]), values)
        if not updated:
            raise LookupError("Percipient record not found.")
        return serialize_percipient(updated)

    def delete(self, row_id: str, user: dict) -> None:
        if not self.repo.delete(row_id, str(user["_id"])):
            raise LookupError("Percipient record not found.")
