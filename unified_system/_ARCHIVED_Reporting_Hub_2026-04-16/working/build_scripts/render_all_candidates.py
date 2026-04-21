"""Render the master ALL_CANDIDATES.pdf from Candidates/candidates_index.json.

Writes:
  Reporting Hub/Candidates/ALL_CANDIDATES.pdf
  Reporting Hub/Candidates/ALL_CANDIDATES.md
"""
from __future__ import annotations
import os, json, datetime
from reportlab.lib.pagesizes import letter
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, PageBreak,
                                Table, TableStyle, KeepTogether)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_JUSTIFY

HUB = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REG_PATH = os.path.join(HUB, "Candidates", "candidates_index.json")
OUT_PDF  = os.path.join(HUB, "Candidates", "ALL_CANDIDATES.pdf")
OUT_MD   = os.path.join(HUB, "Candidates", "ALL_CANDIDATES.md")

TOOL_LABELS = {
    "investment_tool": "Investment Discovery (US)",
    "non_us":          "Non-US Discovery",
    "litigation":      "Litigation & Dockets",
    "silence":         "Silence (behavioral)",
}
TOOL_ORDER = ["investment_tool", "non_us", "litigation", "silence"]

def _fmt_dates(kd_list):
    if not kd_list: return "&mdash;"
    parts = []
    for kd in kd_list[:3]:
        d = kd.get("date") or "TBD"
        e = kd.get("event", "")
        parts.append(f"<b>{d}</b> — {e}")
    return "<br/>".join(parts)

def _atomic_write(path, bytes_or_str, binary=False):
    tmp = path + ".tmp"
    mode = "wb" if binary else "w"
    with open(tmp, mode) as f:
        f.write(bytes_or_str)
    os.replace(tmp, path)

def render():
    reg = json.load(open(REG_PATH))
    entries = reg["theses"]
    today = datetime.date.today().isoformat()

    # ------- PDF -------
    doc = SimpleDocTemplate(OUT_PDF + ".tmp", pagesize=letter,
        leftMargin=0.6*inch, rightMargin=0.6*inch,
        topMargin=0.65*inch, bottomMargin=0.6*inch,
        title="All Candidates — Master Summary",
        author="Reporting Hub")

    ss = getSampleStyleSheet()
    title = ParagraphStyle("T", parent=ss["Title"], fontSize=18, leading=22, spaceAfter=4)
    subt  = ParagraphStyle("S", parent=ss["Normal"], fontSize=9.5, textColor=colors.HexColor("#666"), spaceAfter=14)
    toolh = ParagraphStyle("TH", parent=ss["Heading2"], fontSize=13, leading=16,
                           spaceBefore=14, spaceAfter=6, textColor=colors.HexColor("#1f3a5f"))
    cellb = ParagraphStyle("CB", parent=ss["Normal"], fontSize=8.5, leading=11, alignment=TA_LEFT)
    cellh = ParagraphStyle("CH", parent=ss["Normal"], fontSize=8.5, leading=11, alignment=TA_LEFT,
                           textColor=colors.white, fontName="Helvetica-Bold")

    story = []
    story.append(Paragraph("All Candidates &mdash; Master Summary", title))
    total = len(entries)
    by_tool = {}
    for e in entries:
        by_tool.setdefault(e["source_tool"], 0); by_tool[e["source_tool"]] += 1
    breakdown = " &middot; ".join(f"{TOOL_LABELS.get(k,k)}: {v}" for k,v in sorted(by_tool.items()))
    story.append(Paragraph(f"{today} &middot; {total} active candidates &middot; {breakdown}", subt))

    for tool in TOOL_ORDER:
        tool_entries = [e for e in entries if e["source_tool"] == tool]
        if not tool_entries: continue
        story.append(Paragraph(f"{TOOL_LABELS[tool]} &mdash; {len(tool_entries)} candidates", toolh))

        # Header row + data rows
        header = [Paragraph(h, cellh) for h in
                  ["Ticker", "Hypothesis", "Next key dates", "Status", "Conviction"]]
        rows = [header]
        for e in sorted(tool_entries, key=lambda x: x["ticker"]):
            ticker = e["ticker"]
            hyp = e.get("hypothesis") or "<i>[backfill pending on next deep-dive run]</i>"
            kd = _fmt_dates(e.get("next_key_dates") or [])
            status = e.get("status") or "—"
            conv = e.get("conviction") or "—"
            cat = e.get("catalyst_category") or ""
            ticker_cell = f"<b>{ticker}</b>"
            if cat:
                ticker_cell += f"<br/><font size=7 color='#888'>{cat}</font>"
            rows.append([
                Paragraph(ticker_cell, cellb),
                Paragraph(hyp, cellb),
                Paragraph(kd, cellb),
                Paragraph(status, cellb),
                Paragraph(conv, cellb),
            ])

        tbl = Table(rows, colWidths=[0.85*inch, 3.4*inch, 2.1*inch, 0.65*inch, 0.7*inch], repeatRows=1)
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#1f3a5f")),
            ("GRID", (0,0), (-1,-1), 0.25, colors.HexColor("#bfbfbf")),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("LEFTPADDING", (0,0), (-1,-1), 4),
            ("RIGHTPADDING", (0,0), (-1,-1), 4),
            ("TOPPADDING", (0,0), (-1,-1), 3),
            ("BOTTOMPADDING", (0,0), (-1,-1), 3),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#f5f7fa")]),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 6))

    footer = ParagraphStyle("F", parent=ss["Normal"], fontSize=8, leading=10,
                            spaceBefore=16, textColor=colors.HexColor("#888"))
    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "Generated from Candidates/candidates_index.json (schema 3.0). Deep-dive PDFs are in "
        "Candidates/deep_dives/pdf/. Entries marked <i>[backfill pending]</i> will get their "
        "hypothesis and key dates populated on the next reporting-hub-deep-dives run.",
        footer))

    doc.build(story)
    os.replace(OUT_PDF + ".tmp", OUT_PDF)

    # ------- MD -------
    lines = [f"# All Candidates — Master Summary\n",
             f"_{today} · {total} active candidates · {breakdown}_\n"]
    for tool in TOOL_ORDER:
        te = [e for e in entries if e["source_tool"] == tool]
        if not te: continue
        lines.append(f"\n## {TOOL_LABELS[tool]} — {len(te)} candidates\n")
        lines.append("| Ticker | Category | Hypothesis | Next key dates | Status | Conviction |")
        lines.append("|---|---|---|---|---|---|")
        for e in sorted(te, key=lambda x: x["ticker"]):
            kd = "; ".join(f"{(k.get('date') or 'TBD')}: {k.get('event','')}" for k in (e.get("next_key_dates") or []))
            hyp = (e.get("hypothesis") or "_[backfill pending]_").replace("|","\\|").replace("\n"," ")
            lines.append(f"| **{e['ticker']}** | {e.get('catalyst_category') or '—'} | {hyp} | {kd or '—'} | {e.get('status') or '—'} | {e.get('conviction') or '—'} |")
    _atomic_write(OUT_MD, "\n".join(lines))

    print(f"Wrote {OUT_PDF}")
    print(f"Wrote {OUT_MD}")
    print(f"Total entries: {total}")

if __name__ == "__main__":
    render()
