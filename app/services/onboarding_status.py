"""
Single source of truth for onboarding progress.

Fiscal profile (Spain) is complete only when NIF/NIE, IAE code, and VAT regime
are present on the latest census record — not merely because a file was uploaded.
"""

from datetime import datetime
from typing import Any, Dict, Optional

from pymongo.collection import Collection

SPAIN = "ES"


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


def latest_census_record(census_collection: Collection, user_id: str) -> Optional[dict]:
    # Prefer the explicit canonical profile pointer carried on the user in callers
    # that pass a full user document; this helper remains the legacy fallback.
    return census_collection.find_one(
        {"user_id": str(user_id)},
        sort=[("updated_at", -1), ("created_at", -1)],
    )


def is_census_file_uploaded(census_collection: Collection, user_id: str) -> bool:
    """True if a census *file* was uploaded (not a form-only save)."""
    return census_collection.find_one(
        {"user_id": str(user_id), "source": {"$ne": "form"}}
    ) is not None


def compute_onboarding_status(user: dict, census_collection: Collection) -> Dict[str, Any]:
    user_id = str(user["_id"])
    country = user.get("country")
    user_type = user.get("user_type_selection")
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

    if not country:
        step, next_action = "country_selection", "Select your country to continue"
    elif not user_type:
        step, next_action = "user_type_selection", "Select your user type to continue"
    elif fiscal_needed and not fiscal_fields_ok:
        step, next_action = "fiscal_profile", "Complete NIF/NIE, IAE and VAT regime"
    else:
        step, next_action = "completed", None

    completed = step == "completed"
    if not country:
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
        "fiscal_profile_completed": fiscal_flag,
        "census_data_uploaded": is_census_file_uploaded(census_collection, user_id),
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
        "updated_at": now,
    }
    if status["onboarding_completed"] and not user.get("onboarding_completed_at"):
        update["onboarding_completed_at"] = now
        status["completed_at"] = now
    users_collection.update_one({"_id": user["_id"]}, {"$set": update})
    return status
