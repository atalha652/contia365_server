"""Modelo 111 official file (quarterly IRPF withholdings — needs percipient lines)."""

from __future__ import annotations

from typing import Optional, Union

from app.models.tax_engine import Modelo111Results, Quarter
from app.services.modelo_boe_common import (
    ModeloFileError,
    build_wrapper,
    declaration_type_ingreso,
    digits,
    normalize_period,
    num,
    put,
    require_name,
    require_nif,
    signed,
    slice_field,
    write_identity,
)

PAGE_LEN = 1000


def _split_keys(totals: Modelo111Results) -> dict:
    work_count = work_base = work_ret = 0.0
    ae_count = ae_base = ae_ret = 0.0
    lines = list(getattr(totals, "lines", None) or [])
    if lines:
        for line in lines:
            if isinstance(line, dict):
                key = str(line.get("perception_key") or "G").upper()[:1]
                base = float(line.get("base_amount") or 0)
                ret = float(line.get("withheld_amount") or 0)
            else:
                key = str(getattr(line, "perception_key", "G") or "G").upper()[:1]
                base = float(getattr(line, "base_amount", 0) or 0)
                ret = float(getattr(line, "withheld_amount", 0) or 0)
            if key == "A":
                work_count += 1
                work_base += base
                work_ret += ret
            else:
                ae_count += 1
                ae_base += base
                ae_ret += ret
    else:
        ae_count = float(totals.percipient_count or 0)
        ae_base = float(totals.total_base or 0)
        ae_ret = float(totals.total_withheld or totals.withholding_payable or 0)
    return {
        "work_count": int(work_count),
        "work_base": round(work_base, 2),
        "work_ret": round(work_ret, 2),
        "ae_count": int(ae_count),
        "ae_base": round(ae_base, 2),
        "ae_ret": round(ae_ret, 2),
        "total_ret": round(work_ret + ae_ret, 2),
    }


def build_page_01(
    *,
    nif: str,
    name: str,
    year: int,
    period: str,
    totals: Modelo111Results,
    declaration_type: Optional[str] = None,
) -> str:
    split = _split_keys(totals)
    payable = float(totals.withholding_payable or split["total_ret"] or 0)
    buf = [" "] * PAGE_LEN
    write_identity(
        buf, modelo="111", nif=nif, name=name, year=year, period=period,
        declaration_type=declaration_type or declaration_type_ingreso(payable),
    )
    # [01]-[03] trabajo dinerario; [07]-[09] actividades económicas dinerarias.
    put(buf, 109, digits(split["work_count"], 8))
    put(buf, 117, signed(split["work_base"], 17))
    put(buf, 134, signed(split["work_ret"], 17))
    put(buf, 151, digits(0, 8))  # [04]
    put(buf, 159, signed(0, 17))
    put(buf, 176, signed(0, 17))
    put(buf, 193, digits(split["ae_count"], 8))  # [07]
    put(buf, 201, signed(split["ae_base"], 17))
    put(buf, 218, signed(split["ae_ret"], 17))
    pos = 235
    for _ in range(6):  # [10]-[27] unused groups
        put(buf, pos, digits(0, 8))
        put(buf, pos + 8, signed(0, 17))
        put(buf, pos + 25, signed(0, 17))
        pos += 42
    put(buf, 487, signed(split["total_ret"], 17))  # [28]
    put(buf, 504, signed(0, 17))  # [29]
    put(buf, 521, signed(payable, 17))  # [30]
    put(buf, 538, " ")
    put(buf, 539, " " * 13)
    put(buf, 552, " ")
    put(buf, 553, " " * 34)
    put(buf, 587, " " * 389)
    put(buf, 976, " " * 13)
    put(buf, 989, "</T11101000>")
    return "".join(buf)


def build_modelo_111_file(
    *,
    nif: str,
    name: str,
    year: int,
    quarter: Union[Quarter, str],
    totals: Modelo111Results,
    declaration_type: Optional[str] = None,
) -> str:
    nif = require_nif(nif)
    name = require_name(name)
    if not (totals.legally_complete and (totals.lines or totals.percipient_count)):
        raise ModeloFileError(
            "Modelo 111 is legally incomplete without employee (percipient) records."
        )
    period = normalize_period(quarter)
    page = build_page_01(
        nif=nif, name=name, year=year, period=period, totals=totals,
        declaration_type=declaration_type,
    )
    if len(page) != PAGE_LEN:
        raise ModeloFileError(f"DP11101 length {len(page)} != {PAGE_LEN}")
    return build_wrapper(modelo="111", year=year, period=period, pages=page)


slice_page_field = slice_field
