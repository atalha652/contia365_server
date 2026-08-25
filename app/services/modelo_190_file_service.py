"""Modelo 190 annual withholding summary (BOE tipo 1 + tipo 2 perceptor records).

This is NOT an annual income return. Each tipo-2 line is one percipient.
"""

from __future__ import annotations

from typing import Iterable, Optional

from app.models.tax_engine import Modelo190Results
from app.services.modelo_boe_common import (
    ModeloFileError,
    an,
    digits,
    num,
    require_name,
    require_nif,
)

TIPO1_LEN = 500
TIPO2_LEN = 500


def _lines(totals: Modelo190Results) -> list:
    return list(getattr(totals, "lines", None) or [])


def _line_get(line, key, default=""):
    if isinstance(line, dict):
        return line.get(key, default)
    return getattr(line, key, default)


def build_tipo_1(
    *,
    nif: str,
    name: str,
    year: int,
    totals: Modelo190Results,
    contact_phone: str = "",
    contact_name: str = "",
    contact_email: str = "",
) -> str:
    buf = [" "] * TIPO1_LEN
    count = int(totals.percipient_count or len(_lines(totals)))
    base = float(totals.total_base or 0)
    withheld = float(totals.total_withheld or 0)

    def _put(pos: int, text: str) -> None:
        buf[pos - 1 : pos - 1 + len(text)] = list(text)

    _put(1, "1")
    _put(2, "190")
    _put(5, str(int(year)).zfill(4))
    _put(9, an(nif, 9))
    _put(18, an(name, 40))
    _put(58, "T")
    _put(59, digits(contact_phone or "0", 9))
    _put(68, an(contact_name or name, 40))
    _put(108, digits(0, 13))
    _put(121, " ")
    _put(122, " ")
    _put(123, digits(0, 13))
    _put(136, digits(count, 9))  # [01]
    _put(145, num(base, 16))  # [02]
    _put(161, num(withheld, 15))  # [03]
    _put(176, an(contact_email, 50))
    return "".join(buf)


def build_tipo_2(
    *,
    declarant_nif: str,
    year: int,
    line,
) -> str:
    buf = [" "] * TIPO2_LEN

    def _put(pos: int, text: str) -> None:
        buf[pos - 1 : pos - 1 + len(text)] = list(text)

    nif = require_nif(str(_line_get(line, "nif") or ""))
    name = an(str(_line_get(line, "full_name") or ""), 40)
    key = str(_line_get(line, "perception_key") or "G").upper()[:1] or "G"
    subkey = str(_line_get(line, "perception_subkey") or "01").zfill(2)[:2]
    province = str(_line_get(line, "province_code") or "00").zfill(2)[:2]
    base = float(_line_get(line, "base_amount") or 0)
    withheld = float(_line_get(line, "withheld_amount") or 0)
    _put(1, "2")
    _put(2, "190")
    _put(5, str(int(year)).zfill(4))
    _put(9, an(declarant_nif, 9))
    _put(18, an(nif, 9))
    _put(27, " " * 9)
    _put(36, name)
    _put(76, province)
    _put(78, key)
    _put(79, subkey)
    _put(81, "N" if base < 0 else " ")
    _put(82, num(abs(base), 13))
    _put(95, num(abs(withheld), 13))
    return "".join(buf)


def build_modelo_190_file(
    *,
    nif: str,
    name: str,
    year: int,
    totals: Modelo190Results,
    contact_phone: str = "",
    contact_name: str = "",
    contact_email: str = "",
    quarter=None,
) -> str:
    nif = require_nif(nif)
    name = require_name(name)
    lines = _lines(totals)
    if not lines:
        raise ModeloFileError(
            "Modelo 190 is legally incomplete without employee (percipient) records."
        )
    tipo1 = build_tipo_1(
        nif=nif, name=name, year=year, totals=totals,
        contact_phone=contact_phone, contact_name=contact_name,
        contact_email=contact_email,
    )
    if len(tipo1) != TIPO1_LEN:
        raise ModeloFileError(f"190 tipo 1 length {len(tipo1)} != {TIPO1_LEN}")
    records = [tipo1]
    for line in lines:
        rec = build_tipo_2(declarant_nif=nif, year=year, line=line)
        if len(rec) != TIPO2_LEN:
            raise ModeloFileError(f"190 tipo 2 length {len(rec)} != {TIPO2_LEN}")
        records.append(rec)
    return "\r\n".join(records) + "\r\n"
