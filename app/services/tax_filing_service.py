"""Tax filing lifecycle and calculation orchestration."""

import os
from datetime import datetime
from typing import Optional
from uuid import uuid4

from pymongo.errors import DuplicateKeyError

from app.models.tax_engine import Quarter
from app.models.tax_filing import TaxFilingStatus
from app.repos.tax_filing_repo import TaxFilingRepository
from app.services.aeat_modelo_client import AeatModeloClient, AeatModeloResponse
from app.services.aeat_result_messages import enrich_aeat_result, readable_aeat_message
from app.services.fiscal_profile_service import (
    applicable_modelos,
    get_canonical_fiscal_profile,
    obligation_periodicity,
)
from app.services.tax_period import resolve_tax_period
from app.services.justificante_pdf import build_justificante_pdf
from app.services.modelo_boe_common import ModeloFileError
from app.services.modelo_file_builder import (
    ANNUAL_FILE_MODELOS,
    LIVE_MODELOS,
    PERCIPIENT_MODELOS,
    build_modelo_file,
    filing_is_legally_complete,
)
from app.services.spain_tax_access import assert_spanish_tax_allowed
from app.services.tax_engine_service import TaxEngineService


ALLOWED_TRANSITIONS = {
    TaxFilingStatus.DRAFT: {TaxFilingStatus.CALCULATED},
    TaxFilingStatus.CALCULATED: {TaxFilingStatus.IN_REVIEW},
    TaxFilingStatus.IN_REVIEW: {TaxFilingStatus.APPROVED},
    TaxFilingStatus.APPROVED: {
        TaxFilingStatus.SUBMITTED,
        TaxFilingStatus.ACCEPTED,
        TaxFilingStatus.REJECTED,
    },
    TaxFilingStatus.SUBMITTED: {
        TaxFilingStatus.ACCEPTED,
        TaxFilingStatus.REJECTED,
    },
    TaxFilingStatus.REJECTED: {TaxFilingStatus.CALCULATED},
    TaxFilingStatus.ACCEPTED: set(),
}

AEAT_RAW_MAX = 8000
LIVE_MODELO = "303"  # kept for callers; live set is LIVE_MODELOS

ANNUAL_MODELOS = {"190", "390", "347"}
SUPPORTED_CALCULATIONS = {"111", "115", "130", "190", "303", "390"}
ALREADY_SENT_STATUSES = {
    TaxFilingStatus.SUBMITTED,
    TaxFilingStatus.ACCEPTED,
}


class FilingForbiddenError(Exception):
    """Caller is authenticated but is not the filing owner — HTTP 403."""

    def __init__(self, message: str = "Only the filing owner may perform this action."):
        super().__init__(message)
        self.message = message


class FilingConflictError(Exception):
    """Duplicate period or second submit — HTTP 409."""

    def __init__(self, message: str, filing: dict):
        super().__init__(message)
        self.message = message
        self.filing = filing

    def as_detail(self) -> dict:
        filing = self.filing or {}
        return {
            "error": "FILING_EXISTS",
            "detail": self.message,
            "filing_id": str(filing.get("_id") or ""),
            "reference": period_reference(filing),
            "status": filing.get("status"),
            "modelo": filing.get("modelo"),
            "year": filing.get("year"),
            "period_key": filing.get("period_key") or filing.get("quarter"),
        }


def period_reference(filing: dict) -> Optional[str]:
    submission = filing.get("submission") or {}
    aeat = filing.get("aeat_result") or {}
    return (
        submission.get("reference")
        or aeat.get("csv")
        or aeat.get("justificante")
        or aeat.get("code")
        or str(filing.get("_id") or "")
        or None
    )


def serialize_filing(document: dict) -> dict:
    result = dict(document)
    result["_id"] = str(result["_id"])
    for key, value in list(result.items()):
        if isinstance(value, datetime):
            result[key] = value.isoformat()
    for collection_key in ("comments", "history", "validation_results"):
        for item in result.get(collection_key) or []:
            for key, value in list(item.items()):
                if isinstance(value, datetime):
                    item[key] = value.isoformat()
    for nested_key in ("submission", "aeat_result"):
        nested = result.get(nested_key)
        if isinstance(nested, dict):
            for key, value in list(nested.items()):
                if isinstance(value, datetime):
                    nested[key] = value.isoformat()
    if result.get("aeat_result"):
        result["aeat_result"] = enrich_aeat_result(result["aeat_result"])
    result["can_recalculate"] = result.get("status") == TaxFilingStatus.REJECTED.value
    result["totals_locked"] = bool(result.get("totals_locked"))
    result["can_live_submit"] = str(result.get("modelo") or "") in LIVE_MODELOS
    result["needs_percipients"] = str(result.get("modelo") or "") in PERCIPIENT_MODELOS
    result["justificante_available"] = bool(
        (result.get("aeat_result") or {}).get("has_justificante")
    )
    return result


def env_cert_password() -> str:
    """Removed — Contia365 now uses its own company certificate."""
    return ""


class TaxFilingService:
    def __init__(self, repo=None, engine=None, aeat_modelo_client=None):
        self.repo = repo if repo is not None else TaxFilingRepository()
        self.engine = engine if engine is not None else TaxEngineService()
        self.aeat_modelo_client = aeat_modelo_client or AeatModeloClient()

    def _profile(self, user: dict) -> dict:
        profile = get_canonical_fiscal_profile(
            self.repo.db["users"], self.repo.db["census_data"], user["_id"]
        )
        if not profile:
            raise ValueError("Complete the fiscal profile before creating a tax filing.")
        return profile

    def create(
        self,
        user: dict,
        modelo: str,
        year: int,
        quarter: Optional[str],
        month: Optional[int] = None,
        period_key: Optional[str] = None,
    ) -> dict:
        modelo = str(modelo).strip()
        profile = self._profile(user)
        if modelo not in applicable_modelos(profile):
            raise ValueError(f"Modelo {modelo} is not applicable under the fiscal profile.")
        assert_spanish_tax_allowed(user)

        periodicity = self._periodicity(profile, modelo)
        is_annual = modelo in ANNUAL_MODELOS or periodicity == "ANUAL"
        if is_annual:
            quarter_value = None
            month_value = None
            period_key = "ANNUAL"
            redeme = False
            aeat_period = None
        else:
            period = resolve_tax_period(
                year=year,
                periodicity=periodicity,
                quarter=quarter,
                month=month,
                period_key=period_key,
                modelo=modelo,
                allow_monthly=modelo == "303",
            )
            quarter_value = period.quarter
            month_value = period.month
            period_key = period.period_key
            redeme = period.is_redeme and modelo == "303"
            aeat_period = period.aeat_period

        now = datetime.utcnow()
        user_id = str(user["_id"])
        existing = self.repo.get_by_period(user_id, modelo, year, period_key)
        if existing:
            raise FilingConflictError(
                f"A {modelo} filing already exists for {year} {period_key}.",
                existing,
            )
        document = {
            "user_id": user_id,
            "organization_id": str(user.get("organization_id", user_id)),
            "fiscal_profile_id": str(profile["_id"]),
            "modelo": modelo,
            "year": year,
            "quarter": quarter_value,
            "month": month_value,
            "period_key": period_key,
            "periodicity": periodicity,
            "redeme": redeme,
            "aeat_period": aeat_period,
            "status": TaxFilingStatus.DRAFT.value,
            "calculation": None,
            "tax_report_id": None,
            "reviewer_id": None,
            "approver_id": None,
            "reviewed_at": None,
            "approved_at": None,
            "submitted_at": None,
            "accepted_at": None,
            "rejected_at": None,
            "submission": None,
            "totals_locked": False,
            "validation_results": [],
            "comments": [],
            "history": [{
                "from_status": None,
                "to_status": TaxFilingStatus.DRAFT.value,
                "actor_id": user_id,
                "created_at": now,
                "comment": "Tax filing created",
            }],
            "created_at": now,
            "updated_at": now,
        }
        try:
            return self.repo.create(document)
        except DuplicateKeyError as exc:
            existing = self.repo.get_by_period(user_id, modelo, year, period_key)
            if existing:
                raise FilingConflictError(
                    f"A {modelo} filing already exists for {year} {period_key}.",
                    existing,
                ) from exc
            raise FilingConflictError(
                f"A {modelo} filing already exists for {year} {period_key}.",
                {"modelo": modelo, "year": year, "period_key": period_key},
            ) from exc

    def calculate(
        self,
        filing_id: str,
        user: dict,
        modelo_id: Optional[str],
        comment: Optional[str],
    ) -> dict:
        filing = self._require_owner(filing_id, user)
        current = TaxFilingStatus(filing["status"])
        if current not in {TaxFilingStatus.DRAFT, TaxFilingStatus.REJECTED}:
            raise ValueError("Only DRAFT or REJECTED filings can be calculated.")
        if filing.get("totals_locked") and current != TaxFilingStatus.REJECTED:
            raise ValueError(
                "Totals are locked after approval and cannot be overwritten."
            )
        if filing["modelo"] not in SUPPORTED_CALCULATIONS:
            raise ValueError(
                f"Calculation is not implemented for Modelo {filing['modelo']}."
            )

        user_id = str(user["_id"])
        org_id = str(user.get("organization_id", user_id))
        modelo = filing["modelo"]
        year = filing["year"]
        calculator = getattr(self.engine, f"calculate_modelo_{modelo}")
        if modelo == "303":
            result = calculator(
                user_id, org_id, year,
                quarter=filing.get("quarter"),
                modelo_id=modelo_id,
                month=filing.get("month"),
                period_key=filing.get("period_key"),
            )
            report_quarter = (
                filing.get("period_key")
                or filing.get("quarter")
                or (
                    f"M{int(filing['month']):02d}"
                    if filing.get("month")
                    else None
                )
            )
        elif filing.get("quarter"):
            quarter = Quarter(filing["quarter"])
            result = calculator(
                user_id, org_id, year, quarter, modelo_id
            )
            report_quarter = quarter.value
        else:
            result = calculator(user_id, org_id, year, modelo_id)
            report_quarter = Quarter.Q4.value

        calculation = result.model_dump(mode="json")
        report_query = {
            "user_id": user_id,
            "modelo": modelo,
            "year": year,
        }
        if filing.get("period_key"):
            report = self.repo.db["tax_reports"].find_one({
                **report_query,
                "period_key": filing["period_key"],
            })
        else:
            report = None
        if not report:
            report = self.repo.db["tax_reports"].find_one({
                **report_query,
                "quarter": {"$in": [report_quarter, Quarter(report_quarter)]}
                if report_quarter in {"Q1", "Q2", "Q3", "Q4"}
                else report_quarter,
            })
        extra = {
            "calculation": calculation,
            "calculated_at": datetime.utcnow(),
            "tax_report_id": str(report["_id"]) if report else None,
            "validation_results": [],
            "totals_locked": False,
        }
        return self._transition(
            filing, user_id, TaxFilingStatus.CALCULATED, comment, extra
        )

    def start_review(self, filing_id: str, user: dict, comment: Optional[str]) -> dict:
        filing = self._require_owner(filing_id, user)
        now = datetime.utcnow()
        return self._transition(
            filing,
            str(user["_id"]),
            TaxFilingStatus.IN_REVIEW,
            comment,
            {"reviewer_id": str(user["_id"]), "reviewed_at": now},
        )

    def approve(self, filing_id: str, user: dict, comment: Optional[str]) -> dict:
        filing = self._require_owner(filing_id, user)
        now = datetime.utcnow()
        return self._transition(
            filing,
            str(user["_id"]),
            TaxFilingStatus.APPROVED,
            comment,
            {
                "approver_id": str(user["_id"]),
                "approved_at": now,
                "totals_locked": True,
            },
        )

    def submit(
        self,
        filing_id: str,
        user: dict,
        comment: Optional[str],
        test_mode: bool,
    ) -> dict:
        if test_mode:
            return self.submit_test(filing_id, user, comment)
        return self.submit_live(filing_id, user, comment)

    def submit_test(
        self,
        filing_id: str,
        user: dict,
        comment: Optional[str],
        test_mode: bool = True,
    ) -> dict:
        if not test_mode:
            return self.submit_live(filing_id, user, comment)
        filing = self._require_owner(filing_id, user)
        self._refuse_duplicate_submit(filing)
        now = datetime.utcnow()
        submission = {
            "mode": "test",
            "reference": f"TEST-{uuid4().hex[:16].upper()}",
            "submitted_by": str(user["_id"]),
            "submitted_at": now,
        }
        return self._transition(
            filing,
            str(user["_id"]),
            TaxFilingStatus.SUBMITTED,
            comment,
            {"submitted_at": now, "submission": submission},
        )

    def submit_live(
        self,
        filing_id: str,
        user: dict,
        comment: Optional[str],
    ) -> dict:
        filing = self._require_owner(filing_id, user)
        self._refuse_duplicate_submit(filing)
        if TaxFilingStatus(filing["status"]) != TaxFilingStatus.APPROVED:
            raise ValueError("Only APPROVED filings can be submitted to AEAT.")
        modelo = str(filing.get("modelo") or "")
        if modelo not in LIVE_MODELOS:
            raise ValueError(
                f"Live AEAT submission is not implemented for Modelo {modelo}. "
                "Use test_mode=true."
            )
        if modelo not in ANNUAL_FILE_MODELOS and not (
            filing.get("quarter") or filing.get("month") or filing.get("period_key")
        ):
            raise ValueError(f"Modelo {modelo} live submit requires a filing period.")
        if not filing.get("calculation"):
            raise ValueError("Calculate the filing before submitting.")
        if not filing_is_legally_complete(modelo, filing.get("calculation")):
            raise ValueError(
                f"Modelo {modelo} is legally incomplete without employee "
                "(percipient) records."
            )

        nif, name = self._declarant_identity(user)
        try:
            declaration = build_modelo_file(
                modelo=modelo,
                nif=nif,
                name=name,
                year=int(filing["year"]),
                calculation=filing["calculation"],
                quarter=filing.get("quarter")
                or filing.get("aeat_period")
                or filing.get("period_key"),
                redeme=bool(filing.get("redeme")),
                contact_phone=str(user.get("phone") or user.get("mobile") or ""),
                contact_name=name,
                contact_email=str(user.get("email") or ""),
            )
        except ModeloFileError as exc:
            raise ValueError(str(exc)) from exc

        aeat_response = self.aeat_modelo_client.submit(
            declaration.encode("latin-1"),
            nif,
            modelo,
        )
        return self._store_aeat_outcome(filing, user, comment, aeat_response)

    def record_result(
        self,
        filing_id: str,
        user: dict,
        accepted: bool,
        result: dict,
        comment: Optional[str],
    ) -> dict:
        filing = self._require_owner(filing_id, user)
        if str(filing.get("modelo")) in LIVE_MODELOS:
            raise ValueError(
                f"Modelo {filing.get('modelo')} results come from AEAT. "
                "Manual accept/reject is not allowed."
            )
        if TaxFilingStatus(filing["status"]) != TaxFilingStatus.SUBMITTED:
            raise ValueError(
                "Only SUBMITTED (test-mode) filings can record a manual AEAT result."
            )
        now = datetime.utcnow()
        target = (
            TaxFilingStatus.ACCEPTED if accepted else TaxFilingStatus.REJECTED
        )
        result = dict(result)
        result["recorded_at"] = now
        result["source"] = result.get("source") or "manual"
        result["message"] = readable_aeat_message(
            result.get("code"), result.get("description")
        )
        extra = {
            "validation_results": result.get("validation_results") or [],
            "aeat_result": result,
            "accepted_at" if accepted else "rejected_at": now,
            "totals_locked": accepted,
        }
        return self._transition(
            filing, str(user["_id"]), target, comment, extra
        )

    def justificante_pdf(self, filing_id: str, user: dict) -> tuple[bytes, str]:
        filing = self._required(filing_id, user)
        aeat = filing.get("aeat_result")
        if not aeat:
            raise LookupError("No AEAT result is stored on this filing.")
        status = str(filing.get("status") or "")
        if status not in {
            TaxFilingStatus.ACCEPTED.value,
            TaxFilingStatus.REJECTED.value,
        }:
            raise ValueError("A justificante is only available after AEAT ACCEPTED or REJECTED.")
        pdf_bytes = build_justificante_pdf(filing)
        period = filing.get("quarter") or filing.get("period_key") or "period"
        filename = f"justificante-modelo-{filing.get('modelo')}-{filing.get('year')}-{period}.pdf"
        return pdf_bytes, filename

    def get(self, filing_id: str, user: dict) -> dict:
        return self._required(filing_id, user)

    def list(
        self,
        user: dict,
        status: Optional[str] = None,
        year: Optional[int] = None,
        modelo: Optional[str] = None,
    ) -> list[dict]:
        if status:
            try:
                status = TaxFilingStatus(status.upper()).value
            except ValueError as exc:
                raise ValueError("Invalid tax filing status.") from exc
        return self.repo.list(str(user["_id"]), status, year, modelo)

    def _required(self, filing_id: str, user: dict) -> dict:
        filing = self.repo.get_by_id(filing_id, str(user["_id"]))
        if not filing:
            raise LookupError("Tax filing not found.")
        return filing

    def _require_owner(self, filing_id: str, user: dict) -> dict:
        getter = getattr(self.repo, "get_by_id_any", None)
        filing = getter(filing_id) if getter else self.repo.get_by_id(
            filing_id, str(user["_id"])
        )
        if not filing:
            raise LookupError("Tax filing not found.")
        if str(filing.get("user_id")) != str(user["_id"]):
            raise FilingForbiddenError(
                "Only the filing owner may review, approve, or submit this filing."
            )
        return filing

    def _transition(
        self,
        filing: dict,
        actor_id: str,
        target: TaxFilingStatus,
        comment: Optional[str],
        extra: Optional[dict] = None,
    ) -> dict:
        current = TaxFilingStatus(filing["status"])
        if target not in ALLOWED_TRANSITIONS[current]:
            raise ValueError(f"Invalid transition: {current.value} -> {target.value}.")
        now = datetime.utcnow()
        event = {
            "from_status": current.value,
            "to_status": target.value,
            "actor_id": actor_id,
            "created_at": now,
            "comment": comment,
        }
        set_values = {
            "status": target.value,
            "updated_at": now,
            **(extra or {}),
        }
        update = {"$set": set_values, "$push": {"history": event}}
        if comment:
            update["$push"]["comments"] = {
                "user_id": actor_id,
                "action": target.value,
                "text": comment,
                "created_at": now,
            }
        updated = self.repo.update(
            str(filing["_id"]), filing["user_id"], update
        )
        if not updated:
            raise LookupError("Tax filing not found.")
        return updated

    @staticmethod
    def _periodicity(profile: dict, modelo: str) -> str:
        return obligation_periodicity(profile, modelo)

    def _declarant_identity(self, user: dict) -> tuple[str, str]:
        """
        Returns (taxpayer_nif, taxpayer_name) for AEAT submission.

        Business: taxpayer = the company → returns CIF + legal name.
        Person:   taxpayer = the user    → returns personal NIF + full name.
        """
        from app.services.user_type_vocab import canonicalize_user_type
        user_type = canonicalize_user_type(user.get("user_type_selection"))

        if user_type == "business":
            bp = user.get("business_profile") or {}
            cif = str(bp.get("cif") or "").replace(" ", "").upper()
            legal_name = str(bp.get("legal_name") or "").strip()
            if not cif:
                raise ValueError(
                    "Business profile is missing the company CIF. "
                    "Complete business onboarding step 2 first."
                )
            if not legal_name:
                raise ValueError(
                    "Business profile is missing the company legal name. "
                    "Complete business onboarding step 2 first."
                )
            return cif, legal_name

        # Person (autónomo) — existing logic unchanged
        identity = {}
        try:
            profile = self._profile(user)
            identity = profile.get("taxpayer_identity") or {}
        except ValueError:
            identity = {}
        nif = (
            identity.get("nif_nie")
            or user.get("tax_id")
            or user.get("dni_nie")
            or ""
        )
        name = (
            identity.get("full_name")
            or user.get("full_name")
            or user.get("name")
            or ""
        )
        nif = str(nif).replace(" ", "").upper()
        name = str(name).strip()
        if not nif:
            raise ValueError("Fiscal profile is missing NIF/NIE.")
        if not name:
            raise ValueError("Fiscal profile is missing the declarant name.")
        return nif, name

    def _store_aeat_outcome(
        self,
        filing: dict,
        user: dict,
        comment: Optional[str],
        aeat: AeatModeloResponse,
    ) -> dict:
        now = datetime.utcnow()
        accepted = bool(aeat.success)
        target = (
            TaxFilingStatus.ACCEPTED if accepted else TaxFilingStatus.REJECTED
        )
        submission = {
            "mode": "live",
            "reference": aeat.csv or aeat.justificante or aeat.code,
            "submitted_by": str(user["_id"]),
            "submitted_at": now,
            "code": aeat.code,
            "description": aeat.description,
            "csv": aeat.csv,
            "justificante": aeat.justificante,
            "http_status": aeat.http_status,
        }
        extra = {
            "submitted_at": now,
            "submission": submission,
            "aeat_result": {
                "code": aeat.code,
                "description": aeat.description,
                "message": readable_aeat_message(aeat.code, aeat.description),
                "csv": aeat.csv,
                "justificante": aeat.justificante,
                "raw_response": (aeat.raw_response or "")[:AEAT_RAW_MAX],
                "recorded_at": now,
                "source": "aeat",
            },
            "validation_results": [],
            "accepted_at" if accepted else "rejected_at": now,
            "totals_locked": accepted,
        }
        return self._transition(
            filing, str(user["_id"]), target, comment, extra
        )

    def _refuse_duplicate_submit(self, filing: dict) -> None:
        status = TaxFilingStatus(filing["status"])
        if status not in ALREADY_SENT_STATUSES:
            return
        reference = period_reference(filing)
        raise FilingConflictError(
            f"A {filing.get('modelo')} filing for "
            f"{filing.get('year')} {filing.get('period_key') or filing.get('quarter')} "
            f"was already submitted"
            + (f" (reference {reference})." if reference else "."),
            filing,
        )

