"""
Report Generator — produces PDF deliverables for the unified system.

Three public functions per Plan Part 5:
  - generate_daily_digest(signals, candidates, convergences) -> PDF path
  - generate_candidate_dossier(candidate_md_path, scoring_profile) -> PDF path
  - generate_weekly_strategic(scanner_stats, pipeline_stats) -> PDF path

Per D-005: reportlab is the PDF engine (pure Python, clean install).
This module is READ-ONLY with respect to operational data — it reads
signals/signal_log.json, candidates/, working/*, but never writes to them.
It only writes into reports/.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
)

REPO = Path(__file__).parent.parent
REPORTS = REPO / "reports"
DAILY_DIR = REPORTS / "daily"
WEEKLY_DIR = REPORTS / "weekly"
DOSSIER_DIR = REPORTS / "dossiers" / "pdf"
for d in (DAILY_DIR, WEEKLY_DIR, DOSSIER_DIR):
    d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------
# Styles (shared)
# --------------------------------------------------------------------

def _styles() -> Dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    s: Dict[str, ParagraphStyle] = {}
    s["Title"] = ParagraphStyle(
        "UTitle", parent=base["Title"], fontSize=18, spaceAfter=10,
        textColor=colors.HexColor("#1a1a1a"),
    )
    s["H2"] = ParagraphStyle(
        "UH2", parent=base["Heading2"], fontSize=13, spaceBefore=12,
        spaceAfter=6, textColor=colors.HexColor("#0b3d91"),
    )
    s["H3"] = ParagraphStyle(
        "UH3", parent=base["Heading3"], fontSize=11, spaceBefore=8,
        spaceAfter=4, textColor=colors.HexColor("#333333"),
    )
    s["Body"] = ParagraphStyle(
        "UBody", parent=base["BodyText"], fontSize=9.5, leading=12,
    )
    s["Small"] = ParagraphStyle(
        "USmall", parent=base["BodyText"], fontSize=8, leading=10,
        textColor=colors.HexColor("#555555"),
    )
    s["Mono"] = ParagraphStyle(
        "UMono", parent=base["Code"], fontSize=8, leading=10,
    )
    return s


def _table_style(header_bg: str = "#0b3d91") -> TableStyle:
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(header_bg)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f7fb")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ])


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _signals_list() -> List[Dict]:
    data = _load_json(REPO / "signals" / "signal_log.json", [])
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("signals", [])
    return []


def _band_color(band: str) -> str:
    return {
        "immediate": "#b00020",
        "watchlist": "#b8860b",
        "archive": "#2e7d32",
        "discard": "#666666",
    }.get(band, "#333333")


def _escape(s: str) -> str:
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# --------------------------------------------------------------------
# Daily Digest
# --------------------------------------------------------------------

def generate_daily_digest(
    signals: Optional[List[Dict]] = None,
    candidates: Optional[List[Dict]] = None,
    convergences: Optional[Dict] = None,
) -> Path:
    """Produce a 1–2 page daily digest PDF."""
    today = _now_utc()
    stamp = today.strftime("%Y-%m-%d_%H%M")
    out = DAILY_DIR / f"{stamp}_digest.pdf"

    # Default-load data if not supplied
    if signals is None:
        signals = _signals_list()
    if convergences is None:
        convergences = _load_json(
            REPO / "working" / f"convergence_report_{today.date().isoformat()}.json",
            {"groups": []},
        )
    if candidates is None:
        candidates = _collect_candidate_summaries()

    st = _styles()
    story: List = []

    story.append(Paragraph(f"Unified Daily Digest — {today.strftime('%Y-%m-%d %H:%M UTC')}", st["Title"]))
    story.append(Paragraph(
        f"Signals in log: {len(signals)} · Convergence groups: {len(convergences.get('groups', []))} · "
        f"Active candidates: {len(candidates)}", st["Small"],
    ))
    story.append(Spacer(1, 6))

    # Section 1 — New / high-band signals
    story.append(Paragraph("New &amp; High-Band Signals (last 24h)", st["H2"]))
    recent = _filter_recent(signals, hours=24)
    top = sorted(
        recent,
        key=lambda s: (s.get("scoring", {}) or {}).get("score_with_bonus",
                                                        (s.get("scoring", {}) or {}).get("score", 0)),
        reverse=True,
    )[:12]
    if top:
        rows = [["Ticker", "Profile", "Dir", "Score", "Band", "Headline"]]
        for s in top:
            sc = s.get("scoring", {}) or {}
            score = sc.get("score_with_bonus", sc.get("score", 0))
            band = sc.get("band_with_bonus", sc.get("band", "—"))
            rows.append([
                _escape(s.get("ticker", "?")),
                _escape((s.get("scoring_profile") or "—")[:16]),
                _escape(s.get("thesis_direction", "—")),
                f"{score:.1f}" if isinstance(score, (int, float)) else "—",
                _escape(band),
                _truncate(_escape(s.get("headline") or s.get("summary") or ""), 70),
            ])
        t = Table(rows, colWidths=[0.7*inch, 1.15*inch, 0.35*inch, 0.55*inch, 0.75*inch, 3.2*inch])
        t.setStyle(_table_style())
        story.append(t)
    else:
        story.append(Paragraph("No new signals in the last 24 hours.", st["Body"]))

    story.append(Spacer(1, 8))

    # Section 2 — Convergence alerts
    story.append(Paragraph("Convergence Alerts", st["H2"]))
    conv_groups = convergences.get("groups", [])
    live_conv = [g for g in conv_groups if g.get("bonus", 0) > 0 or g.get("convergence_type") == "contradiction"]
    if live_conv:
        rows = [["Issuer / Tickers", "# Sigs", "Type", "Bonus", "Scanners", "Profiles"]]
        for g in live_conv[:10]:
            rows.append([
                _escape((", ".join(g.get("tickers_seen") or []))[:24] or g.get("issuer_key", "?")[:24]),
                str(g.get("signal_count", 0)),
                _escape(g.get("convergence_type", "—")),
                f"+{g.get('bonus', 0)}",
                _escape(", ".join(g.get("scanners") or [])[:40]),
                _escape(", ".join(g.get("profiles") or [])[:30]),
            ])
        t = Table(rows, colWidths=[1.6*inch, 0.5*inch, 1.0*inch, 0.5*inch, 1.8*inch, 1.3*inch])
        t.setStyle(_table_style("#8b0000"))
        story.append(t)
    else:
        story.append(Paragraph("No active convergences.", st["Body"]))

    story.append(Spacer(1, 8))

    # Section 3 — Candidate status
    story.append(Paragraph("Candidate Status Snapshot", st["H2"]))
    if candidates:
        rows = [["Ticker", "Profile", "Band", "Stage", "Last Update"]]
        for c in candidates[:12]:
            rows.append([
                _escape(c.get("ticker", "?")),
                _escape((c.get("profile") or "—")[:16]),
                _escape(c.get("band") or "—"),
                _escape(c.get("stage") or "—"),
                _escape(c.get("last_update") or "—"),
            ])
        t = Table(rows, colWidths=[0.8*inch, 1.4*inch, 0.8*inch, 1.2*inch, 1.4*inch])
        t.setStyle(_table_style("#2e7d32"))
        story.append(t)
    else:
        story.append(Paragraph("No active candidates.", st["Body"]))

    story.append(Spacer(1, 8))

    # Section 4 — Local system health
    story.append(Paragraph("System Health (Local Registry Only)", st["H2"]))
    reg = _load_json(REPO / "config" / "scanner_registry.json", {"scanners": []})
    scanners = reg.get("scanners", [])
    ok = sum(1 for s in scanners if s.get("last_run_status") == "ok")
    err = sum(1 for s in scanners if s.get("last_run_status") in ("error", "timeout"))
    non_op = sum(1 for s in scanners if s.get("status") != "operational")
    story.append(Paragraph(
        "This section reflects unified_system/config/scanner_registry.json and local generated files. "
        "It is not the live Supabase fleet dashboard.",
        st["Body"],
    ))
    story.append(Paragraph(
        f"Scanners total: {len(scanners)} · OK last run: {ok} · Errors/timeouts: {err} · Non-operational: {non_op}",
        st["Body"],
    ))

    SimpleDocTemplate(
        str(out), pagesize=LETTER,
        leftMargin=0.55*inch, rightMargin=0.55*inch,
        topMargin=0.55*inch, bottomMargin=0.55*inch,
        title=f"Unified Daily Digest {stamp}", author="Unified System",
    ).build(story)
    return out


# --------------------------------------------------------------------
# Candidate Dossier
# --------------------------------------------------------------------

def generate_candidate_dossier(candidate_md_path: str, scoring_profile: Optional[str] = None) -> Path:
    """Render a candidate markdown file as a styled PDF dossier."""
    md_path = Path(candidate_md_path)
    if not md_path.exists():
        raise FileNotFoundError(candidate_md_path)

    text = md_path.read_text(encoding="utf-8", errors="replace")
    ticker = _extract_ticker(md_path.stem, text)
    mic = _extract_mic(text)
    stamp = _now_utc().strftime("%Y-%m-%d")
    suffix = f"_{mic}" if mic else ""
    out = DOSSIER_DIR / f"{ticker}{suffix}_{stamp}.pdf"

    st = _styles()
    story: List = []

    title = _extract_title(text) or f"Dossier — {ticker}"
    story.append(Paragraph(_escape(title), st["Title"]))
    story.append(Paragraph(
        f"Source: {md_path.name} · Profile: {_escape(scoring_profile or 'unspecified')} · Rendered: {stamp}",
        st["Small"],
    ))
    story.append(Spacer(1, 6))

    # Render the markdown with a lightweight parser (headings, bullets, paragraphs, fences).
    for block in _md_blocks(text):
        kind, content = block
        if kind == "h1":
            story.append(Paragraph(_escape(content), st["Title"]))
        elif kind == "h2":
            story.append(Paragraph(_escape(content), st["H2"]))
        elif kind == "h3":
            story.append(Paragraph(_escape(content), st["H3"]))
        elif kind == "code":
            story.append(Paragraph(f"<font face='Courier'>{_escape(content)}</font>", st["Mono"]))
        elif kind == "bullet":
            story.append(Paragraph(f"• {_inline(content)}", st["Body"]))
        elif kind == "p":
            story.append(Paragraph(_inline(content), st["Body"]))
        story.append(Spacer(1, 2))

    # Append scoring breakdown if profile is provided and rubric is known
    if scoring_profile:
        story.append(Spacer(1, 8))
        story.append(Paragraph(f"Scoring Rubric — {_escape(scoring_profile)}", st["H2"]))
        rubric_rows = _rubric_rows(scoring_profile)
        if rubric_rows:
            t = Table(rubric_rows, colWidths=[2.4*inch, 0.9*inch, 3.0*inch])
            t.setStyle(_table_style())
            story.append(t)
        else:
            story.append(Paragraph("Rubric not available for this profile.", st["Body"]))

    SimpleDocTemplate(
        str(out), pagesize=LETTER,
        leftMargin=0.6*inch, rightMargin=0.6*inch,
        topMargin=0.6*inch, bottomMargin=0.6*inch,
        title=f"Dossier {ticker}", author="Unified System",
    ).build(story)
    return out


# --------------------------------------------------------------------
# Weekly Strategic
# --------------------------------------------------------------------

def generate_weekly_strategic(
    scanner_stats: Optional[Dict] = None,
    pipeline_stats: Optional[Dict] = None,
) -> Path:
    """Weekly strategic report (Sundays). Renders even if called mid-week,
    but the scheduler only fires it weekly."""
    now = _now_utc()
    year, week, _ = now.isocalendar()
    out = WEEKLY_DIR / f"{year}-W{week:02d}_strategic.pdf"

    if scanner_stats is None:
        scanner_stats = _compute_scanner_stats()
    if pipeline_stats is None:
        pipeline_stats = _compute_pipeline_stats()

    st = _styles()
    story: List = []

    story.append(Paragraph(f"Unified Weekly Strategic — {year}-W{week:02d}", st["Title"]))
    story.append(Paragraph(f"Generated {now.strftime('%Y-%m-%d %H:%M UTC')}", st["Small"]))
    story.append(Spacer(1, 8))

    # Scanner health
    story.append(Paragraph("Scanner Health (7d)", st["H2"]))
    rows = [["Scanner", "Status", "Last Run", "Last Signals", "Cadence"]]
    for s in scanner_stats.get("scanners", []):
        rows.append([
            _escape(s.get("name", "?")),
            _escape(s.get("status", "—")),
            _escape(s.get("last_run_utc", "—")),
            str(s.get("last_run_signals", 0)),
            _escape(s.get("cadence", "—")),
        ])
    t = Table(rows, colWidths=[1.7*inch, 1.0*inch, 1.9*inch, 0.9*inch, 0.8*inch])
    t.setStyle(_table_style())
    story.append(t)
    story.append(Spacer(1, 10))

    # Pipeline stats
    story.append(Paragraph("Pipeline Metrics", st["H2"]))
    rows = [["Metric", "Value"]]
    for k, v in pipeline_stats.get("summary", {}).items():
        rows.append([_escape(k), _escape(str(v))])
    t = Table(rows, colWidths=[3.0*inch, 3.0*inch])
    t.setStyle(_table_style("#2e7d32"))
    story.append(t)
    story.append(Spacer(1, 10))

    # Hit rate by profile
    story.append(Paragraph("Hit Rate by Profile", st["H2"]))
    rows = [["Profile", "Signals", "Avg Score", "Watchlist+", "Immediate"]]
    for p, d in pipeline_stats.get("by_profile", {}).items():
        rows.append([
            _escape(p),
            str(d.get("count", 0)),
            f"{d.get('avg_score', 0):.2f}",
            str(d.get("watchlist_plus", 0)),
            str(d.get("immediate", 0)),
        ])
    if len(rows) > 1:
        t = Table(rows, colWidths=[1.8*inch, 0.9*inch, 0.9*inch, 0.9*inch, 0.9*inch])
        t.setStyle(_table_style())
        story.append(t)
    else:
        story.append(Paragraph("No profile data yet.", st["Body"]))

    story.append(Spacer(1, 10))

    # Coverage gaps & recommendations
    story.append(Paragraph("Coverage Gaps", st["H2"]))
    gaps = pipeline_stats.get("coverage_gaps", [])
    if gaps:
        for g in gaps:
            story.append(Paragraph(f"• {_escape(g)}", st["Body"]))
    else:
        story.append(Paragraph("No gaps flagged.", st["Body"]))

    story.append(Spacer(1, 6))
    story.append(Paragraph("Recommendations", st["H2"]))
    recs = pipeline_stats.get("recommendations", [])
    if recs:
        for r in recs:
            story.append(Paragraph(f"• {_escape(r)}", st["Body"]))
    else:
        story.append(Paragraph("No recommendations.", st["Body"]))

    SimpleDocTemplate(
        str(out), pagesize=LETTER,
        leftMargin=0.6*inch, rightMargin=0.6*inch,
        topMargin=0.6*inch, bottomMargin=0.6*inch,
        title=f"Weekly Strategic {year}-W{week:02d}", author="Unified System",
    ).build(story)
    return out


# --------------------------------------------------------------------
# Support routines
# --------------------------------------------------------------------

def _filter_recent(signals: List[Dict], hours: int) -> List[Dict]:
    cutoff = _now_utc() - timedelta(hours=hours)
    out = []
    for s in signals:
        ds = s.get("scan_date") or s.get("source_date")
        if not ds:
            continue
        try:
            dt = datetime.fromisoformat(ds.replace("Z", "+00:00"))
        except Exception:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt >= cutoff:
            out.append(s)
    return out


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _collect_candidate_summaries() -> List[Dict]:
    """Read candidates/ for minimal summary info."""
    cand_root = REPO / "candidates"
    out: List[Dict] = []
    for sub in ("delivered", "watchlist"):
        d = cand_root / sub
        if not d.exists():
            continue
        for p in sorted(d.iterdir()):
            if p.is_file() and p.suffix in (".md", ".json"):
                out.append({
                    "ticker": _extract_ticker(p.stem, ""),
                    "profile": _guess_profile_from_path(p),
                    "band": sub,
                    "stage": sub,
                    "last_update": datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d"),
                })
    return out


def _guess_profile_from_path(p: Path) -> str:
    name = p.name.lower()
    if "merger" in name or "arb" in name:
        return "merger_arb"
    if "fda" in name or "pdufa" in name:
        return "binary_catalyst"
    if "short" in name:
        return "short_positioning"
    if "litig" in name or "court" in name:
        return "litigation"
    return "activist_governance"


def _extract_ticker(stem: str, text: str) -> str:
    _PLACEHOLDER_TICKERS = {"DRAFT", "TODO", "TBD", "TBA", "XXX", "PLACEHOLDER", "CURATOR"}

    if text:
        m0 = re.search(r"\*\*Ticker\*\*:\s*([A-Za-z0-9]{1,8})\b", text[:2000])
        if m0:
            cand = m0.group(1).upper()
            if cand not in _PLACEHOLDER_TICKERS:
                return cand

    # Prefer filename convention "<TICKER>_<MIC>_..." where first token before the
    # first underscore is the local ticker (may be digits for XTKS/XASX).
    parts = stem.split("_")
    if len(parts) >= 2 and re.fullmatch(r"X[A-Z]{3}", parts[1] or ""):
        head = parts[0]
        if head and re.fullmatch(r"[A-Za-z0-9]{1,8}", head):
            cand = head.upper()
            if cand not in _PLACEHOLDER_TICKERS:
                return cand
    if parts and parts[0] and re.fullmatch(r"[A-Za-z0-9]{1,8}", parts[0]):
        head = parts[0]
        if head.isdigit() or any(c.isalpha() for c in head):
            cand = head.upper()
            if cand not in _PLACEHOLDER_TICKERS:
                return cand
    for m in re.finditer(r"\b([A-Z]{1,6})\b", stem):
        cand = m.group(1)
        if cand not in _PLACEHOLDER_TICKERS:
            return cand
    if text:
        for m2 in re.finditer(r"\b([A-Z]{2,6})\b", text[:500]):
            cand = m2.group(1)
            if cand not in _PLACEHOLDER_TICKERS:
                return cand
    return stem[:8].upper()


def _extract_mic(text: str) -> Optional[str]:
    m = re.search(r"\b(X[A-Z]{3})\b", text[:1000])
    return m.group(1) if m else None


def _extract_title(text: str) -> Optional[str]:
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            t = s[2:].strip()
            # Strip "Candidate:" or "Candidate —" prefix
            t = re.sub(r"^(?:Candidate\s*[:—-]\s*)", "", t, flags=re.IGNORECASE)
            # Strip trailing parenthetical like "(Session 26)"
            t = re.sub(r"\s*\([^)]*Session[^)]*\)\s*$", "", t, flags=re.IGNORECASE)
            return t.strip()
    return None


# Lightweight markdown → block parser.
def _md_blocks(text: str):
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i].rstrip()
        if ln.startswith("```"):
            buf = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            yield ("code", "\n".join(buf))
            i += 1
            continue
        if ln.startswith("### "):
            yield ("h3", ln[4:].strip())
        elif ln.startswith("## "):
            yield ("h2", ln[3:].strip())
        elif ln.startswith("# "):
            yield ("h1", ln[2:].strip())
        elif ln.startswith(("- ", "* ")):
            yield ("bullet", ln[2:].strip())
        elif ln.strip():
            yield ("p", ln.strip())
        i += 1


def _inline(s: str) -> str:
    # Very small inline-markdown transform: **bold** and *italic* and `code`.
    s = _escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<!\*)\*(?!\*)(.+?)\*(?!\*)", r"<i>\1</i>", s)
    s = re.sub(r"`([^`]+)`", r"<font face='Courier'>\1</font>", s)
    return s


def _rubric_rows(profile: str) -> List[List[str]]:
    # Mirrors WEIGHTS in run_post_scan.py but kept independently so this
    # module doesn't couple to the scoring module at import time.
    tables = {
        "merger_arb": [
            ("Spread Size", "3.0", "Closing spread vs. expected terms."),
            ("Deal Certainty", "2.5", "Approval path, regulatory load, financing."),
            ("Annualized Return", "2.0", "Return annualized on expected close."),
            ("Break Risk", "1.5", "Probability and severity of deal failure."),
            ("Liquidity", "1.0", "ADV vs. intended position size."),
        ],
        "activist_governance": [
            ("Signal Strength", "2.0", "Quality and specificity of the filing or disclosure."),
            ("Information Asymmetry", "2.0", "How widely the signal is known to market."),
            ("Activist Track Record", "1.5", "Historical outcome rate for this party."),
            ("Risk Reward", "1.5", "Payoff skew under realistic outcomes."),
            ("Catalyst Clarity", "1.0", "Specific forcing events or deadlines."),
            ("Edge Decay", "1.0", "How quickly the edge compresses post-publication."),
            ("Liquidity", "1.0", "ADV vs. intended position size."),
        ],
        "binary_catalyst": [
            ("Approval Probability", "2.5", "Odds of positive outcome."),
            ("Market Mispricing", "2.5", "Implied probability vs. fair probability."),
            ("Magnitude", "1.5", "Upside% and downside% scale."),
            ("Competitive Landscape", "1.5", "Competitors in/near same indication."),
            ("Catalyst Timeline", "1.0", "Days to decision."),
            ("Liquidity", "1.0", "ADV vs. intended position size."),
        ],
        "short_positioning": [
            ("Crowding Intensity", "2.5", "Short interest and % of float."),
            ("Trend Direction", "2.0", "Rising / stable / falling shorts; historical tracking required."),
            ("Catalyst Proximity", "2.0", "Nearest forcing event."),
            ("Size vs Float", "1.5", "Total shares short relative to free float."),
            ("Historical Analog", "1.0", "Prior episodes with similar setup."),
            ("Liquidity", "1.0", "ADV vs. intended position size."),
        ],
        "litigation": [
            ("Financial Materiality", "3.0", "Quantum vs. market cap."),
            ("Legal Outcome Probability", "2.0", "Odds of judgment or settlement in direction."),
            ("Market Pricing", "2.0", "Reaction already baked in."),
            ("Resolution Timeline", "1.5", "Days to ruling / next status."),
            ("Liquidity", "1.0", "ADV vs. intended position size."),
            ("Party Resolution Confidence", "0.5", "Certainty that named defendant = issuer."),
        ],
        "takeover_candidate": [
            ("Setup Strength", "3.0", "Setup patterns hit; extra weight for explicit strategic-review language."),
            ("Edge Freshness", "2.0", "Recency of the key triggering signal."),
            ("Valuation Cushion", "2.0", "Discount to historical median EV/EBITDA or EV/Revenue vs. comparables."),
            ("Strategic Buyer Clarity", "2.0", "Named strategic acquirer with sector M&A history vs. generic PE."),
            ("Liquidity", "1.0", "30-day ADV, spread, borrow availability."),
        ],
    }
    entries = tables.get(profile)
    if not entries:
        return []
    rows = [["Dimension", "Weight", "Definition"]]
    for name, w, desc in entries:
        rows.append([name, w, desc])
    return rows


def _compute_scanner_stats() -> Dict:
    reg = _load_json(REPO / "config" / "scanner_registry.json", {"scanners": []})
    out = []
    for s in reg.get("scanners", []):
        out.append({
            "name": s.get("name"),
            "status": s.get("status"),
            "last_run_utc": s.get("last_run_utc") or "—",
            "last_run_signals": s.get("last_run_signals", 0),
            "cadence": s.get("cadence"),
        })
    return {"scanners": out}


def _compute_pipeline_stats() -> Dict:
    signals = _signals_list()
    summary = {
        "total_signals_in_log": len(signals),
        "signals_last_7d": len(_filter_recent(signals, hours=24 * 7)),
        "signals_last_24h": len(_filter_recent(signals, hours=24)),
    }
    by_profile: Dict[str, Dict[str, Any]] = {}
    for s in signals:
        prof = s.get("scoring_profile") or "activist_governance"
        d = by_profile.setdefault(prof, {"count": 0, "score_sum": 0.0, "watchlist_plus": 0, "immediate": 0})
        d["count"] += 1
        sc = s.get("scoring", {}) or {}
        score = sc.get("score_with_bonus", sc.get("score", 0)) or 0
        d["score_sum"] += float(score)
        band = sc.get("band_with_bonus", sc.get("band", ""))
        if band in ("watchlist", "immediate"):
            d["watchlist_plus"] += 1
        if band == "immediate":
            d["immediate"] += 1
    for d in by_profile.values():
        d["avg_score"] = round(d["score_sum"] / d["count"], 2) if d["count"] else 0.0
        d.pop("score_sum", None)

    coverage_gaps: List[str] = []
    # Simple gap heuristic — planned scanners still not operational.
    reg = _load_json(REPO / "config" / "scanner_registry.json", {"scanners": []})
    for s in reg.get("scanners", []):
        if s.get("status") == "planned":
            coverage_gaps.append(f"{s.get('name')} not yet operational ({s.get('reason', 'planned')})")
        elif s.get("status") == "blocked":
            coverage_gaps.append(f"{s.get('name')} blocked — {s.get('reason', 'see OPEN_QUESTIONS')}")

    recommendations: List[str] = []
    if summary["signals_last_7d"] == 0:
        recommendations.append("Signal log empty over last 7 days — verify scheduled tasks are running.")
    if by_profile.get("merger_arb", {}).get("count", 0) == 0:
        recommendations.append("No merger_arb signals — verify edgar_filing_monitor is picking up M&A 8-Ks.")

    return {
        "summary": summary,
        "by_profile": by_profile,
        "coverage_gaps": coverage_gaps,
        "recommendations": recommendations,
    }


# --------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily", action="store_true")
    ap.add_argument("--weekly", action="store_true")
    ap.add_argument("--dossier", help="Path to candidate .md")
    ap.add_argument("--profile", help="Scoring profile for dossier")
    ap.add_argument("--candidates-summary", action="store_true",
                    help="Generate an all-candidates summary PDF into reports/candidates/")
    ap.add_argument("--include-archive", action="store_true",
                    help="Include archived candidates in the summary")
    args = ap.parse_args()

    if args.daily:
        p = generate_daily_digest()
        print(str(p))
    if args.weekly:
        p = generate_weekly_strategic()
        print(str(p))
    if args.dossier:
        p = generate_candidate_dossier(args.dossier, args.profile)
        print(str(p))
    if args.candidates_summary:
        p = generate_candidates_summary(include_archive=args.include_archive)
        print(str(p))


# --------------------------------------------------------------------
# Candidates Summary — all-candidates digest PDF (added 2026-04-17)
# --------------------------------------------------------------------

CANDIDATES_DIR = REPORTS / "candidates"
CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)


def _candidate_band_from_stage(stage: str) -> str:
    return {
        "delivered": "delivered",
        "watchlist": "watchlist",
        "immediate": "immediate",
        "archive": "archive",
        "active": "active",
    }.get(stage, stage)


def _stage_sort_key(stage: str) -> int:
    return {"immediate": 0, "delivered": 1, "active": 2, "watchlist": 3, "archive": 4}.get(stage, 9)


def _strip_yaml_frontmatter(text: str) -> tuple:
    """If text starts with a YAML frontmatter block, return (meta_dict, body)."""
    if not text.startswith("---"):
        return {}, text
    # Find the closing ---
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    yaml_block = text[3:end].strip("\n")
    body = text[end + 4:].lstrip("\n")
    meta: Dict[str, str] = {}
    for ln in yaml_block.splitlines():
        if ":" in ln and not ln.lstrip().startswith("#"):
            k, _, v = ln.partition(":")
            meta[k.strip()] = v.strip().strip('"\'')
    return meta, body


def _parse_md_candidate(path: Path) -> Dict:
    """Extract minimal summary from a markdown candidate file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}
    yaml_meta, body = _strip_yaml_frontmatter(text)
    title = _extract_title(body) or _extract_title(text) or path.stem.replace("_", " ")
    ticker = _extract_ticker(path.stem, body or text)
    mic = (yaml_meta.get("mic") or _extract_mic(body) or _extract_mic(text) or "")

    # Score — prefer explicit Score line; fall back to YAML score_total or score
    score = None
    m_score = re.search(r"Score\*{0,2}\s*[:：]?\s*\*{0,2}\s*([\d.]+)", body or text)
    if m_score:
        try:
            score = float(m_score.group(1))
        except Exception:
            score = None
    if score is None:
        for k in ("score_total", "score"):
            if k in yaml_meta:
                try:
                    score = float(yaml_meta[k])
                    break
                except Exception:
                    pass

    # Status
    status = ""
    m_status = re.search(r"Status\*{0,2}\s*[:：]?\s*\*{0,2}\s*([^\n\r\\*]+)", body or text)
    if m_status:
        status = m_status.group(1).strip()[:80]
    elif yaml_meta.get("status"):
        status = yaml_meta["status"][:80]

    # Strategy / profile
    strategy = _guess_profile_from_path(path)
    m_strat = re.search(r"(?:Strategy|Source Strategy)\*{0,2}\s*[:：]?\s*\*{0,2}\s*([^\n\r\\*]+)", body or text)
    if m_strat:
        strategy_txt = m_strat.group(1).strip()[:60]
    elif yaml_meta.get("signal_type"):
        strategy_txt = yaml_meta["signal_type"][:60]
    else:
        strategy_txt = strategy

    # Thesis — prefer named sections, but ALWAYS search within body (not frontmatter)
    thesis = ""
    search_src = body or text
    # Try named sections first (allow optional "1." / "1)" numbering prefix)
    for hdr_pat in [
        r"##\s*(?:\d+[\.\)]\s*)?TL;DR\b[^\n]*\n+(.+?)(?=\n##|\n---|\Z)",
        r"##\s*(?:\d+[\.\)]\s*)?One[-\s]?line thesis\b[^\n]*\n+(.+?)(?=\n##|\n---|\Z)",
        r"##\s*(?:\d+[\.\)]\s*)?Thesis(?:\s+Statement)?\b[^\n]*\n+(.+?)(?=\n##|\n---|\Z)",
        r"##\s*(?:\d+[\.\)]\s*)?Situation\s+summary\b[^\n]*\n+(.+?)(?=\n##|\n---|\Z)",
        r"##\s*(?:\d+[\.\)]\s*)?Company\s+Overview\b[^\n]*\n+(.+?)(?=\n##|\n---|\Z)",
        r"##\s*(?:\d+[\.\)]\s*)?Summary\b[^\n]*\n+(.+?)(?=\n##|\n---|\Z)",
    ]:
        m_th = re.search(hdr_pat, search_src, re.DOTALL | re.IGNORECASE)
        if m_th:
            candidate = m_th.group(1).strip()
            # Skip if it opens with a table/quote/heading — prefer first real prose
            # Allow bold-prefixed paragraphs like "**Label:** content..." since that
            # is the common thesis style in this repo.
            collected: List[str] = []
            for ln in candidate.splitlines():
                s = ln.strip()
                if not s:
                    if collected:
                        break
                    continue
                if s.startswith(("#", ">", "---", "|")):
                    continue
                collected.append(s)
                if sum(len(x) for x in collected) > 250:
                    break
            if collected:
                thesis = " ".join(collected)
                break
    if not thesis:
        # First meaningful paragraph — skip headers, quotes, tables, YAML-like lines (key: value)
        lines = search_src.splitlines()
        for ln in lines:
            s = ln.strip()
            if not s:
                continue
            if s.startswith(("#", "**", ">", "---", "|", "-", "*")):
                continue
            # Skip "key: value" style YAML-like lines (no spaces before colon or short word before colon)
            if re.match(r"^[a-z_][a-z0-9_]{0,30}\s*:\s*", s):
                continue
            thesis = s
            break
    # Strip markdown emphasis and inline links, collapse whitespace
    thesis = re.sub(r"\*\*", "", thesis)
    thesis = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", thesis)
    thesis = re.sub(r"\s+", " ", thesis)[:600]

    # Source strategy date / date identified
    date_id = ""
    m_date = re.search(r"(?:Date Identified|Date identified|Last Updated|Created)\*{0,2}\s*[:：]?\s*\*{0,2}\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", text)
    if m_date:
        date_id = m_date.group(1)

    # Key dates — look for "Catalyst" or "PDUFA" or AM dates
    key_dates: List[str] = []
    for pat in [
        r"PDUFA[^0-9\n]{0,40}([A-Za-z]+ \d{1,2},? \d{4}|\d{4}-\d{2}-\d{2})",
        r"Annual Meeting[^0-9\n]{0,60}([A-Za-z]+ \d{1,2},? \d{4}|\d{4}-\d{2}-\d{2})",
        r"(?:Catalyst|Trigger|Close date|Expected close|Settlement)[^0-9\n]{0,40}([A-Za-z]+ \d{1,2},? \d{4}|\d{4}-\d{2}-\d{2})",
        r"Voting deadline[^0-9\n]{0,40}([A-Za-z]+ \d{1,2},? \d{4}|\d{4}-\d{2}-\d{2})",
        r"Record date[^0-9\n]{0,40}([A-Za-z]+ \d{1,2},? \d{4}|\d{4}-\d{2}-\d{2})",
    ]:
        for m in re.finditer(pat, text, re.IGNORECASE):
            d = m.group(1).strip()
            if d and d not in key_dates:
                key_dates.append(d)
            if len(key_dates) >= 4:
                break
        if len(key_dates) >= 4:
            break

    # Source URLs — grab first two
    urls = re.findall(r"https?://[^\s)>\]]+", text)
    urls = [u.rstrip(".,);") for u in urls[:3]]

    return {
        "path": str(path),
        "ticker": ticker,
        "mic": mic,
        "title": title,
        "score": score,
        "status": status,
        "strategy": strategy_txt,
        "profile": strategy,
        "thesis": thesis,
        "date_identified": date_id,
        "key_dates": key_dates,
        "urls": urls,
        "last_update": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d"),
    }


def _parse_json_candidate(path: Path) -> Dict:
    """Extract summary from a JSON watchlist-stub candidate."""
    try:
        d = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    ticker = d.get("ticker") or ""
    if not ticker and d.get("ticker_resolution_pending"):
        ticker = f"[{d.get('codigo_cvm') or d.get('id_empresa_biva') or d.get('stock_code') or 'PENDING'}]"
    mic = d.get("mic") or ""
    title = d.get("company_name_en") or d.get("primary_headline") or path.stem
    score = d.get("score_with_convergence_bonus") or d.get("max_raw_score")
    profile = d.get("primary_scoring_profile") or "activist_governance"
    strategy = d.get("primary_signal_type") or profile
    thesis = d.get("primary_headline") or ""
    if d.get("notes"):
        thesis = (thesis + " — " + d["notes"])[:600]
    date_id = (d.get("scan_date") or "")[:10]
    key_dates: List[str] = []
    if d.get("primary_source_date"):
        key_dates.append(d["primary_source_date"][:10])
    conv = d.get("convergence") or {}
    conv_note = ""
    if conv:
        ctype = conv.get("type") or ""
        bonus = conv.get("bonus") or 0
        n = conv.get("n_signals") or 0
        conv_note = f"{ctype} convergence, +{bonus} bonus, n={n}"
    urls = []
    if d.get("primary_source_url"):
        urls.append(d["primary_source_url"])
    return {
        "path": str(path),
        "ticker": ticker,
        "mic": mic,
        "title": title,
        "score": float(score) if isinstance(score, (int, float)) else None,
        "status": d.get("status") or d.get("routing") or "watchlist",
        "strategy": strategy,
        "profile": profile,
        "thesis": thesis,
        "convergence_note": conv_note,
        "date_identified": date_id,
        "key_dates": key_dates,
        "urls": urls,
        "last_update": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d"),
    }


def _load_post_edge_archive() -> set:
    """Return the set of tickers that have been explicitly archived in _curated_rationales.json
    under the `_archived` block. Post-edge gate: these are candidates whose catalyst has already
    publicly resolved (M&A signed, PDUFA approved, activist offer published). The system does not
    surface them — they have no information edge left.
    """
    try:
        import json as _json
        rat_path = REPO / "candidates" / "_curated_rationales.json"
        if not rat_path.exists():
            return set()
        with open(rat_path) as f:
            d = _json.load(f)
        archived = d.get("_archived", {})
        return {k for k in archived.keys() if isinstance(k, str) and not k.startswith("_")}
    except Exception:
        return set()


def _load_draft_tickers() -> set:
    """Return the set of tickers whose curated rationale is still a draft."""
    drafts: set = set()
    try:
        rat_path = REPO / "candidates" / "_curated_rationales.json"
        if not rat_path.exists():
            return drafts
        with open(rat_path, "r", encoding="utf-8") as f:
            rats = json.load(f) or {}
        for tk, rat in rats.items():
            if tk == "_archived" or not isinstance(rat, dict):
                continue
            is_draft = bool(rat.get("_draft"))
            if not is_draft:
                one = str(rat.get("one_liner") or "")
                hyp = str(rat.get("hypothesis") or "")
                if "[DRAFT" in one or "[DRAFT" in hyp or "[TODO" in one or "[TODO" in hyp:
                    is_draft = True
            if is_draft:
                drafts.add(tk.upper())
    except Exception:
        pass
    return drafts


def _collect_all_candidates() -> List[Dict]:
    """Read every candidate file across candidates/ and subfolders.

    POST-EDGE GATE: Tickers present in _curated_rationales.json's `_archived` block
    are filtered out — they represent opportunities where the catalyst has publicly
    resolved and the market has priced the outcome. Not surfaced.

    DRAFT GATE: Tickers whose curated rationale is still `_draft: true` are
    excluded from published artifacts so TODO scaffolds never leak into PDFs.
    """
    cand_root = REPO / "candidates"
    results: List[Dict] = []
    if not cand_root.exists():
        return results

    post_edge = _load_post_edge_archive()
    drafts = _load_draft_tickers()

    # Top-level markdown candidates (flat = active)
    for p in sorted(cand_root.iterdir()):
        if p.is_file() and p.suffix == ".md":
            rec = _parse_md_candidate(p)
            if rec:
                tk_upper = (rec.get("ticker") or "").upper()
                if rec.get("ticker") in post_edge:
                    continue  # post-edge — skip
                if tk_upper in drafts:
                    continue  # draft — skip
                rec["stage"] = "active"
                rec["_source_path"] = str(p)
                results.append(rec)

    # Subfolders
    for sub in ("immediate", "delivered", "watchlist", "archive"):
        d = cand_root / sub
        if not d.exists():
            continue
        for p in sorted(d.iterdir()):
            if p.is_file():
                if p.suffix == ".md":
                    rec = _parse_md_candidate(p)
                elif p.suffix == ".json":
                    rec = _parse_json_candidate(p)
                else:
                    continue
                if rec:
                    tk_upper = (rec.get("ticker") or "").upper()
                    if rec.get("ticker") in post_edge:
                        continue  # post-edge — skip
                    if tk_upper in drafts:
                        continue  # draft — skip
                    rec["stage"] = sub
                    rec["_source_path"] = str(p)
                    results.append(rec)

    # Sort: stage priority then score desc then ticker asc
    results.sort(key=lambda r: (_stage_sort_key(r.get("stage", "")), -(r.get("score") or 0.0), r.get("ticker") or ""))
    return results


def generate_candidates_summary(
    out_dir: Optional[Path] = None,
    include_archive: bool = False,
) -> Path:
    """Build a comprehensive PDF of ALL candidates across the pipeline.

    One PDF. One section per stage (immediate, active, delivered, watchlist).
    Each candidate: ticker, company, score, stage, strategy/profile, date
    identified, key dates, one-paragraph rationale, source links.
    """
    s = _styles()
    now = _now_utc()
    stamp = now.strftime("%Y-%m-%d_%H%M")
    out_dir = out_dir or CANDIDATES_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = out_dir / f"{stamp}_candidates_summary.pdf"

    cands = _collect_all_candidates()
    if not include_archive:
        cands = [c for c in cands if c.get("stage") != "archive"]

    # Bucket by stage
    by_stage: Dict[str, List[Dict]] = {}
    for c in cands:
        by_stage.setdefault(c.get("stage", "active"), []).append(c)

    doc = SimpleDocTemplate(
        str(pdf), pagesize=LETTER,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.55 * inch, bottomMargin=0.55 * inch,
        title=f"Candidates Summary — {stamp}",
    )
    story: List[Any] = []

    # Cover block
    story.append(Paragraph(f"Candidate Pipeline Summary — {now.strftime('%Y-%m-%d %H:%M UTC')}", s["Title"]))
    story.append(Paragraph(
        f"Total candidates: <b>{len(cands)}</b> &nbsp;&nbsp; "
        f"Immediate: <b>{len(by_stage.get('immediate', []))}</b> &nbsp; "
        f"Active: <b>{len(by_stage.get('active', []))}</b> &nbsp; "
        f"Delivered: <b>{len(by_stage.get('delivered', []))}</b> &nbsp; "
        f"Watchlist: <b>{len(by_stage.get('watchlist', []))}</b>",
        s["Body"],
    ))
    story.append(Paragraph(
        "Stages: <b>immediate</b> = actionable within current session; "
        "<b>active</b> = live candidate under monitoring; "
        "<b>delivered</b> = thesis resolved (approved / closed / voted); "
        "<b>watchlist</b> = promising but needs enrichment or waiting on data.",
        s["Small"],
    ))
    story.append(Spacer(1, 10))

    # Master index table (one row per candidate, compact)
    idx_rows = [["Stage", "Ticker", "MIC", "Company / Title", "Score", "Strategy", "Date"]]
    for c in cands:
        idx_rows.append([
            c.get("stage", "")[:10],
            _escape(c.get("ticker", ""))[:14],
            _escape(c.get("mic", ""))[:5],
            _escape(_truncate(c.get("title", ""), 48)),
            f"{c['score']:.1f}" if c.get("score") is not None else "—",
            _escape(_truncate(c.get("strategy", ""), 22)),
            c.get("date_identified") or c.get("last_update") or "",
        ])
    idx_table = Table(idx_rows, colWidths=[0.7*inch, 0.7*inch, 0.45*inch, 2.8*inch, 0.5*inch, 1.3*inch, 0.75*inch], repeatRows=1)
    idx_table.setStyle(_table_style())
    story.append(Paragraph("Index — all candidates", s["H2"]))
    story.append(idx_table)
    story.append(PageBreak())

    # Detail sections by stage
    stage_order = ["immediate", "active", "delivered", "watchlist"]
    if include_archive:
        stage_order.append("archive")

    for stg in stage_order:
        items = by_stage.get(stg, [])
        if not items:
            continue
        story.append(Paragraph(f"{stg.upper()} — {len(items)} candidate(s)", s["H2"]))

        for c in items:
            # Candidate header
            tkr = _escape(c.get("ticker", ""))
            mic = _escape(c.get("mic", ""))
            title = _escape(_truncate(c.get("title", ""), 110))
            score_s = f"{c['score']:.1f}" if c.get("score") is not None else "—"
            header = f"<b>{tkr}</b>"
            if mic:
                header += f" · {mic}"
            header += f" &nbsp;|&nbsp; Score: <b>{score_s}</b> &nbsp;|&nbsp; Profile: {_escape(c.get('profile', ''))} &nbsp;|&nbsp; Stage: {stg}"
            story.append(Paragraph(header, s["H3"]))
            story.append(Paragraph(f"<b>{title}</b>", s["Body"]))

            # Key fields line
            line_parts = []
            if c.get("status"):
                line_parts.append(f"Status: {_escape(c['status'])}")
            if c.get("strategy"):
                line_parts.append(f"Strategy: {_escape(c['strategy'])}")
            if c.get("date_identified"):
                line_parts.append(f"Identified: {c['date_identified']}")
            if c.get("last_update"):
                line_parts.append(f"Updated: {c['last_update']}")
            if line_parts:
                story.append(Paragraph(" &nbsp;·&nbsp; ".join(line_parts), s["Small"]))

            # Key dates
            if c.get("key_dates"):
                kd = ", ".join(c["key_dates"][:4])
                story.append(Paragraph(f"<b>Key dates:</b> {_escape(kd)}", s["Body"]))

            # Convergence note (json candidates)
            if c.get("convergence_note"):
                story.append(Paragraph(f"<b>Convergence:</b> {_escape(c['convergence_note'])}", s["Body"]))

            # Rationale / thesis
            th = c.get("thesis") or ""
            if th:
                story.append(Paragraph(f"<b>Rationale:</b> {_inline(_truncate(th, 700))}", s["Body"]))

            # Sources
            if c.get("urls"):
                url_text = " &nbsp;|&nbsp; ".join(
                    f'<link href="{u}" color="#0b3d91">{_escape(_truncate(u, 70))}</link>'
                    for u in c["urls"][:2]
                )
                story.append(Paragraph(f"<b>Sources:</b> {url_text}", s["Small"]))

            # File reference
            p = Path(c.get("path", ""))
            try:
                rel = p.relative_to(REPO)
            except Exception:
                rel = p.name
            story.append(Paragraph(f"<i>File:</i> {_escape(str(rel))}", s["Small"]))
            story.append(Spacer(1, 8))

        story.append(PageBreak())

    # Footer
    footer_txt = (
        "Generated by tools/report_generator.py generate_candidates_summary() at "
        + now.isoformat()
        + ". Next auto-refresh runs on the <b>unified-reporting</b> scheduled task (every 4h at :30)."
    )
    story.append(Paragraph(footer_txt, s["Small"]))

    doc.build(story)
    return pdf


def _cli_candidates_summary():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--include-archive", action="store_true")
    args = ap.parse_args()
    p = generate_candidates_summary(include_archive=args.include_archive)
    print(str(p))



# --------------------------------------------------------------------
# Two-PDF structure — executive summary + detail book (D-008, 2026-04-17)
# --------------------------------------------------------------------

def _parse_catalyst_date(raw: str, today: datetime) -> tuple:
    """Return (date_str_display, days_away_or_None)."""
    if not raw:
        return "—", None
    s = raw.strip()
    # ISO date
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            days = (dt.date() - today.date()).days
            return m.group(1), days
        except Exception:
            pass
    # "April 30, 2026" / "Apr 30 2026"
    for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"):
        try:
            dt = datetime.strptime(s[:30], fmt).replace(tzinfo=timezone.utc)
            days = (dt.date() - today.date()).days
            return dt.strftime("%Y-%m-%d"), days
        except Exception:
            continue
    # "May 2026" / "Q2 2026" / "H2 2026"
    m2 = re.match(
        r"^((?:Q[1-4]|H[12]|January|February|March|April|May|June|July|August|"
        r"September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|"
        r"Oct|Nov|Dec)\s+\d{4})",
        s, re.IGNORECASE,
    )
    if m2:
        return m2.group(1), None
    return s[:24], None


def _extract_catalyst_for_summary(cand: Dict, today: datetime) -> Dict[str, Any]:
    """Pick the soonest FORWARD-looking catalyst date for a candidate row.

    Historical dates (signal-discovery, filing dates already priced in) are
    skipped. Preference order:
      1. Soonest date >= today (-3 days slop) from key_dates.
      2. Any ISO/month-name date parsed from the curated rationale 'when' field.
      3. Last-resort: the latest parseable date, even if past.
    """
    kd_list = cand.get("key_dates") or []
    forward: List[tuple] = []
    any_parsed: List[tuple] = []
    for raw in kd_list:
        disp, days = _parse_catalyst_date(raw, today)
        if days is not None:
            any_parsed.append((days, disp, raw))
            if days >= -3:
                forward.append((days, disp, raw))

    if forward:
        forward.sort(key=lambda t: t[0])
        d, disp, raw = forward[0]
        return {"display": disp, "days": d, "raw": raw}

    # Try curated rationale 'when' field
    tk = (cand.get("ticker") or "").upper().strip()
    if tk:
        curated = _load_curated_rationales()
        rat = curated.get(tk)
        if rat and rat.get("when"):
            when_text = rat["when"]
            m = re.search(r"(\d{4}-\d{2}-\d{2})", when_text)
            if m:
                disp, days = _parse_catalyst_date(m.group(1), today)
                if days is not None:
                    return {"display": disp, "days": days, "raw": m.group(1)}
            m2 = re.search(
                r"((?:January|February|March|April|May|June|July|August|"
                r"September|October|November|December)\s+\d{1,2},?\s+\d{4})",
                when_text, re.IGNORECASE,
            )
            if m2:
                disp, days = _parse_catalyst_date(m2.group(1), today)
                if days is not None:
                    return {"display": disp, "days": days, "raw": m2.group(1)}

    if any_parsed:
        any_parsed.sort(key=lambda t: t[0])
        d, disp, raw = any_parsed[-1]
        return {"display": disp, "days": d, "raw": raw}

    return {"display": "—", "days": None, "raw": ""}


def _one_line_why(cand: Dict) -> str:
    """Compress thesis into a short rationale. Skip short/empty leads."""
    t = (cand.get("thesis") or "").strip()
    if not t:
        return "—"
    # Strip all bold/emphasis markup and emphasis runs anywhere
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)
    t = re.sub(r"__([^_]+)__", r"\1", t)
    t = re.sub(r"\s+", " ", t).strip()
    # Collect first few sentences until we have >= 12 words of substance
    sentences = re.split(r"(?<=[\.!\?])\s+", t)
    collected: List[str] = []
    total_words = 0
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        # Skip short fragments (< 4 words) — they're usually leading bold labels
        wc = len(sent.split())
        collected.append(sent)
        total_words += wc
        if total_words >= 18:
            break
    first = " ".join(collected)
    words = first.split()
    if len(words) > 35:
        first = " ".join(words[:33]) + "…"
    return first[:260]


def _load_curated_rationales() -> Dict[str, Dict[str, str]]:
    """Load the hand-curated per-candidate rationales sidecar.

    File: candidates/_curated_rationales.json — keyed by uppercase ticker.
    Each entry provides: what / edge / expect / when. These are rendered
    as the richer executive-summary cards (Pedro's 2026-04-17 feedback:
    the one-line why was insufficient to make a decision to deep-dive).

    When working/_live_prices_overlay.json is present and fresh (<6h old),
    prefer the overlay so published price targets stay in sync with current
    reference prices.
    """
    try:
        from price_refresh import load_overlay_if_fresh  # type: ignore

        overlay = load_overlay_if_fresh()
        if overlay:
            return {k.upper(): v for k, v in overlay.items() if isinstance(v, dict) and not k.startswith("_")}
    except Exception:
        pass
    path = REPO / "candidates" / "_curated_rationales.json"
    data = _load_json(path, {})
    if not isinstance(data, dict):
        return {}
    # Strip meta
    return {k.upper(): v for k, v in data.items() if isinstance(v, dict) and not k.startswith("_")}


def _fallback_rationale(cand: Dict) -> Dict[str, Any]:
    """If no curated rationale exists for a ticker, synthesize baseline fields
    from the candidate .md content. Flags as auto-generated so it's clear the
    curated layer is missing."""
    thesis = (cand.get("thesis") or "").strip()
    thesis = re.sub(r"\*\*([^*]+)\*\*", r"\1", thesis)
    thesis = re.sub(r"\s+", " ", thesis).strip()
    sents = re.split(r"(?<=[\.!\?])\s+", thesis)
    buf = []
    tot = 0
    for sent in sents:
        sent = sent.strip()
        if not sent:
            continue
        buf.append(sent)
        tot += len(sent)
        if tot >= 500:
            break
    narrative = " ".join(buf)[:700] or "—"
    kd = cand.get("key_dates") or []
    when = "; ".join(kd[:3])[:200] if kd else "—"
    return {
        "one_liner": "[Curated rationale pending — see detail_book.pdf for full dossier.]",
        "hypothesis": "[Auto-generated: see thesis narrative below.]",
        "thesis": narrative,
        "expected_outcome": "[Curated outcome pending.]",
        "price_targets": {
            "reference_price": "—",
            "upside_base": "—",
            "upside_best": "—",
            "downside": "—",
        },
        "time_sensitivity": when,
        "kill_watch": "[See detail_book.pdf]",
        "catalyst_date_iso": "",
    }


def _urgency_band(text: str) -> tuple:
    """Map free-text time_sensitivity to (label, color). Cheap heuristic."""
    if not text:
        return ("—", "#999999")
    lo = text.lower()
    if lo.startswith("very high") or "very high" in lo[:30]:
        return ("VERY HIGH", "#b00020")
    if lo.startswith("high") or "high — " in lo[:20] or "high —" in lo[:20]:
        return ("HIGH", "#c62828")
    if lo.startswith("medium-high") or "medium-high" in lo[:30]:
        return ("MEDIUM-HIGH", "#e65100")
    if lo.startswith("medium") or "medium" in lo[:20]:
        return ("MEDIUM", "#b8860b")
    if lo.startswith("low") or "low" in lo[:15]:
        return ("LOW", "#2e7d32")
    return ("—", "#555555")


def generate_executive_summary(
    out_dir: Optional[Path] = None,
    include_archive: bool = False,
) -> Path:
    """Executive summary: one rich card per candidate answering what/edge/expect/when.

    Pedro's 2026-04-17 directive: every candidate needs a self-explanatory case
    — what is happening, why it's interesting, what to expect, and when — enough
    to decide whether to commission a deep-dive. A compact table with a one-line
    rationale was too thin. This function renders a card per candidate drawing
    from the hand-curated candidates/_curated_rationales.json sidecar.
    """
    s = _styles()
    now = _now_utc()
    today = now
    stamp = now.strftime("%Y-%m-%d_%H%M")
    out_dir = out_dir or CANDIDATES_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = out_dir / "executive_summary.pdf"

    cands = _collect_all_candidates()
    if not include_archive:
        cands = [c for c in cands if c.get("stage") != "archive"]

    curated = _load_curated_rationales()

    # Enrich
    enriched = []
    for c in cands:
        cat = _extract_catalyst_for_summary(c, today)
        tk = (c.get("ticker") or "").upper().strip()
        rationale = curated.get(tk) or _fallback_rationale(c)
        enriched.append({**c, "_cat": cat, "_rat": rationale})

    # Sort: stage then catalyst days then score desc
    def sort_key(c):
        days = c["_cat"].get("days")
        days_key = days if days is not None else 10_000
        return (_stage_sort_key(c.get("stage", "")), days_key, -(c.get("score") or 0))
    enriched.sort(key=sort_key)

    # Card style — portrait LETTER with generous margins
    doc = SimpleDocTemplate(
        str(pdf), pagesize=LETTER,
        leftMargin=0.55 * inch, rightMargin=0.55 * inch,
        topMargin=0.55 * inch, bottomMargin=0.55 * inch,
        title=f"Candidates — Executive Summary {stamp}",
    )
    story: List[Any] = []

    # Cover
    story.append(Paragraph(
        f"Candidate Pipeline — Executive Summary",
        s["Title"],
    ))
    story.append(Paragraph(
        f"{now.strftime('%Y-%m-%d %H:%M UTC')} &nbsp;·&nbsp; {len(enriched)} candidate(s)",
        s["Small"],
    ))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "<b>How to read this report.</b> Each candidate gets a full page that walks through "
        "the case in plain English. The layout: (1) <b>One-liner</b> — the situation in one sentence; "
        "(2) <b>Hypothesis</b> — the specific bet we're making; (3) <b>Thesis</b> — the narrative "
        "with enough background for a non-specialist to follow; (4) <b>Expected outcome</b> — what "
        "happens if we're right; (5) <b>Price targets</b> — explicit $ moves and % up/down from "
        "today's price; (6) <b>Time sensitivity</b> — how urgent, with a concrete window; "
        "(7) <b>Kill watch</b> — what would invalidate the thesis. Full dossiers are in "
        "<b>detail_book.pdf</b>. Sorted by urgency and proximity to catalyst.",
        s["Body"],
    ))
    story.append(Spacer(1, 10))

    # At-a-glance index: two rows per candidate so the one-liner displays in full with no truncation
    # Row A: meta — #, ticker, next catalyst, T-days, urgency
    # Row B: full-width wrapped one-liner (spans all columns)
    idx_style = ParagraphStyle(
        "IdxOneLiner", parent=s["Small"], fontSize=9, leading=11.5,
        leftIndent=6, spaceBefore=1, spaceAfter=1,
    )
    header_row = ["#", "Ticker", "Next catalyst", "T−days", "Urgency", "One-line situation"]
    idx_rows = [header_row]
    idx_col_widths = [0.3*inch, 0.7*inch, 1.15*inch, 0.6*inch, 0.95*inch, 3.75*inch]
    span_commands = []
    body_style_cmds = []
    for i, c in enumerate(enriched, 1):
        cat = c["_cat"]
        rat = c["_rat"]
        days_txt = "—"
        if cat.get("days") is not None:
            d = cat["days"]
            days_txt = f"T{d:+d}" if d != 0 else "T0"
        urg_label, urg_color = _urgency_band(rat.get("time_sensitivity", ""))
        urgency_para = Paragraph(
            f"<font color='{urg_color}'><b>{urg_label}</b></font>", s["Small"],
        )
        # Row A — meta (last column holds a short "(see below)" placeholder that is hidden by the span)
        meta_row_idx = len(idx_rows)
        idx_rows.append([
            str(i),
            _escape(c.get("ticker") or "")[:10],
            _escape(cat.get("display") or "—")[:18],
            days_txt,
            urgency_para,
            "",  # one-line-situation column empty on meta row; real content on row below
        ])
        # Row B — full-width one-liner, no truncation, wraps as needed
        one_liner_full = _escape(rat.get("one_liner", "") or c.get("title", "") or "—")
        idx_rows.append([
            Paragraph(one_liner_full, idx_style), "", "", "", "", "",
        ])
        oneliner_row_idx = meta_row_idx + 1
        # Span the one-liner across all 6 columns on row B
        span_commands.append(("SPAN", (0, oneliner_row_idx), (5, oneliner_row_idx)))
        # Style: the one-liner row gets a subtle left-indent feel; meta row stays centered
        body_style_cmds.append(("BACKGROUND", (0, oneliner_row_idx), (5, oneliner_row_idx), colors.HexColor("#f7f9fc")))
        body_style_cmds.append(("TOPPADDING", (0, oneliner_row_idx), (5, oneliner_row_idx), 2))
        body_style_cmds.append(("BOTTOMPADDING", (0, oneliner_row_idx), (5, oneliner_row_idx), 4))
    idx = Table(
        idx_rows,
        colWidths=idx_col_widths,
        repeatRows=1,
    )
    idx.setStyle(_table_style())
    # Apply row spans + one-liner row styling on top of the base table style
    from reportlab.platypus import TableStyle as _TS
    idx.setStyle(_TS(span_commands + body_style_cmds))
    story.append(idx)
    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "— Full case per candidate on following pages —",
        s["Small"],
    ))
    story.append(PageBreak())

    # Full-page card per candidate
    for i, c in enumerate(enriched, 1):
        cat = c["_cat"]
        rat = c["_rat"]
        days_txt = ""
        if cat.get("days") is not None:
            d = cat["days"]
            days_txt = f"  (T{d:+d})" if d != 0 else "  (T0)"

        ticker = _escape(c.get("ticker") or "")
        title = _escape(c.get("title") or "")
        stage = _escape((c.get("stage") or "").upper())
        strategy = _escape(c.get("strategy") or "")
        score = f"{c['score']:.1f}" if c.get("score") is not None else "—"
        next_cat = _escape(cat.get("display") or "—") + days_txt

        # Urgency band
        urg_label, urg_color = _urgency_band(rat.get("time_sensitivity", ""))

        # Header
        hdr_inner = (
            f"<b>#{i} &nbsp; {ticker}</b>"
            f" &nbsp;·&nbsp; {title}"
        )
        story.append(Paragraph(hdr_inner, s["H2"]))
        meta = (
            f"<b>Stage:</b> <font color='#b8860b'>{stage}</font> &nbsp;|&nbsp; "
            f"<b>Strategy:</b> {strategy} &nbsp;|&nbsp; "
            f"<b>Score:</b> {score} &nbsp;|&nbsp; "
            f"<b>Next catalyst:</b> {next_cat} &nbsp;|&nbsp; "
            f"<b>Urgency:</b> <font color='{urg_color}'><b>{urg_label}</b></font>"
        )
        story.append(Paragraph(meta, s["Small"]))
        story.append(Spacer(1, 6))

        # One-liner — big, blue, above the fold
        one_liner = _escape(rat.get("one_liner", "") or "—")
        story.append(Paragraph(
            f'<font color="#0b3d91"><i>{one_liner}</i></font>',
            ParagraphStyle("OneLiner", parent=s["Body"], fontSize=11, leading=14, spaceAfter=8),
        ))

        # Structured fields
        def _p(txt: str, size: float = 9.5) -> Paragraph:
            txt = _escape(txt or "—")
            return Paragraph(txt, ParagraphStyle(
                "B", parent=s["Body"], fontSize=size, leading=size + 2.5,
            ))

        # Hypothesis
        story.append(Paragraph("<b>HYPOTHESIS</b> <font color='#555555' size='8'>(the specific bet)</font>", s["H3"]))
        story.append(_p(rat.get("hypothesis", "")))
        story.append(Spacer(1, 4))

        # Thesis — the narrative, in a tinted box
        story.append(Paragraph("<b>THESIS</b> <font color='#555555' size='8'>(why we believe it, in plain English)</font>", s["H3"]))
        thesis_para = _p(rat.get("thesis", ""))
        thesis_box = Table([[thesis_para]], colWidths=[7.3*inch])
        thesis_box.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8f9fc")),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LINEBEFORE", (0, 0), (0, -1), 3, colors.HexColor("#0b3d91")),
        ]))
        story.append(thesis_box)
        story.append(Spacer(1, 6))

        # Expected outcome
        story.append(Paragraph("<b>EXPECTED OUTCOME</b> <font color='#555555' size='8'>(what happens if we're right)</font>", s["H3"]))
        story.append(_p(rat.get("expected_outcome", "")))
        story.append(Spacer(1, 4))

        # Price targets (structured)
        pt = rat.get("price_targets") or {}
        if isinstance(pt, dict) and pt:
            story.append(Paragraph("<b>PRICE TARGETS</b>", s["H3"]))
            pt_rows = [
                ["Reference", _escape(str(pt.get("reference_price", "—")))],
                ["Upside (base)", _escape(str(pt.get("upside_base", "—")))],
                ["Upside (best)", _escape(str(pt.get("upside_best", "—")))],
                ["Downside", _escape(str(pt.get("downside", "—")))],
            ]
            pt_tbl_rows = [[
                Paragraph(f"<b>{r[0]}</b>", s["Small"]),
                Paragraph(r[1], s["Body"]),
            ] for r in pt_rows]
            pt_tbl = Table(pt_tbl_rows, colWidths=[1.3*inch, 6.0*inch])
            pt_tbl.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef2fa")),
                ("BACKGROUND", (1, 1), (1, 1), colors.HexColor("#f1f8f3")),  # upside base greenish
                ("BACKGROUND", (1, 2), (1, 2), colors.HexColor("#e8f5ea")),  # upside best greener
                ("BACKGROUND", (1, 3), (1, 3), colors.HexColor("#fbecec")),  # downside reddish
                ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#dddddd")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#bbbbbb")),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            story.append(pt_tbl)
            story.append(Spacer(1, 6))

        # Time sensitivity (full text with urgency band color accent)
        ts_text = rat.get("time_sensitivity", "") or "—"
        story.append(Paragraph(
            f"<b>TIME SENSITIVITY</b> &nbsp;<font color='{urg_color}'><b>[{urg_label}]</b></font>",
            s["H3"],
        ))
        ts_para = _p(ts_text)
        ts_box = Table([[ts_para]], colWidths=[7.3*inch])
        ts_box.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fffbe8")),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LINEBEFORE", (0, 0), (0, -1), 3, colors.HexColor(urg_color)),
        ]))
        story.append(ts_box)
        story.append(Spacer(1, 6))

        # Kill watch
        story.append(Paragraph(
            "<b>KILL WATCH</b> <font color='#555555' size='8'>(what would invalidate the thesis)</font>",
            s["H3"],
        ))
        story.append(_p(rat.get("kill_watch", "")))

        # Page break unless last
        if i < len(enriched):
            story.append(PageBreak())

    try:
        story.append(PageBreak())
        _append_legal_desk(story, s, now)
    except Exception as e:
        story.append(Paragraph(f"<font color='#999'>[Legal Desk skipped: {e}]</font>", s["Small"]))

    try:
        story.append(PageBreak())
        _append_biotech_desk(story, s, now)
    except Exception as e:
        story.append(Paragraph(f"<font color='#999'>[Biotech Desk skipped: {e}]</font>", s["Small"]))

    story.append(Spacer(1, 8))
    story.append(Paragraph(
        f"Generated by tools/report_generator.py generate_executive_summary() at {now.isoformat()}. "
        f"Candidates pass the thesis gate (tools/candidate_gate.py). "
        f"Rationales sourced from candidates/_curated_rationales.json (hand-curated; updated any time a candidate's thesis changes). "
        f"Full dossiers: detail_book.pdf. Stubs without rationale: candidates/rejected_pending_thesis/.",
        s["Small"],
    ))

    doc.build(story)
    return pdf


_LEGAL_COLOR_HEX = {
    "GREEN": "#2e7d32",
    "YELLOW": "#b88600",
    "ORANGE": "#d97706",
    "RED": "#b91c1c",
}


def _append_legal_desk(story: List[Any], s, now) -> None:
    try:
        from legal_enricher import summarize_legal_desk  # type: ignore
    except ImportError:
        try:
            from tools.legal_enricher import summarize_legal_desk  # type: ignore
        except Exception:
            story.append(Paragraph("<i>Legal Desk unavailable (enricher not importable)</i>", s["Small"]))
            return

    items = summarize_legal_desk(window_days=3, max_items=12)
    story.append(Paragraph("Legal Desk", s["Title"]))
    story.append(
        Paragraph(
            "Severity × Likelihood enrichment of litigation-profile signals (last 3 days). "
            "Framework: legal:legal-risk-assessment (GREEN ≤ 4, YELLOW 5-9, ORANGE 10-15, RED 16-25). "
            "Regulations surfaced heuristically for operator context.",
            s["Small"],
        )
    )
    story.append(Spacer(1, 6))

    if not items:
        story.append(
            Paragraph(
                "<i>No litigation-profile signals in the last 3 days. "
                "Run <b>python3 tools/legal_enricher.py</b> if the signal log has not been enriched yet.</i>",
                s["Body"],
            )
        )
        return

    counts = {"GREEN": 0, "YELLOW": 0, "ORANGE": 0, "RED": 0}
    for item in items:
        counts[item["color"]] = counts.get(item["color"], 0) + 1
    chip_html = " &nbsp; ".join(
        f"<font color='{_LEGAL_COLOR_HEX[key]}'><b>{key}</b></font>: {value}" for key, value in counts.items()
    )
    story.append(Paragraph(chip_html, s["Body"]))
    story.append(Spacer(1, 8))

    for item in items:
        color = _LEGAL_COLOR_HEX.get(item["color"], "#555")
        head_line = (
            f"<font color='{color}'><b>[{item['color']} · score {item['risk_score']}]</b></font> "
            f"<b>{item.get('ticker') or '—'}</b> "
            f"&nbsp;·&nbsp; <font color='#555'>{item.get('scanner', '')} / {item.get('signal_type', '')}</font>"
        )
        story.append(Paragraph(head_line, s["Body"]))
        story.append(Paragraph(f"<i>{(item.get('headline') or '')[:200]}</i>", s["Small"]))
        meta = (
            f"Severity: <b>{item['severity']}</b> · "
            f"Likelihood: <b>{item['likelihood']}</b> · "
            f"Date: {(str(item.get('source_date') or ''))[:10]}"
        )
        if item.get("filing_url"):
            meta += f" · <font color='#555'>{item['filing_url'][:90]}</font>"
        story.append(Paragraph(meta, s["Small"]))
        if item.get("regulations"):
            story.append(Paragraph("Regulations: " + "; ".join(item["regulations"]), s["Small"]))
        story.append(Spacer(1, 6))


_BIOTECH_COLOR_HEX = {
    "GREEN": "#2e7d32",
    "YELLOW": "#b88600",
    "ORANGE": "#d97706",
    "RED": "#b91c1c",
}


def _append_biotech_desk(story: List[Any], s, now) -> None:
    try:
        from biotech_enricher import summarize_biotech_desk  # type: ignore
    except ImportError:
        try:
            from tools.biotech_enricher import summarize_biotech_desk  # type: ignore
        except Exception:
            story.append(Paragraph("<i>Biotech Desk unavailable (enricher not importable)</i>", s["Small"]))
            return

    items = summarize_biotech_desk(window_days=7, max_items=12)
    story.append(Paragraph("Biotech Desk", s["Title"]))
    story.append(
        Paragraph(
            "Endpoint × Sponsor × Indication enrichment of binary-catalyst biotech signals "
            "(last 7 days). Framework: biotech_enricher v1.",
            s["Small"],
        )
    )
    story.append(Spacer(1, 6))

    if not items:
        story.append(
            Paragraph(
                "<i>No biotech binary-catalyst signals in the last 7 days. "
                "Run <b>python3 tools/biotech_enricher.py</b> if the signal log has not been enriched yet.</i>",
                s["Body"],
            )
        )
        return

    counts = {"GREEN": 0, "YELLOW": 0, "ORANGE": 0, "RED": 0}
    for item in items:
        counts[item["enrichment_color"]] = counts.get(item["enrichment_color"], 0) + 1
    chip_html = " &nbsp; ".join(
        f"<font color='{_BIOTECH_COLOR_HEX[key]}'><b>{key}</b></font>: {value}" for key, value in counts.items()
    )
    story.append(Paragraph(chip_html, s["Body"]))
    story.append(Spacer(1, 8))

    for item in items:
        color = _BIOTECH_COLOR_HEX.get(item["enrichment_color"], "#555")
        head_line = (
            f"<font color='{color}'><b>[{item['enrichment_color']} · score {item['enrichment_score']}]</b></font> "
            f"<b>{item.get('ticker') or '—'}</b> &nbsp;·&nbsp; "
            f"<font color='#555'>{item.get('sponsor') or '—'} · {item.get('indication') or '—'} · {item.get('mechanism') or '—'}</font>"
        )
        story.append(Paragraph(head_line, s["Body"]))
        story.append(Paragraph(f"<i>{(item.get('headline') or '')[:200]}</i>", s["Small"]))
        pc_date = item.get("primary_completion_date") or ""
        dtr = item.get("days_until_readout")
        ap = item.get("approval_probability")
        up = item.get("upside_pct")
        dn = item.get("downside_pct")
        meta = (
            f"Endpoint: <b>{item.get('endpoint_strength') or '—'}</b> · "
            f"Sponsor: <b>{item.get('sponsor_tier') or '—'}</b> · "
            f"PCD: {pc_date[:10]}"
            + (f" (T−{dtr}d)" if dtr is not None else "")
            + (f" · AP: {int(round(ap * 100))}%" if isinstance(ap, (int, float)) else "")
            + (f" · Upside: +{up}%" if isinstance(up, (int, float)) else "")
            + (f" · Downside: −{dn}%" if isinstance(dn, (int, float)) else "")
        )
        if item.get("nct_id"):
            meta += f" · <font color='#555'>{item['nct_id']}</font>"
        story.append(Paragraph(meta, s["Small"]))
        if item.get("source_url"):
            story.append(Paragraph(f"<font color='#555'>{item['source_url'][:120]}</font>", s["Small"]))
        story.append(Spacer(1, 6))


def generate_detail_book(
    out_dir: Optional[Path] = None,
    include_archive: bool = False,
) -> Path:
    """Detail book: one candidate per page with structured fields.

    Each page: Ticker + Company · Situation · Next catalyst + date · Why under-priced ·
    Timeline · Kill conditions · Sources.
    """
    s = _styles()
    now = _now_utc()
    today = now
    stamp = now.strftime("%Y-%m-%d_%H%M")
    out_dir = out_dir or CANDIDATES_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = out_dir / "detail_book.pdf"

    cands = _collect_all_candidates()
    if not include_archive:
        cands = [c for c in cands if c.get("stage") != "archive"]

    # Same sort as executive summary
    def sort_key(c):
        cat = _extract_catalyst_for_summary(c, today)
        days = cat.get("days")
        days_key = days if days is not None else 10_000
        return (_stage_sort_key(c.get("stage", "")), days_key, -(c.get("score") or 0))
    cands.sort(key=sort_key)

    doc = SimpleDocTemplate(
        str(pdf), pagesize=LETTER,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        title=f"Candidates — Detail Book {stamp}",
    )
    story: List[Any] = []

    # Cover
    story.append(Paragraph(
        f"Candidate Detail Book &nbsp;·&nbsp; {now.strftime('%Y-%m-%d %H:%M UTC')}",
        s["Title"],
    ))
    story.append(Paragraph(
        f"{len(cands)} candidates, one per page. Each entry shows the situation, "
        f"next catalyst, why under-priced, kill conditions, and sources. Pairs with "
        f"<b>executive_summary.pdf</b> for the at-a-glance table.",
        s["Body"],
    ))
    story.append(Spacer(1, 12))
    story.append(Paragraph("Index", s["H2"]))
    idx_rows = [["#", "Ticker", "MIC", "Company", "Next catalyst", "Score"]]
    for i, c in enumerate(cands, 1):
        cat = _extract_catalyst_for_summary(c, today)
        idx_rows.append([
            str(i),
            _escape(c.get("ticker") or "")[:10],
            _escape(c.get("mic") or "")[:6],
            _escape(_truncate(c.get("title", ""), 60)),
            _escape(cat.get("display") or "—")[:16],
            f"{c['score']:.1f}" if c.get("score") is not None else "—",
        ])
    idx_table = Table(idx_rows, colWidths=[0.3*inch, 0.7*inch, 0.45*inch, 3.2*inch, 1.2*inch, 0.55*inch], repeatRows=1)
    idx_table.setStyle(_table_style())
    story.append(idx_table)
    story.append(PageBreak())

    # One page per candidate
    for i, c in enumerate(cands, 1):
        cat = _extract_catalyst_for_summary(c, today)
        tkr = _escape(c.get("ticker") or "")
        mic = _escape(c.get("mic") or "")
        company = _escape(_truncate(c.get("title", ""), 110))
        score = f"{c['score']:.1f}" if c.get("score") is not None else "—"
        hdr = f"<b>{i}. {tkr}</b>"
        if mic:
            hdr += f" · {mic}"
        hdr += f" &nbsp;|&nbsp; Score: <b>{score}</b> &nbsp;|&nbsp; Stage: {_escape(c.get('stage') or '')} &nbsp;|&nbsp; Strategy: {_escape(c.get('strategy') or '')}"
        story.append(Paragraph(hdr, s["H2"]))
        story.append(Paragraph(f"<b>{company}</b>", s["Body"]))
        story.append(Spacer(1, 6))

        # Next catalyst panel
        days_txt = "—"
        if cat.get("days") is not None:
            d = cat["days"]
            days_txt = f"T{d:+d} ({'today' if d == 0 else (str(d) + ' days away' if d > 0 else str(abs(d)) + ' days ago')})"
        cat_lines = [
            f"<b>Date:</b> {_escape(cat.get('display') or '—')}",
            f"<b>Relative:</b> {days_txt}",
        ]
        if cat.get("raw"):
            cat_lines.append(f"<b>Raw:</b> {_escape(cat['raw'][:120])}")
        story.append(Paragraph("NEXT CATALYST", s["H3"]))
        for ln in cat_lines:
            story.append(Paragraph(ln, s["Body"]))
        story.append(Spacer(1, 6))

        # Situation / Thesis — prefer the rich thesis we already extracted
        story.append(Paragraph("SITUATION / THESIS", s["H3"]))
        th = c.get("thesis") or "(no thesis extracted — see file)"
        story.append(Paragraph(_inline(_truncate(th, 1400)), s["Body"]))
        story.append(Spacer(1, 6))

        # Key dates (full list)
        if c.get("key_dates"):
            story.append(Paragraph("KEY DATES", s["H3"]))
            for kd in c["key_dates"][:6]:
                story.append(Paragraph(f"• {_escape(kd)}", s["Body"]))
            story.append(Spacer(1, 6))

        # Status / identified / updated
        meta_bits = []
        if c.get("status"):
            meta_bits.append(f"<b>Status:</b> {_escape(c['status'])}")
        if c.get("date_identified"):
            meta_bits.append(f"<b>Identified:</b> {c['date_identified']}")
        if c.get("last_update"):
            meta_bits.append(f"<b>Updated:</b> {c['last_update']}")
        if meta_bits:
            story.append(Paragraph(" &nbsp;·&nbsp; ".join(meta_bits), s["Small"]))
            story.append(Spacer(1, 6))

        # Sources
        if c.get("urls"):
            story.append(Paragraph("SOURCES", s["H3"]))
            for u in c["urls"][:5]:
                story.append(Paragraph(
                    f'<link href="{u}" color="#0b3d91">{_escape(_truncate(u, 110))}</link>',
                    s["Small"],
                ))

        # File reference
        p = Path(c.get("path", ""))
        try:
            rel = p.relative_to(REPO)
        except Exception:
            rel = p.name
        story.append(Spacer(1, 8))
        story.append(Paragraph(f"<i>Source file:</i> {_escape(str(rel))}", s["Small"]))

        story.append(PageBreak())

    # Footer page
    story.append(Paragraph(
        f"Generated by tools/report_generator.py generate_detail_book() at {now.isoformat()}.",
        s["Small"],
    ))

    doc.build(story)
    return pdf


# --------------------------------------------------------------------
# CLI hook — re-register __main__ so new flags are exposed
# --------------------------------------------------------------------

# --------------------------------------------------------------------
# Published reporting layout (Conan/reporting/)
#   reporting/summary/executive_summary.pdf
#   reporting/dossiers/{TICKER}.pdf   (one per active candidate)
# --------------------------------------------------------------------

# Conan/ is the parent of unified_system/
PUBLISH_ROOT = REPO.parent / "reporting"
PUBLISH_SUMMARY_DIR = PUBLISH_ROOT / "summary"
PUBLISH_DOSSIERS_DIR = PUBLISH_ROOT / "dossiers"


def _render_card(story, s, c, rat, cat, i=None, total=None):
    """Render the executive-summary card sections for one candidate into `story`.
    Shared between the summary PDF and per-candidate dossiers so both stay in sync.
    """
    days_txt = ""
    if cat.get("days") is not None:
        d = cat["days"]
        days_txt = f"  (T{d:+d})" if d != 0 else "  (T0)"

    ticker = _escape(c.get("ticker") or "")
    title = _escape(c.get("title") or "")
    stage = _escape((c.get("stage") or "").upper())
    strategy = _escape(c.get("strategy") or "")
    score = f"{c['score']:.1f}" if c.get("score") is not None else "—"
    next_cat = _escape(cat.get("display") or "—") + days_txt
    urg_label, urg_color = _urgency_band(rat.get("time_sensitivity", ""))

    if i is not None and total is not None:
        hdr_inner = f"<b>#{i} &nbsp; {ticker}</b> &nbsp;·&nbsp; {title}"
    else:
        hdr_inner = f"<b>{ticker}</b> &nbsp;·&nbsp; {title}"
    story.append(Paragraph(hdr_inner, s["H2"]))
    meta = (
        f"<b>Stage:</b> <font color='#b8860b'>{stage}</font> &nbsp;|&nbsp; "
        f"<b>Strategy:</b> {strategy} &nbsp;|&nbsp; "
        f"<b>Score:</b> {score} &nbsp;|&nbsp; "
        f"<b>Next catalyst:</b> {next_cat} &nbsp;|&nbsp; "
        f"<b>Urgency:</b> <font color='{urg_color}'><b>{urg_label}</b></font>"
    )
    story.append(Paragraph(meta, s["Small"]))
    story.append(Spacer(1, 6))

    one_liner = _escape(rat.get("one_liner", "") or "—")
    story.append(Paragraph(
        f'<font color="#0b3d91"><i>{one_liner}</i></font>',
        ParagraphStyle("OneLiner2", parent=s["Body"], fontSize=11, leading=14, spaceAfter=8),
    ))

    def _p(txt, size=9.5):
        txt = _escape(txt or "—")
        return Paragraph(txt, ParagraphStyle(
            "B2", parent=s["Body"], fontSize=size, leading=size + 2.5,
        ))

    story.append(Paragraph("<b>HYPOTHESIS</b> <font color='#555555' size='8'>(the specific bet)</font>", s["H3"]))
    story.append(_p(rat.get("hypothesis", "")))
    story.append(Spacer(1, 4))

    story.append(Paragraph("<b>THESIS</b> <font color='#555555' size='8'>(why we believe it, in plain English)</font>", s["H3"]))
    thesis_para = _p(rat.get("thesis", ""))
    thesis_box = Table([[thesis_para]], colWidths=[7.3*inch])
    thesis_box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8f9fc")),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LINEBEFORE", (0, 0), (0, -1), 3, colors.HexColor("#0b3d91")),
    ]))
    story.append(thesis_box)
    story.append(Spacer(1, 6))

    story.append(Paragraph("<b>EXPECTED OUTCOME</b> <font color='#555555' size='8'>(what happens if we're right)</font>", s["H3"]))
    story.append(_p(rat.get("expected_outcome", "")))
    story.append(Spacer(1, 4))

    pt = rat.get("price_targets") or {}
    if isinstance(pt, dict) and pt:
        story.append(Paragraph("<b>PRICE TARGETS</b>", s["H3"]))
        pt_rows = [
            ["Reference", _escape(str(pt.get("reference_price", "—")))],
            ["Upside (base)", _escape(str(pt.get("upside_base", "—")))],
            ["Upside (best)", _escape(str(pt.get("upside_best", "—")))],
            ["Downside", _escape(str(pt.get("downside", "—")))],
        ]
        pt_tbl_rows = [[
            Paragraph(f"<b>{r[0]}</b>", s["Small"]),
            Paragraph(r[1], s["Body"]),
        ] for r in pt_rows]
        pt_tbl = Table(pt_tbl_rows, colWidths=[1.3*inch, 6.0*inch])
        pt_tbl.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef2fa")),
            ("BACKGROUND", (1, 1), (1, 1), colors.HexColor("#f1f8f3")),
            ("BACKGROUND", (1, 2), (1, 2), colors.HexColor("#e8f5ea")),
            ("BACKGROUND", (1, 3), (1, 3), colors.HexColor("#fbecec")),
            ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#dddddd")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#bbbbbb")),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(pt_tbl)
        story.append(Spacer(1, 6))

    ts_text = rat.get("time_sensitivity", "") or "—"
    story.append(Paragraph(
        f"<b>TIME SENSITIVITY</b> &nbsp;<font color='{urg_color}'><b>[{urg_label}]</b></font>",
        s["H3"],
    ))
    ts_para = _p(ts_text)
    ts_box = Table([[ts_para]], colWidths=[7.3*inch])
    ts_box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fffbe8")),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBEFORE", (0, 0), (0, -1), 3, colors.HexColor(urg_color)),
    ]))
    story.append(ts_box)
    story.append(Spacer(1, 6))

    story.append(Paragraph(
        "<b>KILL WATCH</b> <font color='#555555' size='8'>(what would invalidate the thesis)</font>",
        s["H3"],
    ))
    story.append(_p(rat.get("kill_watch", "")))


def generate_published_dossier(candidate, out_dir=None):
    out_dir = out_dir or PUBLISH_DOSSIERS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    tkr = (candidate.get("ticker") or "UNKNOWN").upper().strip()
    pdf = out_dir / f"{tkr}.pdf"
    now = _now_utc()
    today = now
    s = _styles()
    curated = _load_curated_rationales()
    cat = _extract_catalyst_for_summary(candidate, today)
    rat = curated.get(tkr) or _fallback_rationale(candidate)

    doc = SimpleDocTemplate(
        str(pdf), pagesize=LETTER,
        leftMargin=0.55 * inch, rightMargin=0.55 * inch,
        topMargin=0.55 * inch, bottomMargin=0.55 * inch,
        title=f"Dossier — {tkr}",
        author="Unified System",
    )
    story = []

    story.append(Paragraph(f"Dossier — <b>{_escape(tkr)}</b>", s["Title"]))
    src_name = Path(candidate.get("_source_path", "")).name or f"{tkr}.md"
    story.append(Paragraph(
        f"{now.strftime('%Y-%m-%d %H:%M UTC')} &nbsp;·&nbsp; "
        f"Source: candidates/{_escape(src_name)} &nbsp;·&nbsp; "
        f"Rationale: candidates/_curated_rationales.json",
        s["Small"],
    ))
    story.append(Spacer(1, 10))

    _render_card(story, s, candidate, rat, cat)
    story.append(PageBreak())

    src = candidate.get("_source_path")
    story.append(Paragraph("Background &amp; detail (source markdown)", s["H2"]))
    story.append(Paragraph(
        f"The section below is the longer analyst note for {_escape(tkr)}. "
        f"The card on the preceding page is the decision-ready summary.",
        s["Small"],
    ))
    story.append(Spacer(1, 6))
    if src and Path(src).exists():
        text = Path(src).read_text(encoding="utf-8", errors="replace")
        for block in _md_blocks(text):
            kind, content = block
            if kind == "h1":
                story.append(Paragraph(_escape(content), s["Title"]))
            elif kind == "h2":
                story.append(Paragraph(_escape(content), s["H2"]))
            elif kind == "h3":
                story.append(Paragraph(_escape(content), s["H3"]))
            elif kind == "code":
                story.append(Paragraph(f"<font face='Courier'>{_escape(content)}</font>", s["Mono"]))
            elif kind == "bullet":
                story.append(Paragraph(f"• {_inline(content)}", s["Body"]))
            elif kind == "p":
                story.append(Paragraph(_inline(content), s["Body"]))
            story.append(Spacer(1, 2))
    else:
        story.append(Paragraph(
            f"<i>No source markdown found for {_escape(tkr)}. "
            "The card on page 1 contains the full curated rationale.</i>",
            s["Body"],
        ))

    doc.build(story)
    return pdf


def generate_published_summary(out_dir=None, include_archive=False):
    out_dir = out_dir or PUBLISH_SUMMARY_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    src = generate_executive_summary(include_archive=include_archive)
    dst = out_dir / "executive_summary.pdf"
    import shutil
    shutil.copyfile(str(src), str(dst))
    return dst


def publish_reporting(include_archive=False):
    PUBLISH_SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    PUBLISH_DOSSIERS_DIR.mkdir(parents=True, exist_ok=True)
    for existing in PUBLISH_DOSSIERS_DIR.glob("*.pdf"):
        try:
            existing.unlink()
        except Exception:
            pass
    try:
        from price_refresh import refresh_and_write  # type: ignore

        meta = refresh_and_write()
        print(
            f"[publish_reporting] live price refresh: "
            f"refreshed={len(meta.get('tickers_refreshed', []))} "
            f"stale={len(meta.get('tickers_stale', []))}"
        )
    except Exception as e:
        print(f"[publish_reporting] price_refresh skipped: {e}")
    summary_path = generate_published_summary(include_archive=include_archive)
    cands = _collect_all_candidates()
    if not include_archive:
        cands = [c for c in cands if c.get("stage") != "archive"]
    dossiers = []
    for c in cands:
        try:
            p = generate_published_dossier(c)
            dossiers.append(str(p))
        except Exception as e:
            dossiers.append(f"ERROR {c.get('ticker')}: {e}")
    return {"summary": str(summary_path), "dossiers": dossiers, "count": len(dossiers)}


def _cli_two_pdfs():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--executive-summary", action="store_true")
    ap.add_argument("--detail-book", action="store_true")
    ap.add_argument("--both", action="store_true", help="Emit both executive_summary.pdf and detail_book.pdf (legacy)")
    ap.add_argument("--publish", action="store_true", help="Publish to Conan/reporting/ — summary + per-candidate dossiers")
    ap.add_argument("--include-archive", action="store_true")
    args = ap.parse_args()
    outs = []
    if args.publish:
        result = publish_reporting(include_archive=args.include_archive)
        outs.append(result["summary"])
        outs.extend(result["dossiers"])
    else:
        if args.executive_summary or args.both:
            outs.append(str(generate_executive_summary(include_archive=args.include_archive)))
        if args.detail_book or args.both:
            outs.append(str(generate_detail_book(include_archive=args.include_archive)))
    for o in outs:
        print(o)


def _main_v2():
    import sys
    if len(sys.argv) > 1 and sys.argv[1] in ("--executive-summary", "--detail-book", "--both", "--publish"):
        _cli_two_pdfs()
        return
    print(str(generate_candidates_summary(include_archive=False)))


if __name__ == "__main__":
    _main_v2()

# --- END OF FILE ---
