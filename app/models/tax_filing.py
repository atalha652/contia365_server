"""First-class tax filing entity and workflow request models."""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

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
        None, description="Q1-Q4 for quarterly modelos; omit for annual modelos"
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
        description="Must be true until an AEAT tax-modelo service is configured",
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

