"""Tax filing lifecycle and calculation orchestration."""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from pymongo.errors import DuplicateKeyError

from app.models.tax_engine import Quarter
from app.models.tax_filing import TaxFilingStatus
from app.repos.tax_filing_repo import TaxFilingRepository
from app.services.fiscal_profile_service import (
    applicable_modelos,
    get_canonical_fiscal_profile,
)
from app.services.tax_engine_service import TaxEngineService


ALLOWED_TRANSITIONS = {
    TaxFilingStatus.DRAFT: {TaxFilingStatus.CALCULATED},
    TaxFilingStatus.CALCULATED: {TaxFilingStatus.IN_REVIEW},
    TaxFilingStatus.IN_REVIEW: {TaxFilingStatus.APPROVED},
    TaxFilingStatus.APPROVED: {TaxFilingStatus.SUBMITTED},
    TaxFilingStatus.SUBMITTED: {
        TaxFilingStatus.ACCEPTED,
        TaxFilingStatus.REJECTED,
    },
    TaxFilingStatus.REJECTED: {TaxFilingStatus.CALCULATED},
    TaxFilingStatus.ACCEPTED: set(),
}

ANNUAL_MODELOS = {"190", "390", "347"}
SUPPORTED_CALCULATIONS = {"111", "115", "130", "190", "303", "390"}


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
    return result


class TaxFilingService:
    def __init__(self):
        self.repo = TaxFilingRepository()
        self.engine = TaxEngineService()

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
    ) -> dict:
        modelo = str(modelo).strip()
        profile = self._profile(user)
        if modelo not in applicable_modelos(profile):
            raise ValueError(f"Modelo {modelo} is not applicable under the fiscal profile.")

        is_annual = modelo in ANNUAL_MODELOS or self._periodicity(profile, modelo) == "ANUAL"
        if is_annual:
            quarter_value = None
            period_key = "ANNUAL"
        else:
            if not quarter:
                raise ValueError("quarter is required for a non-annual filing.")
            try:
                quarter_value = Quarter(quarter.upper()).value
            except ValueError as exc:
                raise ValueError("quarter must be Q1, Q2, Q3 or Q4.") from exc
            period_key = quarter_value

        now = datetime.utcnow()
        user_id = str(user["_id"])
        document = {
            "user_id": user_id,
            "organization_id": str(user.get("organization_id", user_id)),
            "fiscal_profile_id": str(profile["_id"]),
            "modelo": modelo,
            "year": year,
            "quarter": quarter_value,
            "period_key": period_key,
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
            raise ValueError(
                f"A {modelo} filing already exists for {year} {period_key}."
            ) from exc

    def calculate(
        self,
        filing_id: str,
        user: dict,
        modelo_id: Optional[str],
        comment: Optional[str],
    ) -> dict:
        filing = self._required(filing_id, user)
        current = TaxFilingStatus(filing["status"])
        if current not in {TaxFilingStatus.DRAFT, TaxFilingStatus.REJECTED}:
            raise ValueError("Only DRAFT or REJECTED filings can be calculated.")
        if filing["modelo"] not in SUPPORTED_CALCULATIONS:
            raise ValueError(
                f"Calculation is not implemented for Modelo {filing['modelo']}."
            )

        user_id = str(user["_id"])
        org_id = str(user.get("organization_id", user_id))
        modelo = filing["modelo"]
        year = filing["year"]
        if filing.get("quarter"):
            quarter = Quarter(filing["quarter"])
            calculator = getattr(self.engine, f"calculate_modelo_{modelo}")
            result = calculator(
                user_id, org_id, year, quarter, modelo_id
            )
            report_quarter = quarter.value
        else:
            calculator = getattr(self.engine, f"calculate_modelo_{modelo}")
            result = calculator(user_id, org_id, year, modelo_id)
            report_quarter = Quarter.Q4.value

        calculation = result.model_dump(mode="json")
        report = self.repo.db["tax_reports"].find_one({
            "user_id": user_id,
            "modelo": modelo,
            "year": year,
            "quarter": {"$in": [report_quarter, Quarter(report_quarter)]},
        })
        extra = {
            "calculation": calculation,
            "calculated_at": datetime.utcnow(),
            "tax_report_id": str(report["_id"]) if report else None,
            "validation_results": [],
        }
        return self._transition(
            filing, user_id, TaxFilingStatus.CALCULATED, comment, extra
        )

    def start_review(self, filing_id: str, user: dict, comment: Optional[str]) -> dict:
        filing = self._required(filing_id, user)
        now = datetime.utcnow()
        return self._transition(
            filing,
            str(user["_id"]),
            TaxFilingStatus.IN_REVIEW,
            comment,
            {"reviewer_id": str(user["_id"]), "reviewed_at": now},
        )

    def approve(self, filing_id: str, user: dict, comment: Optional[str]) -> dict:
        filing = self._required(filing_id, user)
        now = datetime.utcnow()
        return self._transition(
            filing,
            str(user["_id"]),
            TaxFilingStatus.APPROVED,
            comment,
            {"approver_id": str(user["_id"]), "approved_at": now},
        )

    def submit_test(
        self,
        filing_id: str,
        user: dict,
        comment: Optional[str],
        test_mode: bool,
    ) -> dict:
        if not test_mode:
            raise ValueError(
                "Real AEAT tax-modelo submission is not configured; use test_mode=true."
            )
        filing = self._required(filing_id, user)
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

    def record_result(
        self,
        filing_id: str,
        user: dict,
        accepted: bool,
        result: dict,
        comment: Optional[str],
    ) -> dict:
        filing = self._required(filing_id, user)
        now = datetime.utcnow()
        target = (
            TaxFilingStatus.ACCEPTED if accepted else TaxFilingStatus.REJECTED
        )
        result = dict(result)
        result["recorded_at"] = now
        extra = {
            "validation_results": result.get("validation_results") or [],
            "aeat_result": result,
            "accepted_at" if accepted else "rejected_at": now,
        }
        return self._transition(
            filing, str(user["_id"]), target, comment, extra
        )

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
        for obligation in profile.get("periodic_tax_obligations") or []:
            if str(obligation.get("modelo")) == modelo:
                return str(obligation.get("periodicity") or "TRIMESTRAL").upper()
        return "TRIMESTRAL"

