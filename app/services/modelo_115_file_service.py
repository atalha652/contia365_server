"""Modelo 115 official file (rent IRPF withholdings)."""

from __future__ import annotations

from typing import Optional, Union

from app.models.tax_engine import Modelo115Results, Quarter
from app.services.modelo_boe_common import (
    ModeloFileError,
    build_wrapper,
    declaration_type_ingreso,
    digits,
    normalize_period,
    put,
    require_name,
    require_nif,
    signed,
    slice_field,
    write_identity,
)

PAGE_LEN = 500


def build_page_01(
    *,
    nif: str,
    name: str,
    year: int,
    period: str,
    totals: Modelo115Results,
    declaration_type: Optional[str] = None,
) -> str:
    base = float(totals.total_rent_base or 0)
    withheld = float(totals.withholding_payable or 0)
    count = int(getattr(totals, "percipient_count", 0) or (1 if withheld or base else 0))
    buf = [" "] * PAGE_LEN
    write_identity(
        buf, modelo="115", nif=nif, name=name, year=year, period=period,
        declaration_type=declaration_type or declaration_type_ingreso(withheld),
    )
    put(buf, 109, digits(count, 15))  # [01]
    put(buf, 124, signed(base, 17))  # [02]
    put(buf, 141, signed(withheld, 17))  # [03]
    put(buf, 158, signed(0, 17))  # [04]
    put(buf, 175, signed(withheld, 17))  # [05]
    put(buf, 192, " ")
    put(buf, 193, " " * 13)
    put(buf, 206, " " * 34)
    put(buf, 240, " " * 236)
    put(buf, 476, " " * 13)
    put(buf, 489, "</T11501000>")
    return "".join(buf)


def build_modelo_115_file(
    *,
    nif: str,
    name: str,
    year: int,
    quarter: Union[Quarter, str],
    totals: Modelo115Results,
    declaration_type: Optional[str] = None,
) -> str:
    nif = require_nif(nif)
    name = require_name(name)
    period = normalize_period(quarter)
    page = build_page_01(
        nif=nif, name=name, year=year, period=period, totals=totals,
        declaration_type=declaration_type,
    )
    if len(page) != PAGE_LEN:
        raise ModeloFileError(f"DP11501 length {len(page)} != {PAGE_LEN}")
    return build_wrapper(modelo="115", year=year, period=period, pages=page)


slice_page_field = slice_field
