"""Spanish AEAT tax is only for country=ES. Italy has no tax layer (T11)."""

from typing import Optional

SPAIN = "ES"
ITALY = "IT"

ITALY_TAX_DETAIL = {
    "error": "ITALY_TAX_UNAVAILABLE",
    "description": (
        "Italian tax filing is not available yet. "
        "Spanish modelos including 303 cannot be used when country is IT."
    ),
}


class ItalyTaxUnavailableError(Exception):
    """country=IT must not use AEAT / 303 endpoints."""

    def __init__(self, detail: Optional[dict] = None):
        self.detail = detail or ITALY_TAX_DETAIL
        super().__init__(self.detail["description"])


def user_country(user: Optional[dict]) -> str:
    return str((user or {}).get("country") or "").strip().upper()


def is_italy_user(user: Optional[dict]) -> bool:
    return user_country(user) == ITALY


def assert_spanish_tax_allowed(user: Optional[dict]) -> None:
    if is_italy_user(user):
        raise ItalyTaxUnavailableError()
