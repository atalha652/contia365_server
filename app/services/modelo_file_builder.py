"""Build the official AEAT file for a tax filing modelo (T5 per modelo)."""

from __future__ import annotations

from typing import Any, Optional, Union

from app.models.tax_engine import (
    Modelo111Results,
    Modelo115Results,
    Modelo130Results,
    Modelo190Results,
    Modelo303Results,
    Modelo390Results,
    Quarter,
)
from app.services.modelo_111_file_service import build_modelo_111_file
from app.services.modelo_115_file_service import build_modelo_115_file
from app.services.modelo_130_file_service import build_modelo_130_file
from app.services.modelo_190_file_service import build_modelo_190_file
from app.services.modelo_303_file_service import build_modelo_303_file
from app.services.modelo_390_file_service import build_modelo_390_file
from app.services.modelo_boe_common import ModeloFileError

LIVE_MODELOS = {"111", "115", "130", "190", "303", "390"}
PERCIPIENT_MODELOS = {"111", "190"}
ANNUAL_FILE_MODELOS = {"190", "390"}


def _totals_payload(calculation) -> dict:
    if calculation is None:
        return {}
    if hasattr(calculation, "model_dump"):
        payload = calculation.model_dump()
    elif isinstance(calculation, dict):
        payload = calculation
    else:
        return {}
    return payload.get("totals") or payload.get("results") or payload


def parse_totals(modelo: str, calculation) -> Any:
    payload = _totals_payload(calculation)
    parsers = {
        "303": Modelo303Results,
        "130": Modelo130Results,
        "111": Modelo111Results,
        "115": Modelo115Results,
        "390": Modelo390Results,
        "190": Modelo190Results,
    }
    cls = parsers.get(str(modelo))
    if not cls:
        raise ModeloFileError(f"No file builder for Modelo {modelo}.")
    if isinstance(payload, cls):
        return payload
    return cls.model_validate(payload)


def filing_is_legally_complete(modelo: str, calculation) -> bool:
    if str(modelo) not in PERCIPIENT_MODELOS:
        return True
    totals = parse_totals(modelo, calculation)
    lines = getattr(totals, "lines", None) or []
    return bool(getattr(totals, "legally_complete", False) and lines)


def build_modelo_file(
    *,
    modelo: str,
    nif: str,
    name: str,
    year: int,
    calculation,
    quarter: Optional[Union[Quarter, str]] = None,
    redeme: bool = False,
    contact_phone: str = "",
    contact_name: str = "",
    contact_email: str = "",
) -> str:
    modelo = str(modelo)
    totals = parse_totals(modelo, calculation)
    if modelo == "303":
        return build_modelo_303_file(
            nif=nif, name=name, year=year, quarter=quarter, totals=totals,
            redeme=redeme,
        )
    if modelo == "130":
        return build_modelo_130_file(
            nif=nif, name=name, year=year, quarter=quarter, totals=totals
        )
    if modelo == "111":
        return build_modelo_111_file(
            nif=nif, name=name, year=year, quarter=quarter, totals=totals
        )
    if modelo == "115":
        return build_modelo_115_file(
            nif=nif, name=name, year=year, quarter=quarter, totals=totals
        )
    if modelo == "390":
        return build_modelo_390_file(nif=nif, name=name, year=year, totals=totals)
    if modelo == "190":
        return build_modelo_190_file(
            nif=nif, name=name, year=year, totals=totals,
            contact_phone=contact_phone, contact_name=contact_name,
            contact_email=contact_email,
        )
    raise ModeloFileError(f"Live AEAT submission is not implemented for Modelo {modelo}.")
