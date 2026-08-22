"""Canonical Spain fiscal-profile access and derived tax configuration."""

from datetime import datetime
from typing import Dict, Iterable, List, Optional

from bson import ObjectId
from pymongo.collection import Collection


def get_canonical_fiscal_profile(
    users: Collection,
    profiles: Collection,
    user_id,
) -> Optional[dict]:
    """Resolve users.fiscal_profile_id, falling back to the latest legacy row."""
    oid = user_id if isinstance(user_id, ObjectId) else ObjectId(str(user_id))
    user = users.find_one({"_id": oid}, {"fiscal_profile_id": 1})
    profile_id = (user or {}).get("fiscal_profile_id")
    profile = None
    if profile_id and ObjectId.is_valid(str(profile_id)):
        profile = profiles.find_one(
            {"_id": ObjectId(str(profile_id)), "user_id": str(oid)}
        )
    if profile:
        return profile
    return profiles.find_one(
        {"user_id": str(oid)},
        sort=[("updated_at", -1), ("created_at", -1)],
    )


def set_canonical_profile_id(users: Collection, user_id, profile_id) -> None:
    oid = user_id if isinstance(user_id, ObjectId) else ObjectId(str(user_id))
    users.update_one(
        {"_id": oid},
        {"$set": {
            "fiscal_profile_id": str(profile_id),
            "updated_at": datetime.utcnow(),
        }},
    )


def merge_profile_data(existing: dict, incoming: dict, incoming_wins: bool = True) -> dict:
    """Recursively merge profile sections without dropping unrelated fields."""
    result = dict(existing or {})
    for key, value in (incoming or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_profile_data(result[key], value, incoming_wins)
        elif incoming_wins or key not in result or result[key] in (None, "", []):
            result[key] = value
    return result


def derive_periodic_tax_obligations(
    profile: dict,
    existing: Optional[Iterable[dict]] = None,
) -> List[Dict[str, str]]:
    """Derive baseline Spanish modelos and retain explicit census obligations."""
    by_modelo = {
        str(item["modelo"]): dict(item)
        for item in (existing or [])
        if item and item.get("modelo") and item.get("source") != "derived"
    }
    registration = profile.get("professional_registration") or {}
    vat_regime = str(registration.get("vat_regime") or "").strip().lower()
    irpf_method = str(registration.get("irpf_method") or "").strip()
    has_iae = any(
        str((activity or {}).get("code") or "").strip()
        for activity in (registration.get("economic_activities") or [])
    )

    vat_exempt = any(term in vat_regime for term in ("exento", "exempt", "no sujeto"))
    if vat_regime and not vat_exempt:
        by_modelo.setdefault("303", {
            "modelo": "303",
            "description": "Autoliquidación trimestral del IVA",
            "periodicity": "TRIMESTRAL",
            "source": "derived",
        })
        by_modelo.setdefault("390", {
            "modelo": "390",
            "description": "Resumen anual del IVA",
            "periodicity": "ANUAL",
            "source": "derived",
        })
    if irpf_method or (profile.get("user_type") == "freelancer" and has_iae):
        by_modelo.setdefault("130", {
            "modelo": "130",
            "description": "Pago fraccionado del IRPF",
            "periodicity": "TRIMESTRAL",
            "source": "derived",
        })
    return list(by_modelo.values())


def canonical_profile_updates(profile: dict) -> dict:
    """Fields recomputed whenever identity, IAE, or VAT configuration changes."""
    return {
        "periodic_tax_obligations": derive_periodic_tax_obligations(
            profile, profile.get("periodic_tax_obligations")
        ),
        "profile_updated_at": datetime.utcnow(),
        "is_canonical": True,
    }


def applicable_modelos(profile: Optional[dict]) -> set[str]:
    return {
        str(item["modelo"])
        for item in ((profile or {}).get("periodic_tax_obligations") or [])
        if item.get("modelo")
    }
