from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field


class TaxDeadlineItem(BaseModel):
    modelo: str = Field(..., description="Modelo number e.g. '303'")
    description: str = Field(..., description="Tax obligation description")
    periodicity: str = Field(..., description="TRIMESTRAL / MENSUAL / ANUAL")
    current_period: str = Field(..., description="e.g. 'Q1 2026' or 'March 2026'")
    deadline_date: str = Field(..., description="ISO date YYYY-MM-DD")
    days_remaining: int = Field(..., description="Days until deadline (negative = overdue)")
    status: str = Field(..., description="upcoming | due_soon | overdue")

    model_config = ConfigDict(populate_by_name=True)


class TaxDeadlineResponse(BaseModel):
    """Legacy single-modelo response — kept for backwards compatibility."""
    modelo_no: str = Field(..., description="Extracted tax model number (e.g., '100').")
    name: str = Field(..., description="Full modelo name.")
    deadline: str = Field(..., description="Exact deadline date/time string as stored.")

    model_config = ConfigDict(populate_by_name=True)


class TaxDeadlinesResponse(BaseModel):
    user_id: str
    census_record_id: str
    nif_nie: Optional[str] = None
    full_name: Optional[str] = None
    deadlines: List[TaxDeadlineItem] = []
    total: int = 0

    model_config = ConfigDict(populate_by_name=True)
