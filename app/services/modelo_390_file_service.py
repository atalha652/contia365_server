"""Modelo 390 official file — identification + régimen general page (2025 OCA layout).

Remaining 390 pages are out of scope (ISP / simplified / recargo). Page 02 maps
engine VAT buckets onto RG 0/4/10/21 casillas.
"""

from __future__ import annotations

from typing import Mapping, Optional, Union

from app.models.tax_engine import Modelo390Results, Quarter, VatByRateBucket
from app.services.modelo_boe_common import (
    ModeloFileError,
    build_wrapper,
    declaration_type_ingreso,
    normalize_period,
    put,
    require_name,
    require_nif,
    signed,
    slice_field,
    write_identity,
)

PAGE01_LEN = 600
PAGE02_LEN = 1806  # 12 header + 96*17 + 150 reserved + 12 close


def _bucket(totals: Modelo390Results, rate: str) -> VatByRateBucket:
    raw = (getattr(totals, "vat_by_rate", None) or {}).get(rate)
    if isinstance(raw, VatByRateBucket):
        return raw
    if isinstance(raw, Mapping):
        return VatByRateBucket(**raw)
    return VatByRateBucket()


def build_page_01(
    *,
    nif: str,
    name: str,
    year: int,
    period: str,
    totals: Modelo390Results,
    declaration_type: Optional[str] = None,
) -> str:
    buf = [" "] * PAGE01_LEN
    tipo = declaration_type or declaration_type_ingreso(float(totals.net_vat or 0))
    write_identity(
        buf, modelo="390", nif=nif, name=name, year=year, period=period,
        declaration_type=tipo,
    )
    put(buf, 589, "</T39001000>")
    return "".join(buf)


def build_page_02(*, totals: Modelo390Results) -> str:
    buf = [" "] * PAGE02_LEN
    put(buf, 1, "<T")
    put(buf, 3, "390")
    put(buf, 6, "02")
    put(buf, 8, "000>")
    put(buf, 12, " ")
    # 96 signed 17-char liquidation fields starting at pos 13.
    put(buf, 13, "0" * (96 * 17))
    b0 = _bucket(totals, "0")
    b4 = _bucket(totals, "4")
    b10 = _bucket(totals, "10")
    b21 = _bucket(totals, "21")
    if (
        b0.output_base == b4.output_base == b10.output_base == b21.output_base == 0
        and float(totals.total_sales or 0)
    ):
        b21 = VatByRateBucket(
            output_base=float(totals.total_sales or 0),
            output_vat=float(totals.output_vat or 0),
            input_base=float(totals.total_expenses or 0),
            input_vat=float(totals.input_vat or 0),
        )
    # Line 6 is field 0 @ pos 13. Line 10 (4% base) is field 4 @ 13+4*17=81.
    put(buf, 13, signed(b0.output_base, 17))   # [700]
    put(buf, 30, signed(b0.output_vat, 17))    # [701]
    put(buf, 81, signed(b4.output_base, 17))   # [01]
    put(buf, 98, signed(b4.output_vat, 17))    # [02]
    put(buf, 183, signed(b10.output_base, 17))  # [03]
    put(buf, 200, signed(b10.output_vat, 17))   # [04]
    put(buf, 217, signed(b21.output_base, 17))  # [05]
    put(buf, 234, signed(b21.output_vat, 17))   # [06]
    put(buf, 1611, signed(totals.total_sales or 0, 17))  # [33]
    put(buf, 1628, signed(totals.output_vat or 0, 17))  # [34]
    put(buf, 1645, " " * 150)
    put(buf, 1795, "</T39002000>")
    return "".join(buf)


def build_modelo_390_file(
    *,
    nif: str,
    name: str,
    year: int,
    totals: Modelo390Results,
    quarter: Union[Quarter, str, None] = None,
    declaration_type: Optional[str] = None,
) -> str:
    nif = require_nif(nif)
    name = require_name(name)
    period = normalize_period(quarter or "0A", annual=True)
    page1 = build_page_01(
        nif=nif, name=name, year=year, period=period, totals=totals,
        declaration_type=declaration_type,
    )
    page2 = build_page_02(totals=totals)
    if len(page1) != PAGE01_LEN:
        raise ModeloFileError(f"DP39001 length {len(page1)} != {PAGE01_LEN}")
    if len(page2) != PAGE02_LEN:
        raise ModeloFileError(f"DP39002 length {len(page2)} != {PAGE02_LEN}")
    return build_wrapper(modelo="390", year=year, period=period, pages=page1 + page2)


slice_page_field = slice_field
