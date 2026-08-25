"""Shared AEAT diseño-de-registro helpers (T-page wrapper used by 303/130/111/115/390)."""

from __future__ import annotations

import os
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Union

from app.models.tax_engine import Quarter


class ModeloFileError(ValueError):
    pass


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
    "0A": "0A",
    "ANNUAL": "0A",
}


def cents(value: Union[float, Decimal, int, None]) -> int:
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int((amount * 100).to_integral_value(rounding=ROUND_HALF_UP))


def num(value: Union[float, Decimal, int, None], length: int) -> str:
    return str(abs(cents(value))).zfill(length)


def signed(value: Union[float, Decimal, int, None], length: int) -> str:
    amount = cents(value)
    if amount < 0:
        return "N" + str(abs(amount)).zfill(length - 1)
    return str(amount).zfill(length)


def an(value: str, length: int) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 ]", " ", (value or "").upper())
    return cleaned[:length].ljust(length)


def digits(value, length: int) -> str:
    raw = re.sub(r"\D", "", str(value or "0")) or "0"
    return raw[-length:].zfill(length)


def put(buf: list, pos: int, text: str) -> None:
    start = pos - 1
    end = start + len(text)
    if end > len(buf):
        raise ModeloFileError(
            f"Field at {pos} length {len(text)} overruns buffer {len(buf)}"
        )
    buf[start:end] = list(text)


def normalize_period(quarter: Union[Quarter, str, None], *, annual: bool = False) -> str:
    if annual:
        return "0A"
    key = quarter if isinstance(quarter, str) else quarter
    period = _QUARTER_TO_PERIOD.get(key) or _QUARTER_TO_PERIOD.get(str(quarter))
    if not period:
        raise ModeloFileError(f"Unsupported period: {quarter!r}")
    return period


def require_nif(nif: str) -> str:
    nif = (nif or "").replace(" ", "").upper()
    if not re.match(r"^[A-Z0-9]{8,9}$", nif):
        raise ModeloFileError("A valid 8–9 character NIF is required")
    return nif


def require_name(name: str) -> str:
    if not (name or "").strip():
        raise ModeloFileError("Declarant name is required")
    return name.strip()


def declaration_type_ingreso(amount: float) -> str:
    if amount > 0:
        return "I"
    if amount < 0:
        return "C"
    return "N"


def page_open(modelo: str, page: str = "01") -> str:
    return f"<T{modelo}{page}000>"


def page_close(modelo: str, page: str = "01") -> str:
    return f"</T{modelo}{page}000>"


def build_wrapper(*, modelo: str, year: int, period: str, pages: str) -> str:
    year_s = str(int(year)).zfill(4)
    period_s = period.ljust(2)[:2]
    version = (os.getenv("MODELO_SW_VERSION") or os.getenv("MODELO_303_SW_VERSION") or "0101")[:4].ljust(4)
    ed_nif = an(
        os.getenv("MODELO_DEVELOPER_NIF") or os.getenv("MODELO_303_DEVELOPER_NIF") or "",
        9,
    )
    header = f"<T{modelo}0{year_s}{period_s}0000>"
    aux = "<AUX>" + (" " * 70) + version + (" " * 4) + ed_nif + (" " * 213) + "</AUX>"
    close = f"</T{modelo}0{year_s}{period_s}0000>"
    return header + aux + pages + close


def write_identity(
    buf: list,
    *,
    modelo: str,
    nif: str,
    name: str,
    year: int,
    period: str,
    declaration_type: str,
    surname_len: int = 60,
    given_len: int = 20,
) -> None:
    """Positions 1–108 used by 130/111/115 (and 390 page 1)."""
    put(buf, 1, "<T")
    put(buf, 3, modelo)
    put(buf, 6, "01")
    put(buf, 8, "000>")
    put(buf, 12, " ")
    put(buf, 13, (declaration_type or "I")[:1])
    put(buf, 14, an(nif, 9))
    put(buf, 23, an(name, surname_len))
    put(buf, 23 + surname_len, an("", given_len))
    put(buf, 103, str(int(year)).zfill(4))
    put(buf, 107, period.ljust(2)[:2])


def slice_field(page: str, pos: int, length: int) -> str:
    start = pos - 1
    return page[start : start + length]
