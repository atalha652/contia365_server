"""First-class tax filing entity and workflow request models."""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class TaxFilingStatus(str, Enum):
    DRAFT = "DRAFT"
    CALCULATED = "CALCULATED"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class TaxFilingCreate(BaseModel):
    modelo: str = Field(..., examples=["303"])
    year: int
    quarter: Optional[str] = Field(
        None, description="Q1-Q4 for quarterly modelos; omit for annual or monthly 303"
    )
    month: Optional[int] = Field(
        None, ge=1, le=12,
        description="1-12 when the fiscal profile files 303 monthly (REDEME)",
    )
    period_key: Optional[str] = Field(
        None, description="Calendar key e.g. 2026-03 for monthly 303"
    )


class TaxFilingCalculateRequest(BaseModel):
    modelo_id: Optional[str] = Field(
        None, description="Optional modelos collection id used to filter classified entries"
    )
    comment: Optional[str] = None


class TaxFilingActionRequest(BaseModel):
    comment: Optional[str] = None


class TaxFilingSubmitRequest(BaseModel):
    comment: Optional[str] = None
    test_mode: bool = Field(
        True,
        description="True = fake TEST- reference. False = live AEAT via T5+T6.",
    )
    cert_password: Optional[str] = Field(
        None,
        description="Password for the .p12 certificate when submitting live",
    )
    # Choose which certificate to use for live submission:
    #   "gestor"   – Contia365’s corporate certificate (requires apoderamiento)
    #   "taxpayer" – The user‑uploaded .p12 certificate (delegated)
    cert_mode: Literal["gestor", "taxpayer"] = Field(
        "taxpayer",
        description="Certificate mode for live submission",
    )



class TaxFilingResultRequest(BaseModel):
    accepted: bool
    code: str = Field(..., description="Test/AEAT result code")
    description: str
    csv: Optional[str] = None
    validation_results: List[Dict[str, Any]] = Field(default_factory=list)
    raw_response: Optional[str] = None
    comment: Optional[str] = None


class TaxFilingComment(BaseModel):
    user_id: str
    action: str
    text: str
    created_at: datetime


class TaxFilingEvent(BaseModel):
    from_status: Optional[TaxFilingStatus] = None
    to_status: TaxFilingStatus
    actor_id: str
    created_at: datetime
    comment: Optional[str] = None

