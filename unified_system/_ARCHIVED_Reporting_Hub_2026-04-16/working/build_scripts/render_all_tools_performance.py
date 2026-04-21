"""Render master ALL_TOOLS_PERFORMANCE.pdf by aggregating producer state.

This script is READ-ONLY against producer tools. It never writes outside
Reporting Hub/.

Writes:
  Reporting Hub/Performance/ALL_TOOLS_PERFORMANCE.pdf
  Reporting Hub/Performance/ALL_TOOLS_PERFORMANCE.md
"""
from __future__ import annotations
import os, json, datetime, re
from reportlab.lib.pagesizes import letter
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, PageBreak,
                                Table, TableStyle, KeepTogether)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_JUSTIFY

HUB   = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CONAN = os.path.abspath(os.path.join(HUB, ".."))
OUT_PDF = os.path.join(HUB, "Performance", "ALL_TOOLS_PERFORMANCE.pdf")
OUT_MD  = os.path.join(HUB, "Performance", "ALL_TOOLS_PERFORMANCE.md")
REG     = os.path.join(HUB, "Candidates", "candidates_index.json")

# Producer roots (read-only)
TOOL_ROOTS = {
    "investment_tool": os.path.join(CONAN, "Investment tool", "investment_discovery_system"),
    "non_us":          os.path.join(CONAN, "Investmet tool Beta", "non_us_discovery_system"),
    "litigation":      os.path.join(CONAN, "Investment tool Delta", "litigation_system"),
    "silence":         os.path.join(CONAN, "Investment tool Gamma", "project set up template",
                                     "silence_tool_bootstrap"),
}
TOOL_LABELS = {
    "investment_tool": "Investment Discovery (US)",
    "non_us":          "Non-US Discovery",
    "litigation":      "Litigation & Dockets",
    "silence":         "Silence (behavioral)",
}

def probe_tool(tool):
    """Read-only probe of producer state. Returns a dict of metrics."""
    root = TOOL_ROOTS[tool]
    info = {"tool": tool, "label": TOOL_LABELS[tool], "root": root}
    if not os.path.isdir(root):
        info["availability"] = "missing"
        return info

    # SESSION_STATE
    ss = os.path.join(root, "SESSION_STATE.md")
    if not os.path.isfile(ss):
        info["availability"] = "pre-launch"
        return info

    # SESSION_LOCK
    lock = os.path.join(root, "SESSION_LOCK.md")
    info["availability"] = "operational"
    info["lock_state"] = "UNLOCKED"
    if os.path.isfile(lock):
        try:
            with open(lock) as f:
                txt = f.read()
            info["lock_state"] = "LOCKED" if txt.strip().startswith("LOCKED") else "UNLOCKED"
        except Exception:
            pass

    # Count candidates/ files
    cand_dir = os.path.join(root, "candidates")
    if os.path.isdir(cand_dir):
        info["active_candidates"] = sum(1 for f in os.listdir(cand_dir) if f.endswith(".md"))
    else:
        info["active_candidates"] = 0

    # PROGRESS_LOG tail — try to read last session tag
    pl = os.path.join(root, "PROGRESS_LOG.md")
    if os.path.isfile(pl):
        try:
            with open(pl) as f:
                tail = f.read()[-4000:]
            m = re.search(r"##\s*(?:Session|session).*?(\d{4}-\d{2}-\d{2}[T\s][^\n]+)", tail)
            if m:
                info["last_session_marker"] = m.group(1)[:60]
        except Exception:
            pass

    return info

def render():
    ss_styles = getSampleStyleSheet()
    today = datetime.date.today().isoformat()

    tool_info = [probe_tool(t) for t in TOOL_LABELS.keys()]

    # Load registry for candidate counts per tool
    reg = json.load(open(REG))
    by_tool = {}
    for e in reg["theses"]:
        by_tool.setdefault(e["source_tool"], 0); by_tool[e["source_tool"]] += 1

    # ---- PDF ----
    os.makedirs(os.path.dirname(OUT_PDF), exist_ok=True)
    tmp = OUT_PDF + ".tmp"
    doc = SimpleDocTemplate(tmp, pagesize=letter,
        leftMargin=0.65*inch, rightMargin=0.65*inch,
        topMargin=0.7*inch, bottomMargin=0.65*inch,
        title="All Tools Performance — Master",
        author="Reporting Hub")

    title = ParagraphStyle("T", parent=ss_styles["Title"], fontSize=18, leading=22, spaceAfter=4)
    subt  = ParagraphStyle("S", parent=ss_styles["Normal"], fontSize=9.5,
                           textColor=colors.HexColor("#666"), spaceAfter=14)
    h2    = ParagraphStyle("H2", parent=ss_styles["Heading2"], fontSize=13,
                           spaceBefore=14, spaceAfter=4, textColor=colors.HexColor("#1f3a5f"))
    body  = ParagraphStyle("B", parent=ss_styles["Normal"], fontSize=9.5, leading=13)
    small = ParagraphStyle("SM", parent=ss_styles["Normal"], fontSize=8.5, leading=11,
                           textColor=colors.HexColor("#666"))

    story = []
    story.append(Paragraph("All Tools Performance &mdash; Master", title))
    story.append(Paragraph(f"{today} &middot; fleet status, funnel, and deep-dive registry health", subt))

    # Fleet status table
    story.append(Paragraph("Fleet status", h2))
    rows = [["Tool", "Availability", "Lock", "Active candidates (producer)", "Deep-dives in hub"]]
    for info in tool_info:
        rows.append([
            info["label"],
            info.get("availability","?"),
            info.get("lock_state","—"),
            str(info.get("active_candidates","—")),
            str(by_tool.get(info["tool"], 0)),
        ])
    tbl = Table(rows, colWidths=[2.2*inch, 1.1*inch, 0.9*inch, 1.9*inch, 1.3*inch], repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#1f3a5f")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,-1), 9),
        ("GRID", (0,0), (-1,-1), 0.25, colors.HexColor("#bfbfbf")),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#f5f7fa")]),
    ]))
    story.append(tbl)

    # Per-tool summary
    story.append(Paragraph("Per-tool summary", h2))
    for info in tool_info:
        block = []
        block.append(Paragraph(f"<b>{info['label']}</b>", body))
        lines = [f"Availability: {info.get('availability','?')}"]
        if info.get("availability") == "operational":
            lines.append(f"Lock: {info.get('lock_state','—')}")
            lines.append(f"Active candidates (producer): {info.get('active_candidates','—')}")
            lines.append(f"Deep-dives in hub: {by_tool.get(info['tool'], 0)}")
            if info.get("last_session_marker"):
                lines.append(f"Last session marker: {info['last_session_marker']}")
        lines.append(f"Detailed PDF: Performance/per_tool/{info['tool']}/{today}_performance.pdf")
        block.append(Paragraph(" &middot; ".join(lines), small))
        story.append(KeepTogether(block))
        story.append(Spacer(1, 6))

    story.append(Spacer(1, 12))
    story.append(Paragraph(
        "This master report aggregates read-only probes of each producer tool's state and the hub's "
        "candidate registry. Detailed per-tool performance PDFs are written to Performance/per_tool/ "
        "by the reporting-hub-performance task.",
        small))

    doc.build(story)
    os.replace(tmp, OUT_PDF)

    # ---- MD ----
    lines = [f"# All Tools Performance — Master\n", f"_{today}_\n",
             "\n## Fleet status\n",
             "| Tool | Availability | Lock | Active candidates | Deep-dives in hub |",
             "|---|---|---|---|---|"]
    for info in tool_info:
        lines.append(f"| {info['label']} | {info.get('availability','?')} | {info.get('lock_state','—')} | "
                     f"{info.get('active_candidates','—')} | {by_tool.get(info['tool'],0)} |")
    tmp_md = OUT_MD + ".tmp"
    with open(tmp_md, "w") as f:
        f.write("\n".join(lines))
    os.replace(tmp_md, OUT_MD)
    print(f"Wrote {OUT_PDF}")
    print(f"Wrote {OUT_MD}")

if __name__ == "__main__":
    render()
