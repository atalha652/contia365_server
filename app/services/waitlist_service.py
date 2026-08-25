"""Persist product waitlist interest (T12 White Label, T11 Italy)."""

from datetime import datetime
from typing import Optional

from app.services.user_type_vocab import ADVISOR, canonicalize_user_type

WAITLIST_INTERESTS = frozenset({"white_label", "italy"})


class WaitlistError(ValueError):
    pass


def normalize_interest(interest: str) -> str:
    key = str(interest or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key not in WAITLIST_INTERESTS:
        raise WaitlistError("interest must be white_label or italy.")
    return key


def serialize_waitlist_row(document: dict) -> dict:
    result = dict(document)
    result["_id"] = str(result["_id"])
    for key, value in list(result.items()):
        if isinstance(value, datetime):
            result[key] = value.isoformat()
    return result


class WaitlistService:
    def __init__(self, collection):
        self.collection = collection
        try:
            self.collection.create_index(
                [("user_id", 1), ("interest", 1)],
                unique=True,
                name="one_waitlist_row_per_user_interest",
            )
        except Exception:
            pass

    def join(self, user: dict, interest: str, source: Optional[str] = None) -> dict:
        interest = normalize_interest(interest)
        user_id = str(user["_id"])
        existing = self.collection.find_one({"user_id": user_id, "interest": interest})
        if existing:
            return serialize_waitlist_row(existing)

        now = datetime.utcnow()
        document = {
            "user_id": user_id,
            "email": user.get("email"),
            "name": user.get("name") or user.get("full_name"),
            "country": user.get("country"),
            "interest": interest,
            "source": (source or "onboarding").strip() or "onboarding",
            "created_at": now,
            "updated_at": now,
        }
        result = self.collection.insert_one(document)
        document["_id"] = result.inserted_id
        return serialize_waitlist_row(document)

    def list_for_user(self, user_id: str) -> list[dict]:
        rows = self.collection.find({"user_id": str(user_id)}).sort("created_at", -1)
        return [serialize_waitlist_row(item) for item in rows]

    def list_all(self) -> list[dict]:
        rows = self.collection.find().sort("created_at", -1)
        return [serialize_waitlist_row(item) for item in rows]


def can_view_sales_waitlist(user: dict) -> bool:
    role = str(user.get("role") or "").lower()
    user_type = canonicalize_user_type(
        user.get("user_type_selection") or user.get("user_type")
    )
    return role == "admin" or user_type == ADVISOR
