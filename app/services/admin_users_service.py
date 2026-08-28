"""Admin directory of Contia users (not Outlook / waitlist)."""

from datetime import datetime
from typing import Any, Optional

from app.services.user_type_vocab import canonicalize_user_type

ADMIN_PAGE_SIZE = 10
ADMIN_PAGES = [
    {"id": "users", "label": "Users", "path": "/admin/users"},
    {"id": "sales", "label": "Sales", "path": "/admin/sales"},
]

_LIST_EXCLUDE = frozenset({
    "password",
    "password_hash",
    "p12_encrypted",
    "gmail_credentials",
    "certificate",
})


def is_admin(user: Optional[dict]) -> bool:
    return str((user or {}).get("role") or "").strip().lower() == "admin"


def normalize_country(value: Any) -> Optional[str]:
    if value is None:
        return None
    key = str(value).strip().upper()
    if not key:
        return None
    if key in {"ES", "SPAIN", "ESP", "ESPAÑA", "ESPANA"}:
        return "ES"
    if key in {"IT", "ITALY", "ITA", "ITALIA"}:
        return "IT"
    return key


def parse_country_filter(value: Optional[str]) -> Optional[str]:
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.lower() == "unset":
        return "unset"
    code = normalize_country(raw)
    if code in {"ES", "IT"}:
        return code
    raise ValueError("country must be ES, IT, or unset")


def parse_user_type_filter(value: Optional[str]) -> Optional[str]:
    raw = (value or "").strip().lower()
    if not raw:
        return None
    if raw == "unset":
        return "unset"
    canon = canonicalize_user_type(raw)
    if canon in {"person", "business", "advisor"}:
        return canon
    raise ValueError("user_type must be person, business, advisor, or unset")


def stored_user_type(doc: dict) -> Optional[str]:
    return (
        canonicalize_user_type(doc.get("user_type_selection"))
        or canonicalize_user_type(doc.get("user_type"))
        or canonicalize_user_type(doc.get("type"))
    )


def _matches(doc: dict, country: Optional[str], user_type: Optional[str]) -> bool:
    if country:
        stored = normalize_country(doc.get("country"))
        if country == "unset":
            if stored is not None:
                return False
        elif stored != country:
            return False
    if user_type:
        stored_type = stored_user_type(doc)
        if user_type == "unset":
            if stored_type is not None:
                return False
        elif stored_type != user_type:
            return False
    return True


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat() + ("Z" if value.tzinfo is None else "")
    return str(value)


def serialize_user(doc: dict) -> dict:
    org = doc.get("organization_info") or {}
    return {
        "id": str(doc.get("_id")),
        "name": doc.get("name"),
        "email": doc.get("email"),
        "phone": doc.get("phone"),
        "country": normalize_country(doc.get("country")),
        "user_type": stored_user_type(doc),
        "role": str(doc.get("role") or "user").strip().lower() or "user",
        "company_name": doc.get("company_name") or org.get("company_name"),
        "onboarding_completed": bool(doc.get("onboarding_completed")),
        "created_at": _iso(doc.get("created_at")),
    }


def parse_page(value: Optional[int]) -> int:
    try:
        page = int(value or 1)
    except (TypeError, ValueError):
        page = 1
    return max(1, page)


def parse_page_size(value: Optional[int]) -> int:
    try:
        size = int(value if value is not None else ADMIN_PAGE_SIZE)
    except (TypeError, ValueError):
        size = ADMIN_PAGE_SIZE
    return max(1, min(size, ADMIN_PAGE_SIZE))


def pagination_meta(total: int, page: int, page_size: int) -> dict:
    total_pages = (total + page_size - 1) // page_size if total else 0
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_prev": page > 1 and total_pages > 0,
    }


class AdminUsersService:
    def __init__(self, collection):
        self.collection = collection

    def list(
        self,
        *,
        country: Optional[str] = None,
        user_type: Optional[str] = None,
        page: Optional[int] = 1,
        page_size: Optional[int] = ADMIN_PAGE_SIZE,
    ) -> dict:
        country_f = parse_country_filter(country)
        type_f = parse_user_type_filter(user_type)
        page_n = parse_page(page)
        size = parse_page_size(page_size)

        cursor = self.collection.find({}, {field: 0 for field in _LIST_EXCLUDE})
        if hasattr(cursor, "sort"):
            cursor = cursor.sort("created_at", -1)

        matched = []
        for doc in cursor:
            if not _matches(doc, country_f, type_f):
                continue
            matched.append(serialize_user(doc))

        start = (page_n - 1) * size
        meta = pagination_meta(len(matched), page_n, size)
        return {
            "users": matched[start:start + size],
            **meta,
        }
