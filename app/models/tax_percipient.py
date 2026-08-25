"""Percipient (perceptor) records required for Modelo 111 and 190."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class PercipientLine(BaseModel):
    nif: str
    full_name: str
    perception_key: str = Field("G", description="AEAT clave: A trabajo, G profesionales, …")
    perception_subkey: str = "01"
    year: int
    quarter: Optional[str] = Field(None, description="Q1-Q4 for 111; omit for annual 190 rollup")
    base_amount: float = 0.0
    withheld_amount: float = 0.0
    in_kind: bool = False
    province_code: Optional[str] = None
    kind: str = Field("professional", description="employee | professional | landlord")


class PercipientCreate(PercipientLine):
    pass


class PercipientUpdate(BaseModel):
    nif: Optional[str] = None
    full_name: Optional[str] = None
    perception_key: Optional[str] = None
    perception_subkey: Optional[str] = None
    year: Optional[int] = None
    quarter: Optional[str] = None
    base_amount: Optional[float] = None
    withheld_amount: Optional[float] = None
    in_kind: Optional[bool] = None
    province_code: Optional[str] = None
    kind: Optional[str] = None


class PercipientRecord(PercipientLine):
    id: str
    user_id: str
    organization_id: str
    created_at: datetime
    updated_at: datetime
