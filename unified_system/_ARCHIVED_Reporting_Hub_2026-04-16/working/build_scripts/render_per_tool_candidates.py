"""Render one per-tool candidates PDF for each source_tool.

Writes:
  Reporting Hub/Candidates/per_tool/<tool>_candidates.pdf
"""
from __future__ import annotations
import os, json, datetime
from reportlab.lib.pagesizes import letter
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, PageBreak,
                                Table, TableStyle, KeepTogether)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT

HUB = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REG_PATH = os.path.join(HUB, "Candidates", "candidates_index.json")
OUT_DIR  = os.path.join(HUB, "Candidates", "per_tool")
os.makedirs(OUT_DIR, exist_ok=True)

TOOL_LABELS = {
    "investment_tool": "Investment Discovery (US)",
    "non_us":          "Non-US Discovery",
    "litigation":      "Litigation & Dockets",
    "silence":         "Silence (behavioral)",
}
ALL_TOOLS = ["investment_tool", "non_us", "litigation", "silence"]

def _fmt_dates(kd_list):
    if not kd_list: return "&mdash;"
    return "<br/>".join(f"<b>{k.get('date') or 'TBD'}</b> — {k.get('event','')}" for k in kd_list[:4])

def render_tool(tool, entries):
    ss = getSampleStyleSheet()
    out_path = os.path.join(OUT_DIR, f"{tool}_candidates.pdf")
    tmp = out_path + ".tmp"

    doc = SimpleDocTemplate(tmp, pagesize=letter,
        leftMargin=0.65*inch, rightMargin=0.65*inch,
        topMargin=0.7*inch, bottomMargin=0.65*inch,
        title=f"{TOOL_LABELS.get(tool,tool)} — Candidates",
        author="Reporting Hub")

    title = ParagraphStyle("T", parent=ss["Title"], fontSize=17, leading=21, spaceAfter=4)
    subt  = ParagraphStyle("S", parent=ss["Normal"], fontSize=9.5,
                           textColor=colors.HexColor("#666"), spaceAfter=12)
    h2    = ParagraphStyle("H2", parent=ss["Heading3"], fontSize=12, spaceBefore=10, spaceAfter=4,
                           textColor=colors.HexColor("#1f3a5f"))
    body  = ParagraphStyle("B", parent=ss["Normal"], fontSize=9.5, leading=13, alignment=TA_LEFT)
    label = ParagraphStyle("L", parent=ss["Normal"], fontSize=9, leading=12,
                           textColor=colors.HexColor("#1f3a5f"), fontName="Helvetica-Bold",
                           spaceBefore=4, spaceAfter=2)
    small = ParagraphStyle("SM", parent=ss["Normal"], fontSize=8.5, leading=11)

    today = datetime.date.today().isoformat()

    story = []
    story.append(Paragraph(f"{TOOL_LABELS.get(tool,tool)} &mdash; Candidates", title))
    story.append(Paragraph(f"{today} &middot; {len(entries)} candidates", subt))

    if not entries:
        story.append(Paragraph("<i>No active candidates for this tool. This file will populate on the next deep-dive run.</i>", body))
    else:
        for e in sorted(entries, key=lambda x: x["ticker"]):
            block = []
            block.append(Paragraph(f"{e['ticker']}", h2))
            block.append(Paragraph("Hypothesis", label))
            hyp = e.get("hypothesis") or "<i>[backfill pending on next deep-dive run]</i>"
            block.append(Paragraph(hyp, body))
            block.append(Paragraph("Next key dates", label))
            block.append(Paragraph(_fmt_dates(e.get("next_key_dates") or []), body))
            meta = []
            if e.get("catalyst_category"): meta.append(f"<b>Category:</b> {e['catalyst_category']}")
            if e.get("status"):            meta.append(f"<b>Status:</b> {e['status']}")
            if e.get("conviction"):        meta.append(f"<b>Conviction:</b> {e['conviction']}")
            if meta:
                block.append(Paragraph(" &middot; ".join(meta), small))
            if e.get("pdf_path"):
                block.append(Paragraph(f"<font color='#888'>Deep-dive: {e['pdf_path']}</font>", small))
            story.append(KeepTogether(block))
            story.append(Spacer(1, 4))

    doc.build(story)
    os.replace(tmp, out_path)
    print(f"Wrote {out_path} ({len(entries)} candidates)")

def render_all():
    reg = json.load(open(REG_PATH))
    entries = reg["theses"]
    for tool in ALL_TOOLS:
        te = [e for e in entries if e["source_tool"] == tool]
        render_tool(tool, te)

if __name__ == "__main__":
    render_all()
