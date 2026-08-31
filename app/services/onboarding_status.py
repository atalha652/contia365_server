"""
Single source of truth for onboarding progress.

Fiscal profile (Spain) is complete only when NIF/NIE, IAE code, and VAT regime
are present on the latest census record — not merely because a file was uploaded.

Business path: country → user_type → company_details → representative → aeat_connection → completed
Person path:   country → user_type → fiscal_profile → certificate_upload → person_aeat_connection → completed
"""

from datetime import datetime
from typing import Any, Dict, Optional

from pymongo.collection import Collection

from app.services.user_type_vocab import canonicalize_user_type

SPAIN = "ES"
BUSINESS = "business"


def _nonempty(value: Any) -> bool:
    return bool(value is not None and str(value).strip())


def is_fiscal_required(country: Optional[str]) -> bool:
    return country == SPAIN


def is_fiscal_profile_complete(record: Optional[dict]) -> bool:
    if not record:
        return False
    identity = record.get("taxpayer_identity") or {}
    prof = record.get("professional_registration") or {}
    activities = prof.get("economic_activities") or []
    has_iae = any(_nonempty((item or {}).get("code")) for item in activities)
    return (
        _nonempty(identity.get("nif_nie"))
        and _nonempty(prof.get("vat_regime"))
        and has_iae
    )


# ---------------------------------------------------------------------------
# Business path helpers
# ---------------------------------------------------------------------------

def is_business_profile_complete(user: dict) -> bool:
    """True when company legal name and CIF are both present."""
    bp = user.get("business_profile") or {}
    return _nonempty(bp.get("legal_name")) and _nonempty(bp.get("cif"))


def is_representative_complete(user: dict) -> bool:
    """True when authorized representative name, DNI/NIE and role are present."""
    rep = user.get("authorized_representative") or {}
    return (
        _nonempty(rep.get("full_name"))
        and _nonempty(rep.get("dni_nie"))
        and _nonempty(rep.get("role"))
    )


def is_aeat_connected(user: dict) -> bool:
    """
    True when AEAT connection is established for a business account
    and re-authentication is not required.
    """
    conn = user.get("aeat_connection") or {}
    return bool(conn.get("connected")) and not bool(conn.get("requires_reauth"))


# ---------------------------------------------------------------------------
# Person path helpers
# ---------------------------------------------------------------------------

def is_certificate_uploaded(user: dict) -> bool:
    """True when a valid .p12 certificate has been stored for this user."""
    return bool(user.get("certificate_info"))


def is_person_aeat_connected(user: dict) -> bool:
    """True when the individual user has confirmed apoderamiento on AEAT."""
    conn = user.get("person_aeat_connection") or {}
    return bool(conn.get("connected")) and not bool(conn.get("requires_reauth"))


# ---------------------------------------------------------------------------
# Census helpers (person path)
# ---------------------------------------------------------------------------

def latest_census_record(census_collection: Collection, user_id: str) -> Optional[dict]:
    return census_collection.find_one(
        {"user_id": str(user_id)},
        sort=[("updated_at", -1), ("created_at", -1)],
    )


def is_census_file_uploaded(census_collection: Collection, user_id: str) -> bool:
    """True if a census *file* was uploaded (not a form-only save)."""
    return census_collection.find_one(
        {"user_id": str(user_id), "source": {"$ne": "form"}}
    ) is not None


# ---------------------------------------------------------------------------
# Core status computation
# ---------------------------------------------------------------------------

def compute_onboarding_status(user: dict, census_collection: Collection) -> Dict[str, Any]:
    user_id = str(user["_id"])
    country = user.get("country")
    user_type = canonicalize_user_type(user.get("user_type_selection")) or user.get("user_type_selection")
    is_business = user_type == BUSINESS

    # ── Business-specific flags ──────────────────────────────────────────────
    biz_profile_ok = is_business_profile_complete(user)
    rep_ok = is_representative_complete(user)
    aeat_ok = is_aeat_connected(user)

    # ── Person-specific flags ────────────────────────────────────────────────
    profile_id = user.get("fiscal_profile_id")
    latest = None
    if profile_id:
        from bson import ObjectId
        if ObjectId.is_valid(str(profile_id)):
            latest = census_collection.find_one({
                "_id": ObjectId(str(profile_id)),
                "user_id": user_id,
            })
    latest = latest or latest_census_record(census_collection, user_id)
    fiscal_fields_ok = is_fiscal_profile_complete(latest)
    fiscal_needed = is_fiscal_required(country)
    cert_ok = is_certificate_uploaded(user)
    person_aeat_ok = is_person_aeat_connected(user)

    # ── Step resolution ──────────────────────────────────────────────────────
    if not country:
        step, next_action = "country_selection", "Select your country to continue"
    elif not user_type:
        step, next_action = "user_type_selection", "Select your user type to continue"
    elif is_business:
        if not biz_profile_ok:
            step, next_action = "company_details", "Enter company legal name and CIF"
        elif not rep_ok:
            step, next_action = "representative", "Enter the authorized representative details"
        elif not aeat_ok:
            step, next_action = "aeat_connection", "Connect to AEAT as authorized representative"
        else:
            step, next_action = "completed", None
    else:
        # Person path: fiscal_profile → certificate_upload → person_aeat_connection → completed
        if fiscal_needed and not fiscal_fields_ok:
            step, next_action = "fiscal_profile", "Complete NIF/NIE, IAE and VAT regime"
        elif fiscal_needed and not cert_ok:
            step, next_action = "certificate_upload", "Upload your digital certificate (.p12/.pfx)"
        elif fiscal_needed and not person_aeat_ok:
            step, next_action = "person_aeat_connection", "Grant Contia365 representation on AEAT portal"
        else:
            step, next_action = "completed", None

    completed = step == "completed"

    # ── fiscal_profile_completed flag ────────────────────────────────────────
    if not country:
        fiscal_flag = False
    elif is_business:
        fiscal_flag = False
    elif not fiscal_needed:
        fiscal_flag = True
    else:
        fiscal_flag = fiscal_fields_ok

    return {
        "user_id": user_id,
        "onboarding_completed": completed,
        "country_selected": country,
        "user_type_selected": user_type,
        "role": str(user.get("role") or "user").strip().lower() or "user",
        # Person path
        "fiscal_profile_completed": fiscal_flag,
        "census_data_uploaded": is_census_file_uploaded(census_collection, user_id),
        "certificate_uploaded": cert_ok,
        "person_aeat_connected": person_aeat_ok,
        # Business path
        "business_profile_completed": biz_profile_ok,
        "representative_completed": rep_ok,
        "aeat_connected": aeat_ok,
        # Common
        "current_step": step,
        "completed_at": user.get("onboarding_completed_at") if completed else None,
        "next_action": next_action,
    }


def persist_computed_onboarding(
    users_collection: Collection,
    census_collection: Collection,
    user: dict,
) -> Dict[str, Any]:
    """Recompute status, write flags onto the user, return the same payload as GET /status."""
    status = compute_onboarding_status(user, census_collection)
    now = datetime.utcnow()
    update = {
        "onboarding_completed": status["onboarding_completed"],
        "onboarding_step": status["current_step"],
        "fiscal_profile_completed": status["fiscal_profile_completed"],
        "certificate_uploaded": status["certificate_uploaded"],
        "person_aeat_connected": status["person_aeat_connected"],
        "business_profile_completed": status["business_profile_completed"],
        "representative_completed": status["representative_completed"],
        "aeat_connected": status["aeat_connected"],
        "updated_at": now,
    }
    canon = status.get("user_type_selected")
    if canon and canon != user.get("user_type_selection"):
        update["user_type_selection"] = canon
    if status["onboarding_completed"] and not user.get("onboarding_completed_at"):
        update["onboarding_completed_at"] = now
        status["completed_at"] = now
    users_collection.update_one({"_id": user["_id"]}, {"$set": update})
    return status
