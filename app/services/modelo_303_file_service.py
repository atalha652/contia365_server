"""
Modelo 303 official file (diseño de registro 2026 v1.01).

AEAT publishes a fixed-width BOE / .303 layout — not Facturae XML.
Casilla numbers and field positions come from DR303e26v101.xlsx
(DP30300 wrapper + DP30301 régimen general page).

Recargo de equivalencia, ISP and OSS are left empty (out of scope).
"""

from __future__ import annotations

import os
import re
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Mapping, Optional, Union

from app.models.tax_engine import Modelo303Results, Quarter, VatByRateBucket

SCHEMA_DIR = (
    Path(__file__).resolve().parents[1] / "schemas" / "modelo_303" / "2026"
)
OFFICIAL_DISENO_PATH = SCHEMA_DIR / "DR303e26v101.xlsx"

PAGE1_LEN = 1581  # DP30301: last field starts at 1570, length 12

_QUARTER_TO_PERIOD = {
    Quarter.Q1: "1T",
    Quarter.Q2: "2T",
    Quarter.Q3: "3T",
    Quarter.Q4: "4T",
    "Q1": "1T",
    "Q2": "2T",
    "Q3": "3T",
    "Q4": "4T",
    "1T": "1T",
    "2T": "2T",
    "3T": "3T",
    "4T": "4T",
}
for _month in range(1, 13):
    _mm = f"{_month:02d}"
    _QUARTER_TO_PERIOD[_mm] = _mm
    _QUARTER_TO_PERIOD[str(_month)] = _mm
    _QUARTER_TO_PERIOD[f"M{_mm}"] = _mm
    _QUARTER_TO_PERIOD[f"M{_month}"] = _mm
    _QUARTER_TO_PERIOD[_month] = _mm


class Modelo303FileError(ValueError):
    pass


def _cents(value: Union[float, Decimal, int, None]) -> int:
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int((amount * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _num(value: Union[float, Decimal, int, None], length: int) -> str:
    return str(abs(_cents(value))).zfill(length)


def _signed(value: Union[float, Decimal, int, None], length: int) -> str:
    cents = _cents(value)
    if cents < 0:
        return "N" + str(abs(cents)).zfill(length - 1)
    return str(cents).zfill(length)


def _an(value: str, length: int) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 ]", " ", (value or "").upper())
    return cleaned[:length].ljust(length)


def _put(buf: list, pos: int, text: str) -> None:
    """Write `text` at 1-based `pos` into a mutable character buffer."""
    start = pos - 1
    end = start + len(text)
    if end > len(buf):
        raise Modelo303FileError(f"Field at {pos} length {len(text)} overruns buffer {len(buf)}")
    buf[start:end] = list(text)


def normalize_period(quarter: Union[Quarter, str, int, None]) -> str:
    if quarter is None or quarter == "":
        raise Modelo303FileError("Unsupported 303 period: None")
    raw = quarter if isinstance(quarter, str) else quarter
    text = str(raw).strip().upper()
    if len(text) >= 7 and text[4:5] == "-" and text[-2:].isdigit():
        month = int(text[-2:])
        if 1 <= month <= 12:
            return f"{month:02d}"
    period = _QUARTER_TO_PERIOD.get(raw) or _QUARTER_TO_PERIOD.get(text)
    if isinstance(raw, int) and 1 <= raw <= 12:
        period = f"{raw:02d}"
    if not period:
        raise Modelo303FileError(f"Unsupported 303 period: {quarter!r}")
    return period


def _bucket(totals: Modelo303Results, rate: str) -> VatByRateBucket:
    raw = (totals.vat_by_rate or {}).get(rate) or (totals.vat_by_rate or {}).get(str(int(float(rate))))
    if isinstance(raw, VatByRateBucket):
        return raw
    if isinstance(raw, Mapping):
        return VatByRateBucket(**raw)
    return VatByRateBucket()


def _declaration_type(vat_payable: float) -> str:
    if vat_payable > 0:
        return "I"
    if vat_payable < 0:
        return "C"
    return "N"


def _is_last_period(period: str) -> bool:
    return period in ("4T", "12")


def build_page_01(
    *,
    nif: str,
    name: str,
    year: int,
    period: str,
    totals: Modelo303Results,
    declaration_type: Optional[str] = None,
    redeme: bool = False,
) -> str:
    """Build DP30301 (page 01000) — identification + régimen general liquidation."""
    buf = [" "] * PAGE1_LEN
    _put(buf, 1, "<T")
    _put(buf, 3, "303")
    _put(buf, 6, "01000")
    _put(buf, 11, ">")
    _put(buf, 12, " ")
    tipo = (declaration_type or _declaration_type(totals.vat_payable))[:1]
    _put(buf, 13, tipo)
    _put(buf, 14, _an(nif, 9))
    _put(buf, 23, _an(name, 80))
    _put(buf, 103, str(int(year)).zfill(4))
    _put(buf, 107, period.ljust(2)[:2])

    # Identification flags: 1=SI 2=NO; 3 on simplified field = sólo régimen general.
    # 390 / art.121 use 0 on non-final periods (diseño notas 3–4).
    last = _is_last_period(period)
    _put(buf, 109, "2")  # foral
    _put(buf, 110, "1" if redeme else "2")  # REDEME
    _put(buf, 111, "3")  # not simplified — sólo RG
    _put(buf, 112, "2")  # conjunta
    _put(buf, 113, "2")  # criterio de caja
    _put(buf, 114, "2")  # destinatario caja
    _put(buf, 115, "2")  # prorrata especial
    _put(buf, 116, "2")  # revocación prorrata
    _put(buf, 117, "2")  # concurso
    _put(buf, 118, " " * 8)  # bankruptcy date
    _put(buf, 126, " ")  # pre/post concurso
    _put(buf, 127, "2")  # SII
    # REDEME monthly filers are exonerated from 390; others file 390 on the last period.
    if redeme:
        _put(buf, 128, "1" if last else "0")
    else:
        _put(buf, 128, "2" if last else "0")  # not exempt from 390
    _put(buf, 129, "1" if last else "0")  # art. 121 turnover ≠ 0 (final period only)
    _put(buf, 130, "2")  # gasolinas

    # Numeric liquidation block: zeros, then overwrite used casillas and rate constants.
    _put(buf, 131, "0" * (1036 - 131))

    # Rate % constants from the diseño (zeros if unused, constant still present).
    _put(buf, 148, "00000")  # [151] 0%
    _put(buf, 187, "00000")  # [166]
    _put(buf, 226, "00400")  # [02] 4%
    _put(buf, 265, "00000")  # [154]
    _put(buf, 304, "01000")  # [05] 10%
    _put(buf, 343, "02100")  # [08] 21%

    b0 = _bucket(totals, "0")
    b4 = _bucket(totals, "4")
    b10 = _bucket(totals, "10")
    b21 = _bucket(totals, "21")

    _put(buf, 131, _num(b0.output_base, 17))
    _put(buf, 153, _num(b0.output_vat, 17))
    _put(buf, 209, _num(b4.output_base, 17))
    _put(buf, 231, _num(b4.output_vat, 17))
    _put(buf, 287, _num(b10.output_base, 17))
    _put(buf, 309, _num(b10.output_vat, 17))
    _put(buf, 326, _num(b21.output_base, 17))
    _put(buf, 348, _num(b21.output_vat, 17))

    output_vat = round(
        b0.output_vat + b4.output_vat + b10.output_vat + b21.output_vat,
        2,
    )
    if output_vat == 0:
        output_vat = round(float(totals.output_vat or 0), 2)

    input_base = round(
        b0.input_base + b4.input_base + b10.input_base + b21.input_base,
        2,
    )
    input_vat = round(
        b0.input_vat + b4.input_vat + b10.input_vat + b21.input_vat,
        2,
    )
    if input_vat == 0:
        input_vat = round(float(totals.input_vat or 0), 2)
        input_base = round(float(totals.total_expenses or input_base), 2)

    _put(buf, 696, _signed(output_vat, 17))  # [27]
    _put(buf, 713, _num(input_base, 17))  # [28]
    _put(buf, 730, _num(input_vat, 17))  # [29]
    _put(buf, 1002, _signed(input_vat, 17))  # [45]
    result = round(output_vat - input_vat, 2)
    _put(buf, 1019, _signed(result, 17))  # [46]

    _put(buf, 1570, "</T30301000>")
    return "".join(buf)


def build_wrapper(
    *,
    year: int,
    period: str,
    pages: str,
    program_version: Optional[str] = None,
    developer_nif: Optional[str] = None,
) -> str:
    """DP30300 envelope around page records."""
    year_s = str(int(year)).zfill(4)
    period_s = period.ljust(2)[:2]
    version = (program_version or os.getenv("MODELO_303_SW_VERSION") or "0101")[:4].ljust(4)
    ed_nif = _an(developer_nif or os.getenv("MODELO_303_DEVELOPER_NIF") or "", 9)

    header = f"<T3030{year_s}{period_s}0000>"
    aux = "<AUX>" + (" " * 70) + version + (" " * 4) + ed_nif + (" " * 213) + "</AUX>"
    close = f"</T3030{year_s}{period_s}0000>"
    return header + aux + pages + close


def build_modelo_303_file(
    *,
    nif: str,
    name: str,
    year: int,
    quarter: Union[Quarter, str],
    totals: Modelo303Results,
    declaration_type: Optional[str] = None,
    program_version: Optional[str] = None,
    developer_nif: Optional[str] = None,
    redeme: bool = False,
) -> str:
    """
    Return the official 2026 Modelo 303 file (BOE / diseño de registro).

    This is the artifact AEAT Importar expects — not Facturae XML.
    """
    nif = (nif or "").replace(" ", "").upper()
    if not re.match(r"^[A-Z0-9]{8,9}$", nif):
        raise Modelo303FileError("A valid 8–9 character NIF is required")
    if not (name or "").strip():
        raise Modelo303FileError("Declarant name is required")

    period = normalize_period(quarter)
    page1 = build_page_01(
        nif=nif,
        name=name,
        year=year,
        period=period,
        totals=totals,
        declaration_type=declaration_type,
        redeme=redeme,
    )
    if len(page1) != PAGE1_LEN:
        raise Modelo303FileError(f"DP30301 length {len(page1)} != {PAGE1_LEN}")
    return build_wrapper(
        year=year,
        period=period,
        pages=page1,
        program_version=program_version,
        developer_nif=developer_nif,
    )


def slice_page_field(page: str, pos: int, length: int) -> str:
    """Read a 1-based field from a generated DP30301 page (tests / debugging)."""
    start = pos - 1
    return page[start : start + length]
