from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field


class TaxDeadlineItem(BaseModel):
    modelo: str = Field(..., description="Modelo number e.g. '303'")
    description: str = Field(..., description="Tax obligation description")
    periodicity: str = Field(..., description="TRIMESTRAL / MENSUAL / ANUAL")
    current_period: str = Field(..., description="e.g. 'Q1 2026' or 'March 2026'")
    year: Optional[int] = None
    quarter: Optional[str] = Field(None, description="Q1-Q4 for quarterly modelos")
    month: Optional[int] = Field(None, description="1-12 for monthly 303 (REDEME)")
    period_key: Optional[str] = Field(
        None, description="Q2, ANNUAL, or 2026-03"
    )
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    deadline_date: str = Field(..., description="ISO date YYYY-MM-DD")
    days_remaining: int = Field(..., description="Days until deadline (negative = overdue)")
    status: str = Field(..., description="upcoming | due | overdue | completed")
    tax_filing_id: Optional[str] = None
    tax_report_id: Optional[str] = None
    filed_at: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)


class TaxDeadlineResponse(BaseModel):
    """Legacy single-modelo response — kept for backwards compatibility."""
    modelo_no: str = Field(..., description="Extracted tax model number (e.g., '100').")
    name: str = Field(..., description="Full modelo name.")
    deadline: str = Field(..., description="Exact deadline date/time string as stored.")

    model_config = ConfigDict(populate_by_name=True)


class TaxDeadlinesResponse(BaseModel):
    user_id: str
    census_record_id: Optional[str] = None
    nif_nie: Optional[str] = None
    full_name: Optional[str] = None
    year: Optional[int] = None
    deadlines: List[TaxDeadlineItem] = []
    total: int = 0
    counts: Optional[dict] = None

    model_config = ConfigDict(populate_by_name=True)
