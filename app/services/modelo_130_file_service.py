"""Modelo 130 official file (diseño de registro — T-page, OCA/AEAT 130 layout)."""

from __future__ import annotations

from typing import Optional, Union

from app.models.tax_engine import Modelo130Results, Quarter
from app.services.modelo_boe_common import (
    ModeloFileError,
    build_wrapper,
    declaration_type_ingreso,
    normalize_period,
    num,
    put,
    require_name,
    require_nif,
    signed,
    slice_field,
    write_identity,
)

PAGE_LEN = 600


def build_page_01(
    *,
    nif: str,
    name: str,
    year: int,
    period: str,
    totals: Modelo130Results,
    declaration_type: Optional[str] = None,
) -> str:
    buf = [" "] * PAGE_LEN
    casilla_01 = float(totals.total_income or 0)
    casilla_02 = float(totals.total_expenses or 0)
    casilla_03 = float(totals.taxable_income or 0)
    casilla_04 = round(max(0.0, casilla_03 * float(totals.irpf_rate or 0.20)), 2)
    casilla_05 = float(getattr(totals, "prior_payments", 0) or 0)
    casilla_06 = float(totals.irpf_already_withheld or 0)
    casilla_07 = round(casilla_04 - casilla_05 - casilla_06, 2)
    casilla_12 = casilla_07
    casilla_14 = casilla_12
    casilla_17 = casilla_14
    casilla_19 = float(totals.irpf_payable or 0)
    tipo = declaration_type or declaration_type_ingreso(casilla_19)
    write_identity(
        buf, modelo="130", nif=nif, name=name, year=year, period=period,
        declaration_type=tipo,
    )
    put(buf, 109, num(casilla_01, 17))
    put(buf, 126, num(casilla_02, 17))
    put(buf, 143, signed(casilla_03, 17))
    put(buf, 160, num(casilla_04, 17))
    put(buf, 177, num(casilla_05, 17))
    put(buf, 194, num(casilla_06, 17))
    put(buf, 211, signed(casilla_07, 17))
    put(buf, 228, num(0, 17))  # [08]
    put(buf, 245, num(0, 17))
    put(buf, 262, num(0, 17))
    put(buf, 279, signed(0, 17))  # [11]
    put(buf, 296, num(casilla_12, 17))
    put(buf, 313, signed(0, 17))  # [13]
    put(buf, 330, num(casilla_14, 17))
    put(buf, 347, num(0, 17))  # [15]
    put(buf, 364, num(0, 17))  # [16]
    put(buf, 381, signed(casilla_17, 17))
    put(buf, 398, num(0, 17))  # [18]
    put(buf, 415, signed(casilla_19, 17))
    put(buf, 432, " ")
    put(buf, 433, " " * 13)
    put(buf, 446, " " * 34)
    put(buf, 480, " " * 96)
    put(buf, 576, " " * 13)
    put(buf, 589, "</T13001000>")
    return "".join(buf)


def build_modelo_130_file(
    *,
    nif: str,
    name: str,
    year: int,
    quarter: Union[Quarter, str],
    totals: Modelo130Results,
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
        raise ModeloFileError(f"DP13001 length {len(page)} != {PAGE_LEN}")
    return build_wrapper(modelo="130", year=year, period=period, pages=page)


slice_page_field = slice_field
