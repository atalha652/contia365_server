"""Canonical onboarding user types (T13).

UI and API: person / business. Advisor is legacy only.
Old DB values freelancer / company are aliases until migrated.
White Label is waitlist interest, not a stored user type.
"""

from enum import Enum
from typing import Optional

PERSON = "person"
BUSINESS = "business"
ADVISOR = "advisor"

USER_TYPE_ALIASES = {
    "person": PERSON,
    "freelancer": PERSON,
    "autonomo": PERSON,
    "autónomo": PERSON,
    "individual": PERSON,
    "business": BUSINESS,
    "company": BUSINESS,
    "empresa": BUSINESS,
    "organization": BUSINESS,
    "advisor": ADVISOR,
    "asesor": ADVISOR,
}


def canonicalize_user_type(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, Enum):
        value = value.value
    key = str(value).strip().lower()
    if not key:
        return None
    return USER_TYPE_ALIASES.get(key)


def is_person_user_type(value) -> bool:
    return canonicalize_user_type(value) == PERSON


def is_business_user_type(value) -> bool:
    return canonicalize_user_type(value) == BUSINESS


def stored_account_kind(value) -> Optional[str]:
    """users.type is individual/organization; onboarding ids are person/business/advisor."""
    canon = canonicalize_user_type(value)
    if canon == PERSON:
        return "individual"
    if canon in (BUSINESS, ADVISOR):
        return "organization"
    return None


def migrate_user_config(config: Optional[dict]) -> dict:
    """Rewrite leftover freelancer/company keys on users.user_config."""
    result = dict(config or {})
    layout = result.get("dashboard_layout")
    if layout == "freelancer":
        result["dashboard_layout"] = PERSON
    elif layout == "company":
        result["dashboard_layout"] = BUSINESS
    coa = result.get("chart_of_accounts")
    if coa == "freelancer_coa":
        result["chart_of_accounts"] = "person_coa"
    elif coa == "company_coa":
        result["chart_of_accounts"] = "business_coa"
    regime = result.get("tax_regime")
    if regime == "company":
        result["tax_regime"] = BUSINESS
    return result


_LEGACY_TYPE_FIELD = frozenset({
    "freelancer", "company", "person", "business", "advisor",
})


def migrate_user_document(doc: dict) -> dict:
    """Fields to $set on a users row. Empty dict means already canonical."""
    selection = doc.get("user_type_selection")
    canon = canonicalize_user_type(selection)
    update = {}
    if canon and selection != canon:
        update["user_type_selection"] = canon
    kind = stored_account_kind(canon or selection)
    if kind and doc.get("type") in _LEGACY_TYPE_FIELD:
        update["type"] = kind
    org_type = (doc.get("organization_info") or {}).get("type")
    if org_type == "company":
        update["organization_info.type"] = BUSINESS
    config = migrate_user_config(doc.get("user_config") or {})
    if config != (doc.get("user_config") or {}):
        update["user_config"] = config
    return update


def migrate_census_document(doc: dict) -> dict:
    current = doc.get("user_type")
    canon = canonicalize_user_type(current)
    if canon and current != canon:
        return {"user_type": canon}
    return {}
