"""Receipt PDF built from stored AEAT modelo fields (not an official AEAT file)."""

from datetime import datetime
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.services.aeat_result_messages import enrich_aeat_result


def _text(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    text = str(value).strip()
    return text or "—"


def build_justificante_pdf(filing: dict) -> bytes:
    aeat = enrich_aeat_result(filing.get("aeat_result") or {})
    status = str(filing.get("status") or "")
    modelo = filing.get("modelo") or "303"
    title = (
        f"AEAT justificant — Modelo {modelo}"
        if status == "ACCEPTED"
        else "AEAT result — tax filing"
    )

    buffer = BytesIO()
    pdf = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=40,
        leftMargin=40,
        topMargin=40,
        bottomMargin=40,
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="ReceiptTitle",
        parent=styles["Title"],
        fontSize=16,
        textColor=colors.HexColor("#003366"),
        alignment=TA_CENTER,
        spaceAfter=16,
    ))
    styles.add(ParagraphStyle(
        name="ReceiptNote",
        parent=styles["Normal"],
        fontSize=9,
        textColor=colors.HexColor("#555555"),
        alignment=TA_LEFT,
        spaceBefore=18,
    ))

    rows = [
        ["Modelo", _text(filing.get("modelo"))],
        ["Period", f"{_text(filing.get('quarter'))} {_text(filing.get('year'))}"],
        ["Status", _text(status)],
        ["AEAT code", _text(aeat.get("code"))],
        ["Message", _text(aeat.get("message"))],
        ["CSV", _text(aeat.get("csv"))],
        ["Justificante", _text(aeat.get("justificante"))],
        ["Submitted at", _text(filing.get("submitted_at") or aeat.get("recorded_at"))],
    ]
    table = Table(
        [[Paragraph(escape(a), styles["Normal"]), Paragraph(escape(b), styles["Normal"])] for a, b in rows],
        colWidths=[140, 360],
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F2F4F7")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D0D5DD")),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))

    elements = [
        Paragraph(escape(title), styles["ReceiptTitle"]),
        table,
        Spacer(1, 12),
        Paragraph(
            escape(
                "This is a Contia receipt built from stored AEAT fields (code, message, CSV, justificante). "
                "It is not an official AEAT PDF. Accepted by AEAT is not paid."
            ),
            styles["ReceiptNote"],
        ),
    ]
    pdf.build(elements)
    buffer.seek(0)
    return buffer.read()
