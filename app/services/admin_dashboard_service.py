"""Cross-tenant snapshot for the admin dashboard (Spain filing funnel)."""

from datetime import datetime, timedelta
from typing import Any, Optional

from app.services.admin_users_service import is_admin, normalize_country, stored_user_type
from app.services.modelo_file_builder import LIVE_MODELOS

FILING_STATUSES = (
    "DRAFT",
    "CALCULATED",
    "IN_REVIEW",
    "APPROVED",
    "SUBMITTED",
    "ACCEPTED",
    "REJECTED",
)
QUEUE_LIMIT = 8
RECENT_DAYS = 7


def _has_certificate(doc: dict) -> bool:
    if doc.get("certificate_uploaded_at"):
        return True
    blob = doc.get("p12_encrypted")
    return blob is not None and blob != "" and blob != b""


def _is_test_submit(filing: dict) -> bool:
    submission = filing.get("submission") or {}
    if submission.get("test_mode") is True:
        return True
    ref = str(submission.get("reference") or "")
    return ref.upper().startswith("TEST-")


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat() + ("Z" if value.tzinfo is None else "")
    return str(value)


class AdminDashboardService:
    def __init__(self, users, tax_filings, waitlist):
        self.users = users
        self.tax_filings = tax_filings
        self.waitlist = waitlist

    def snapshot(self) -> dict:
        users = list(self._find(self.users))
        filings = list(self._find(self.tax_filings))
        wait_rows = list(self._find(self.waitlist))

        tenants = [u for u in users if not is_admin(u)]
        by_country = {"ES": 0, "IT": 0, "unset": 0}
        by_type = {"person": 0, "business": 0, "advisor": 0, "unset": 0}
        spain_onboarding = {"complete": 0, "incomplete": 0}
        spain_fiscal = {"complete": 0, "incomplete": 0}
        spain_cert = {"present": 0, "missing": 0}
        recent_cutoff = datetime.utcnow() - timedelta(days=RECENT_DAYS)
        recent_signups = 0
        queue = []

        for doc in tenants:
            country = normalize_country(doc.get("country"))
            if country == "ES":
                by_country["ES"] += 1
            elif country == "IT":
                by_country["IT"] += 1
            else:
                by_country["unset"] += 1

            user_type = stored_user_type(doc)
            if user_type in by_type:
                by_type[user_type] += 1
            else:
                by_type["unset"] += 1

            created = doc.get("created_at")
            if isinstance(created, datetime) and created >= recent_cutoff:
                recent_signups += 1

            if country != "ES":
                continue

            onboarded = bool(doc.get("onboarding_completed"))
            spain_onboarding["complete" if onboarded else "incomplete"] += 1
            fiscal = bool(doc.get("fiscal_profile_completed"))
            spain_fiscal["complete" if fiscal else "incomplete"] += 1
            cert = _has_certificate(doc)
            spain_cert["present" if cert else "missing"] += 1

            email = doc.get("email") or doc.get("name") or "Unknown"
            if not onboarded and len(queue) < QUEUE_LIMIT:
                queue.append({
                    "kind": "onboarding",
                    "title": "Onboarding incomplete",
                    "detail": email,
                    "href": "/app/users",
                })
            elif not fiscal and len(queue) < QUEUE_LIMIT:
                queue.append({
                    "kind": "fiscal_profile",
                    "title": "Fiscal profile incomplete",
                    "detail": email,
                    "href": "/app/users",
                })
            elif not cert and len(queue) < QUEUE_LIMIT:
                queue.append({
                    "kind": "certificate",
                    "title": "No AEAT certificate",
                    "detail": email,
                    "href": "/app/users",
                })

        by_status = {status: 0 for status in FILING_STATUSES}
        by_modelo = {modelo: 0 for modelo in sorted(LIVE_MODELOS)}
        by_modelo["other"] = 0
        submit_mode = {"test": 0, "live": 0, "not_submitted": 0}
        redeme_303 = 0
        quarterly_303 = 0

        for filing in filings:
            status = str(filing.get("status") or "").upper()
            if status in by_status:
                by_status[status] += 1
            modelo = str(filing.get("modelo") or "")
            if modelo in by_modelo:
                by_modelo[modelo] += 1
            else:
                by_modelo["other"] += 1
            if modelo == "303":
                if filing.get("redeme") or filing.get("month"):
                    redeme_303 += 1
                else:
                    quarterly_303 += 1

            submitted_at = filing.get("submitted_at")
            if submitted_at or status in {"SUBMITTED", "ACCEPTED", "REJECTED"}:
                if _is_test_submit(filing):
                    submit_mode["test"] += 1
                else:
                    submit_mode["live"] += 1
            else:
                submit_mode["not_submitted"] += 1

            if status == "REJECTED" and len(queue) < QUEUE_LIMIT:
                queue.append({
                    "kind": "rejected_filing",
                    "title": f"Rejected modelo {modelo or '?'}",
                    "detail": filing.get("period_key") or filing.get("quarter") or str(filing.get("year") or ""),
                    "href": "/app/users",
                })
            elif status == "DRAFT" and len(queue) < QUEUE_LIMIT:
                queue.append({
                    "kind": "stuck_draft",
                    "title": f"Draft modelo {modelo or '?'}",
                    "detail": filing.get("period_key") or filing.get("quarter") or str(filing.get("year") or ""),
                    "href": "/app/users",
                })

        waitlist = {"italy": 0, "white_label": 0, "total": len(wait_rows)}
        for row in wait_rows:
            interest = str(row.get("interest") or "").lower()
            if interest in waitlist:
                waitlist[interest] += 1

        return {
            "users": {
                "total": len(tenants),
                "admins": sum(1 for u in users if is_admin(u)),
                "by_country": by_country,
                "by_type": by_type,
                "recent_signups_7d": recent_signups,
            },
            "spain": {
                "onboarding": spain_onboarding,
                "fiscal_profile": spain_fiscal,
                "certificate": spain_cert,
            },
            "filings": {
                "total": len(filings),
                "by_status": by_status,
                "by_modelo": by_modelo,
                "submit_mode": submit_mode,
                "modelo_303": {
                    "quarterly": quarterly_303,
                    "redeme_monthly": redeme_303,
                },
            },
            "waitlist": waitlist,
            "queue": queue[:QUEUE_LIMIT],
            "generated_at": _iso(datetime.utcnow()),
            "live_modelos": sorted(LIVE_MODELOS),
        }

    @staticmethod
    def _find(collection) -> Any:
        if collection is None:
            return []
        cursor = collection.find({})
        if hasattr(cursor, "sort"):
            try:
                cursor = cursor.sort("created_at", -1)
            except TypeError:
                pass
        return cursor
