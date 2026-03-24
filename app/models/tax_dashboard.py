from pydantic import BaseModel, ConfigDict, Field


class TaxDeadlineResponse(BaseModel):
    modelo_no: str = Field(..., description="Extracted tax model number (e.g., '100').")
    name: str = Field(..., description="Full modelo name.")
    deadline: str = Field(..., description="Exact deadline date/time string as stored.")

    model_config = ConfigDict(populate_by_name=True)

