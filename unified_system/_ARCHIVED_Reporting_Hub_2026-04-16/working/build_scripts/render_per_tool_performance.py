"""Render per-tool daily performance PDFs (one per producer tool).

Writes only under Reporting Hub/Performance/per_tool/<tool>/.
Read-only against producer state. Atomic write via .tmp + os.replace.
"""
from __future__ import annotations
import os, sys, json, re, datetime, glob
from reportlab.lib.pagesizes import letter
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, PageBreak,
                                Table, TableStyle)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors

HUB   = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CONAN = os.path.abspath(os.path.join(HUB, ".."))

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

styles = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=16, spaceAfter=8,
                    textColor=colors.HexColor("#1e2a44"))
H2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=12, spaceAfter=4,
                    textColor=colors.HexColor("#2b5b84"))
BODY = ParagraphStyle("BODY", parent=styles["BodyText"], fontSize=9, leading=12)
SMALL = ParagraphStyle("SMALL", parent=styles["BodyText"], fontSize=8, leading=10,
                       textColor=colors.HexColor("#4a4a4a"))


def mtime_iso(p):
    try:
        return datetime.datetime.utcfromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M:%SZ")
    except Exception:
        return "—"


def lock_state(root):
    p = os.path.join(root, "SESSION_LOCK.md")
    if not os.path.exists(p):
        return "no-lock-file"
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            first = f.readline().strip()
        return first or "unknown"
    except Exception:
        return "read-error"


def probe(tool):
    root = TOOL_ROOTS[tool]
    info = {"tool": tool, "label": TOOL_LABELS[tool], "root": root,
            "availability": "missing", "candidates": [], "signals_total": 0,
            "signals_7d": 0, "progress_tail": "", "session_state_excerpt": "",
            "lock": "?", "latest_report": None, "time_sensitive": None}
    if not os.path.isdir(root):
        return info
    ss = os.path.join(root, "SESSION_STATE.md")
    if not os.path.exists(ss):
        info["availability"] = "pre-launch"
        return info
    info["availability"] = "operational"
    info["lock"] = lock_state(root)

    # SESSION_STATE excerpt (first ~40 lines)
    try:
        with open(ss, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        info["session_state_excerpt"] = "".join(lines[:60])
        info["session_state_mtime"] = mtime_iso(ss)
    except Exception:
        pass

    # candidates
    cand_dir = os.path.join(root, "candidates")
    if os.path.isdir(cand_dir):
        for entry in sorted(os.listdir(cand_dir)):
            fp = os.path.join(cand_dir, entry)
            if entry.endswith(".md") and os.path.isfile(fp):
                info["candidates"].append({
                    "name": entry, "size": os.path.getsize(fp),
                    "mtime": mtime_iso(fp),
                })

    # signals
    sig_dir = os.path.join(root, "signals")
    if os.path.isdir(sig_dir):
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=7)
        for entry in os.listdir(sig_dir):
            fp = os.path.join(sig_dir, entry)
            if os.path.isfile(fp):
                info["signals_total"] += 1
                try:
                    if datetime.datetime.utcfromtimestamp(os.path.getmtime(fp)) >= cutoff:
                        info["signals_7d"] += 1
                except Exception:
                    pass

    # progress log tail
    plog = os.path.join(root, "PROGRESS_LOG.md")
    if os.path.exists(plog):
        try:
            with open(plog, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
            info["progress_tail"] = "".join(all_lines[-80:])
            info["progress_mtime"] = mtime_iso(plog)
        except Exception:
            pass

    # latest report
    rep_dir = os.path.join(root, "reports")
    if os.path.isdir(rep_dir):
        files = [os.path.join(rep_dir, f) for f in os.listdir(rep_dir)
                 if os.path.isfile(os.path.join(rep_dir, f))]
        if files:
            latest = max(files, key=os.path.getmtime)
            info["latest_report"] = {"name": os.path.basename(latest), "mtime": mtime_iso(latest)}

    # time-sensitive
    ts = os.path.join(root, "TIME_SENSITIVE.md")
    if os.path.exists(ts):
        try:
            with open(ts, "r", encoding="utf-8", errors="replace") as f:
                info["time_sensitive"] = f.read()[:4000]
        except Exception:
            pass
    return info


def build_pdf(tool, info, out_path, run_date, session_id):
    tmp = out_path + ".tmp"
    doc = SimpleDocTemplate(tmp, pagesize=letter,
                            leftMargin=0.6*inch, rightMargin=0.6*inch,
                            topMargin=0.5*inch, bottomMargin=0.5*inch,
                            title=f"{TOOL_LABELS[tool]} Performance {run_date}")
    story = []
    story.append(Paragraph(f"{info['label']} — Daily Performance", H1))
    story.append(Paragraph(f"Run date: {run_date} &nbsp;·&nbsp; Hub session: {session_id} &nbsp;·&nbsp; Tool: <b>{tool}</b>",
                           SMALL))
    story.append(Spacer(1, 8))

    # Availability
    story.append(Paragraph("1. Health", H2))
    if info["availability"] == "missing":
        story.append(Paragraph("<b>Availability:</b> MISSING — producer folder not found at path configured in SOURCES.md.", BODY))
        _finalize(doc, story, tmp, out_path)
        return
    if info["availability"] == "pre-launch":
        story.append(Paragraph("<b>Availability:</b> pre-launch — producer folder exists but SESSION_STATE.md is missing (no operational state yet).", BODY))
        story.append(Paragraph("<i>This is a placeholder performance PDF. It will populate with real metrics once the producer runs its first operational cycle.</i>", SMALL))
        _finalize(doc, story, tmp, out_path)
        return

    rows = [
        ["Availability", info["availability"]],
        ["Producer lock", info["lock"]],
        ["SESSION_STATE mtime", info.get("session_state_mtime", "—")],
        ["PROGRESS_LOG mtime", info.get("progress_mtime", "—")],
        ["Latest report", (info["latest_report"] or {}).get("name", "(none)")],
        ["Latest report mtime", (info["latest_report"] or {}).get("mtime", "—")],
        ["Active candidates (.md)", str(len(info["candidates"]))],
        ["Signals — total in signals/", str(info["signals_total"])],
        ["Signals — mtime < 7d", str(info["signals_7d"])],
    ]
    t = Table(rows, colWidths=[2.2*inch, 4.6*inch])
    t.setStyle(TableStyle([
        ("FONT",(0,0),(-1,-1),"Helvetica",9),
        ("FONT",(0,0),(0,-1),"Helvetica-Bold",9),
        ("BACKGROUND",(0,0),(0,-1),colors.HexColor("#f1f3f8")),
        ("GRID",(0,0),(-1,-1),0.25,colors.HexColor("#c9cfdb")),
        ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("LEFTPADDING",(0,0),(-1,-1),4),
        ("RIGHTPADDING",(0,0),(-1,-1),4),
        ("TOPPADDING",(0,0),(-1,-1),3),
        ("BOTTOMPADDING",(0,0),(-1,-1),3),
    ]))
    story.append(t)
    story.append(Spacer(1, 10))

    # Active candidates table
    story.append(Paragraph("2. Active candidates (candidates/*.md)", H2))
    if not info["candidates"]:
        story.append(Paragraph("<i>No candidate .md files at top level of candidates/. Pipeline may be cold or candidates live in subfolders.</i>", BODY))
    else:
        chdr = [["File", "Size (B)", "Last modified (UTC)"]]
        for c in info["candidates"]:
            chdr.append([c["name"], f"{c['size']:,}", c["mtime"]])
        ct = Table(chdr, colWidths=[4.2*inch, 1.0*inch, 1.6*inch], repeatRows=1)
        ct.setStyle(TableStyle([
            ("FONT",(0,0),(-1,-1),"Helvetica",8),
            ("FONT",(0,0),(-1,0),"Helvetica-Bold",8),
            ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#1e2a44")),
            ("TEXTCOLOR",(0,0),(-1,0),colors.white),
            ("GRID",(0,0),(-1,-1),0.25,colors.HexColor("#c9cfdb")),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, colors.HexColor("#f7f8fb")]),
            ("LEFTPADDING",(0,0),(-1,-1),4),
            ("RIGHTPADDING",(0,0),(-1,-1),4),
        ]))
        story.append(ct)
    story.append(Spacer(1, 10))

    # Session state excerpt
    story.append(Paragraph("3. SESSION_STATE.md (head)", H2))
    excerpt = (info.get("session_state_excerpt") or "")[:4500]
    excerpt_html = (excerpt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    .replace("\n", "<br/>"))
    story.append(Paragraph(f"<font face='Courier' size=7>{excerpt_html}</font>", BODY))
    story.append(Spacer(1, 10))

    # Progress log tail
    story.append(Paragraph("4. PROGRESS_LOG.md (tail ~80 lines)", H2))
    tail = (info.get("progress_tail") or "")[:5000]
    tail_html = (tail.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                 .replace("\n", "<br/>"))
    story.append(Paragraph(f"<font face='Courier' size=7>{tail_html}</font>", BODY))
    story.append(Spacer(1, 10))

    # Time-sensitive (if any)
    if info.get("time_sensitive"):
        story.append(PageBreak())
        story.append(Paragraph("5. TIME_SENSITIVE.md", H2))
        ts = info["time_sensitive"][:4500]
        ts_html = (ts.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                   .replace("\n", "<br/>"))
        story.append(Paragraph(f"<font face='Courier' size=7>{ts_html}</font>", BODY))

    story.append(Spacer(1, 14))
    story.append(Paragraph("— end of per-tool performance report —", SMALL))

    _finalize(doc, story, tmp, out_path)


def _finalize(doc, story, tmp, out_path):
    doc.build(story)
    # atomic rename
    os.replace(tmp, out_path)


def main():
    run_date = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    session_id = os.environ.get("HUB_SESSION_ID", "reporting-hub-performance-" +
                                datetime.datetime.utcnow().strftime("%Y-%m-%dT%H%M"))
    results = {}
    for tool in TOOL_ROOTS:
        info = probe(tool)
        out_dir = os.path.join(HUB, "Performance", "per_tool", tool)
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"{run_date}_performance.pdf")
        try:
            build_pdf(tool, info, out_path, run_date, session_id)
            results[tool] = {"status": "ok", "path": out_path,
                             "availability": info["availability"]}
        except Exception as e:
            # write .error sidecar, continue
            err_path = out_path + ".error"
            with open(err_path, "w", encoding="utf-8") as ef:
                ef.write(f"render error for {tool}: {type(e).__name__}: {e}\n")
            results[tool] = {"status": "error", "error": str(e),
                             "availability": info.get("availability")}
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
