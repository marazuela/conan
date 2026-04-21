"""
Deep-dive PDF generator for the Non-US Discovery System.
Mirrors the visual language of the Tool 1 AXSM / Tool 2 WBC deep-dive PDFs.
"""
from __future__ import annotations
import json, os, sys
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, black, white, grey
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, KeepTogether,
)
from reportlab.pdfgen.canvas import Canvas

NAVY = HexColor("#1e2a44")
ACCENT = HexColor("#2b5b84")
LIGHT = HexColor("#f1f3f8")
BORDER = HexColor("#c9cfdb")
DARKGREY = HexColor("#4a4a4a")

styles = getSampleStyleSheet()

STY_H1 = ParagraphStyle(
    "H1", parent=styles["Heading1"], fontName="Helvetica-Bold",
    fontSize=16, textColor=NAVY, spaceAfter=2, leading=19,
)
STY_SUB = ParagraphStyle(
    "Sub", parent=styles["Normal"], fontName="Helvetica-Oblique",
    fontSize=9, textColor=DARKGREY, spaceAfter=10, leading=11,
)
STY_H2 = ParagraphStyle(
    "H2", parent=styles["Heading2"], fontName="Helvetica-Bold",
    fontSize=11, textColor=NAVY, spaceBefore=10, spaceAfter=5, leading=13,
)
STY_BODY = ParagraphStyle(
    "Body", parent=styles["Normal"], fontName="Helvetica",
    fontSize=9.2, leading=12.6, spaceAfter=4, textColor=black,
)
STY_BODY_SM = ParagraphStyle(
    "BodySm", parent=styles["Normal"], fontName="Helvetica",
    fontSize=8.4, leading=11, spaceAfter=3, textColor=DARKGREY,
)
STY_KEY = ParagraphStyle(
    "Key", parent=styles["Normal"], fontName="Helvetica-Bold",
    fontSize=8.6, leading=10.6, textColor=NAVY,
)
STY_VAL = ParagraphStyle(
    "Val", parent=styles["Normal"], fontName="Helvetica",
    fontSize=8.6, leading=10.6, textColor=black,
)


def _on_page(canvas: Canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(DARKGREY)
    canvas.drawString(
        15 * mm, A4[1] - 10 * mm,
        "Non-US Discovery System \u00b7 Deep-Dive \u00b7 NOT INVESTMENT ADVICE \u00b7 For human review before action.",
    )
    canvas.drawRightString(A4[0] - 15 * mm, 10 * mm, f"Page {doc.page}")
    canvas.restoreState()


def _kv_table(pairs):
    rows = []
    for k, v in pairs:
        rows.append([Paragraph(k, STY_KEY), Paragraph(str(v), STY_VAL)])
    t = Table(rows, colWidths=[35 * mm, 135 * mm])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("LINEABOVE", (0, 0), (-1, 0), 0.4, BORDER),
        ("LINEBELOW", (0, -1), (-1, -1), 0.4, BORDER),
    ]))
    return t


def _row_table(headers, rows, col_widths=None):
    data = [[Paragraph(h, STY_KEY) for h in headers]]
    for r in rows:
        data.append([Paragraph(str(c), STY_BODY_SM) for c in r])
    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), LIGHT),
        ("GRID", (0, 0), (-1, -1), 0.3, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


def build_pdf(path: str, dd: dict):
    doc = SimpleDocTemplate(
        path, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=17 * mm, bottomMargin=15 * mm,
        title=dd["title"],
    )
    story = []
    story.append(Paragraph(dd["title"], STY_H1))
    story.append(Paragraph(dd["subtitle"], STY_SUB))
    story.append(_kv_table(dd["header_kv"]))
    story.append(Spacer(1, 6))

    story.append(Paragraph("Thesis (TL;DR)", STY_H2))
    story.append(Paragraph(dd["tldr"], STY_BODY))

    story.append(Paragraph("Company context", STY_H2))
    for line in dd["company_context"]:
        story.append(Paragraph(line, STY_BODY))

    story.append(Paragraph("What the release actually contains", STY_H2))
    for line in dd["release_contents"]:
        story.append(Paragraph(line, STY_BODY))

    story.append(Paragraph("Thesis statement", STY_H2))
    story.append(Paragraph(dd["thesis"], STY_BODY))

    story.append(Paragraph("Catalyst map", STY_H2))
    story.append(_row_table(
        ["Event", "Date / Window", "Entry trigger", "Exit / resolution"],
        dd["catalyst_rows"],
        col_widths=[55 * mm, 30 * mm, 43 * mm, 42 * mm],
    ))

    story.append(Paragraph("Steelman of the opposite view", STY_H2))
    for line in dd["steelman"]:
        story.append(Paragraph("\u2022 " + line, STY_BODY))

    story.append(Paragraph("Kill conditions", STY_H2))
    story.append(_row_table(
        ["Kill condition", "Numeric / factual threshold", "Where observable"],
        dd["kill_rows"],
        col_widths=[60 * mm, 65 * mm, 45 * mm],
    ))

    story.append(Paragraph("Peer comparables", STY_H2))
    for line in dd["peer_comparables"]:
        story.append(Paragraph("\u2022 " + line, STY_BODY))

    story.append(Paragraph("Position sizing guidance", STY_H2))
    story.append(Paragraph(dd["position_sizing"], STY_BODY))

    story.append(Paragraph("Sources & traceability", STY_H2))
    for line in dd["sources"]:
        story.append(Paragraph("\u2022 " + line, STY_BODY_SM))

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
