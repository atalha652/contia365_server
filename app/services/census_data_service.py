"""
Census Data Extraction Service for Contia365
Regex-based parser for Spanish tax documents:
  - Modelo 100 (IRPF)
  - Census Tax Declaration
  - Certificado de Situación Censal
No AI or external API required.
"""

import io
import re
from datetime import date, datetime
from typing import Optional, List

import pdfplumber


# ─────────────────────────── helpers ────────────────────────────────────────

def _get(text: str, *patterns: str) -> Optional[str]:
    """Return first regex match group(1), stripped. Case-insensitive."""
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if m:
            return m.group(1).strip()
    return None


def _get_float(text: str, *patterns: str) -> Optional[float]:
    """Extract a numeric value, removing currency symbols and thousand separators."""
    raw = _get(text, *patterns)
    if raw is None:
        return None
    raw = re.sub(r"[€$£\s]", "", raw)
    raw = raw.replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def _normalise_date(d: Optional[str]) -> Optional[str]:
    """Normalise dd/mm/yyyy or dd-mm-yyyy → yyyy-mm-dd. Returns None if unparseable."""
    if not d:
        return None
    d = d.strip()
    m = re.match(r"(\d{2})[/\-](\d{2})[/\-](\d{4})", d)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    if re.match(r"\d{4}-\d{2}-\d{2}", d):
        return d
    return None


def _detect_document_type(text: str) -> str:
    """Detect which Spanish tax document this is."""
    t = text.lower()
    if "modelo 100" in t or "irpf" in t and "cuota" in t:
        return "Modelo 100 (IRPF)"
    if "census tax declaration" in t or "household" in t:
        return "Census Tax Declaration"
    if "situación censal" in t or "certificado censal" in t:
        return "Certificado de Situación Censal"
    return "Unknown"


# ─────────────────────────── text extraction ────────────────────────────────

def _extract_text_from_pdf(file_bytes: bytes) -> str:
    parts = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            # Extract regular text
            t = page.extract_text()
            if t:
                parts.append(t)
            # Extract tables as pipe-separated rows so regex can split on |
            for table in (page.extract_tables() or []):
                for row in table:
                    if not row:
                        continue
                    cells = [str(c).strip() if c else "" for c in row]
                    if any(cells):
                        parts.append(" | ".join(cells))
    return "\n".join(parts)


def _extract_text_from_docx(file_bytes: bytes) -> str:
    try:
        from docx import Document
    except ImportError:
        raise ImportError("Run: pip install python-docx")
    doc = Document(io.BytesIO(file_bytes))
    lines = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            row_text = " | ".join(c.text.strip() for c in row.cells if c.text.strip())
            if row_text:
                lines.append(row_text)
    return "\n".join(lines)


def extract_text_from_file(file_bytes: bytes, content_type: str, filename: str) -> str:
    fname = filename.lower()
    if content_type == "application/pdf" or fname.endswith(".pdf"):
        return _extract_text_from_pdf(file_bytes)
    if content_type in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    ) or fname.endswith((".docx", ".doc")):
        return _extract_text_from_docx(file_bytes)
    raise ValueError(f"Unsupported file type: {content_type or filename}")


# ─────────────────────────── section parsers ────────────────────────────────

def _parse_taxpayer(text: str) -> dict:
    nif = _get(text,
               r"NIF[:\s]+([A-Z0-9]+)",
               r"NIE[:\s]+([A-Z0-9]+)",
               r"NIF/NIE[:\s]+([A-Z0-9]+)")

    name = _get(text,
                r"(?:Head of Household|Name)[:\s]+(.+)",
                r"Nombre[:\s]+(.+)",
                r"Raz[oó]n\s*Social[:\s]+(.+)")

    # address variants
    raw_addr = _get(text,
                    r"(?:Fiscal\s*)?Address[:\s]+(.+)",
                    r"Domicilio[:\s]+(.+)")

    # postal code — look in address or standalone
    postal = _get(text, r"\b(\d{5})\b")

    # city — "City: Madrid" or from address "... Madrid (28013)"
    city = _get(text,
                r"City[:\s]+([A-Za-záéíóúÁÉÍÓÚñÑ\s]+?)(?:\s+Postal|\s*$)",
                r"Ciudad[:\s]+(.+)")
    if not city and raw_addr:
        # try to pull city from "Calle Mayor 45, Madrid (28013)"
        m = re.search(r",\s*([A-Za-záéíóúÁÉÍÓÚñÑ\s]+?)(?:\s*\(?\d{5}\)?)?$", raw_addr)
        if m:
            city = m.group(1).strip()

    province = _get(text, r"Province[:\s]+(.+)", r"Provincia[:\s]+(.+)") or city

    return {
        "nif_nie": nif,
        "full_name": name,
        "fiscal_address": {
            "address_line": raw_addr,
            "postal_code": postal,
            "city": city,
            "province": province,
        },
    }


def _parse_income(text: str) -> Optional[dict]:
    gross = _get_float(text,
                       r"Gross\s*Salary[:\s€]+([\d,\.]+)",
                       r"Salario\s*Bruto[:\s€]+([\d,\.]+)")
    withholdings = _get_float(text,
                              r"Withholdings?[:\s€]+([\d,\.]+)",
                              r"Retenciones?[:\s€]+([\d,\.]+)")
    if gross is None and withholdings is None:
        return None
    return {"gross_salary": gross, "withholdings": withholdings}


def _parse_deductions(text: str) -> Optional[dict]:
    items = []
    # Match table rows like "Primary Residence Deduction  5,000"
    for m in re.finditer(
        r"([\w\s]+(?:Deduction|Deducci[oó]n))\s+([\d,\.]+)",
        text, re.IGNORECASE
    ):
        try:
            items.append({
                "concept": m.group(1).strip(),
                "amount": float(m.group(2).replace(",", "")),
            })
        except ValueError:
            pass

    total = _get_float(text,
                       r"(?:Total\s*)?Deductions?\s*\([^)]*\)[:\s€]+([\d,\.]+)",
                       r"Total\s*Deducciones?[:\s€]+([\d,\.]+)")
    if not items and total is None:
        return None
    return {"items": items, "total_deductions": total}


def _parse_tax_calculation(text: str) -> Optional[dict]:
    taxable_base = _get_float(text,
                              r"Taxable\s*Base[^:]*:[:\s€]+([\d,\.]+)",
                              r"Base\s*Imponible[^:]*:[:\s€]+([\d,\.]+)")
    tax_quota = _get_float(text,
                           r"Tax\s*Quota[^:]*:[:\s€]+([\d,\.]+)",
                           r"Cuota\s*[ÍI]ntegra[^:]*:[:\s€]+([\d,\.]+)")
    final_tax = _get_float(text,
                           r"Final\s*Tax[^:]*:[:\s€]+([\d,\.]+)",
                           r"Cuota\s*L[íi]quida[^:]*:[:\s€]+([\d,\.]+)")
    withholdings_paid = _get_float(text,
                                   r"Withholdings?\s*Paid[:\s€]+([\d,\.]+)",
                                   r"Retenciones?\s*Pagadas?[:\s€]+([\d,\.]+)")

    result_raw = _get(text,
                      r"Result[:\s]+(Refund|Payment|A\s*pagar|A\s*devolver)[:\s€]*([\d,\.]*)",
                      r"Resultado[:\s]+(Refund|Payment|A\s*pagar|A\s*devolver)[:\s€]*([\d,\.]*)")
    result_amount = _get_float(text,
                               r"Result[:\s]+(?:Refund|Payment)[:\s€]*([\d,\.]+)",
                               r"Resultado[:\s]+(?:A\s*pagar|A\s*devolver)[:\s€]*([\d,\.]+)")
    result_type = None
    if result_raw:
        result_type = "Refund" if re.search(r"refund|devolver", result_raw, re.I) else "Payment"

    if all(v is None for v in [taxable_base, tax_quota, final_tax]):
        return None
    return {
        "taxable_base": taxable_base,
        "tax_quota": tax_quota,
        "final_tax": final_tax,
        "withholdings_paid": withholdings_paid,
        "result_amount": result_amount,
        "result_type": result_type,
    }


def _parse_household_members(text: str) -> List[dict]:
    """
    Parse household member rows from the document.
    Handles both space-separated and pipe-separated formats from pdfplumber.

    Expected row format (any separator):
      Carlos Martínez García | 42 | Engineer | 45,000
      Carlos Martínez García   42   Engineer   45,000
    """
    members = []

    # ── Strategy 1: pipe-separated rows (pdfplumber table output) ──
    for line in text.splitlines():
        line = line.strip()
        if "|" in line:
            parts = [p.strip() for p in line.split("|") if p.strip()]
            # skip header row
            if len(parts) >= 3 and not re.search(r"member\s*name|age|occupation|income", line, re.I):
                name = parts[0]
                age_raw = parts[1] if len(parts) > 1 else None
                occupation = parts[2] if len(parts) > 2 else None
                income_raw = parts[3] if len(parts) > 3 else None
                try:
                    members.append({
                        "name": name,
                        "age": int(age_raw) if age_raw and age_raw.isdigit() else None,
                        "occupation": occupation,
                        "annual_income": float(income_raw.replace(",", "")) if income_raw else None,
                    })
                except (ValueError, AttributeError):
                    pass

    if members:
        return members

    # ── Strategy 2: extract the section block then parse line by line ──
    section_match = re.search(
        r"Household\s*Members[:\s]*\n(.*?)(?:\n\s*\n|\nTax\s*Summary|\nHousehold\s*Tax|$)",
        text, re.IGNORECASE | re.DOTALL
    )
    block = section_match.group(1) if section_match else text

    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue
        # skip header
        if re.search(r"member\s*name|age|occupation|income", line, re.I):
            continue

        # Try: "Name  Age  Occupation  Income" with 2+ spaces as separator
        m = re.match(
            r"^([A-Za-záéíóúÁÉÍÓÚñÑ][\w\sáéíóúÁÉÍÓÚñÑ]+?)\s{2,}(\d{1,3})\s{2,}(\w+)\s{2,}([\d,\.]+|0)$",
            line
        )
        if m:
            try:
                members.append({
                    "name": m.group(1).strip(),
                    "age": int(m.group(2)),
                    "occupation": m.group(3).strip(),
                    "annual_income": float(m.group(4).replace(",", "")),
                })
            except ValueError:
                pass
            continue

        # Try tab-separated
        parts = re.split(r"\t+", line)
        if len(parts) >= 3:
            name = parts[0].strip()
            if re.match(r"[A-Za-záéíóúÁÉÍÓÚñÑ]", name):
                try:
                    members.append({
                        "name": name,
                        "age": int(parts[1].strip()) if len(parts) > 1 else None,
                        "occupation": parts[2].strip() if len(parts) > 2 else None,
                        "annual_income": float(parts[3].replace(",", "")) if len(parts) > 3 else None,
                    })
                except (ValueError, IndexError):
                    pass

    return members


def _parse_household_tax_summary(text: str) -> Optional[dict]:
    total_income = _get_float(text,
                              r"Total\s*Household\s*Income[:\s€]+([\d,\.]+)",
                              r"Ingresos\s*Totales[:\s€]+([\d,\.]+)")
    deductions = _get_float(text,
                            r"Deductions?\s*\([^)]*\)[:\s€]+([\d,\.]+)")
    taxable_income = _get_float(text,
                                r"Taxable\s*Income[:\s€]+([\d,\.]+)",
                                r"Renta\s*Imponible[:\s€]+([\d,\.]+)")
    tax_liability = _get_float(text,
                               r"(?:Estimated\s*)?Tax\s*Liability[^:]*:[:\s€]+([\d,\.]+)",
                               r"Cuota\s*IRPF[:\s€]+([\d,\.]+)")
    tax_paid = _get_float(text,
                          r"Tax\s*Paid[:\s€]+([\d,\.]+)",
                          r"Impuesto\s*Pagado[:\s€]+([\d,\.]+)")
    balance_due = _get_float(text,
                             r"Balance\s*Due[:\s€]+([\d,\.]+)",
                             r"Saldo\s*a\s*Pagar[:\s€]+([\d,\.]+)")

    if all(v is None for v in [total_income, taxable_income, tax_liability]):
        return None
    return {
        "total_household_income": total_income,
        "total_deductions": deductions,
        "taxable_income": taxable_income,
        "estimated_tax_liability": tax_liability,
        "tax_paid": tax_paid,
        "balance_due": balance_due,
    }


# ─────────────────────────── main entry point ───────────────────────────────

def parse_census_data_from_text(raw_text: str) -> dict:
    """
    Parse any supported Spanish tax document using regex.
    Returns a dict matching the CensusDataCreate schema.
    """
    doc_type = _detect_document_type(raw_text)

    # Document metadata
    issue_date = _normalise_date(
        _get(raw_text,
             r"Issue\s*Date[:\s]+([\d\-/]+)",
             r"Fecha\s*de\s*Emisi[oó]n[:\s]+([\d\-/]+)")
    )
    csv_code  = _get(raw_text, r"\bCSV[:\s]+([A-Z0-9]+)")
    aeat_ref  = _get(raw_text,
                     r"Reference[:\s]+(\S+)",
                     r"Referencia[:\s]+(\S+)")

    return {
        "document_metadata": {
            "document_type": doc_type,
            "official_name": _get(raw_text,
                                  r"^(Agencia Tributaria[^\n]+)",
                                  r"^(Census Tax Declaration[^\n]+)",
                                  r"^(Certificado[^\n]+)") or doc_type,
            "issue_date": issue_date,
            "csv_code": csv_code,
            "aeat_reference": aeat_ref,
        },
        "taxpayer_identity": _parse_taxpayer(raw_text),
        "income": _parse_income(raw_text),
        "deductions": _parse_deductions(raw_text),
        "tax_calculation": _parse_tax_calculation(raw_text),
        "household_members": _parse_household_members(raw_text),
        "household_tax_summary": _parse_household_tax_summary(raw_text),
        "platform_verification": {
            "verification_status": "PENDING",
            "verified_at": None,
            "needs_renewal_at": None,
        },
    }


def build_ocr_confidence(raw_text: str) -> float:
    """Heuristic confidence score based on key Spanish tax terms found."""
    key_terms = ["NIF", "IRPF", "IVA", "ALTA", "Cuota", "Deducci", "Imponible",
                 "Tributaria", "Censal", "Retenci"]
    hits = sum(1 for t in key_terms if t.lower() in raw_text.lower())
    return round(min(hits / len(key_terms), 1.0), 2)
