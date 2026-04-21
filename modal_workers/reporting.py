"""Weekly reporting (spec §7.3) + integrity sweep (spec §7.7).

Entry point: `reporting_weekly()` — called from app.py's Sunday 12:00 UTC Modal cron.

Does two things:

1. **Integrity sweep (§7.7)** — pre-render checks before rendering the PDF. Writes
   `operator_flags` for: orphan alerts (alerts referencing signals whose band has
   since changed), stuck-active candidates (state='active' with no candidate_events
   for >45 days), stuck-drafting thesis_jobs (status='drafting' older than 1 hour).
   Sweep failures don't block rendering.

2. **Executive-summary PDF** — active + watch candidates with one-liner, thesis,
   kill_watch, catalyst_date. Reportlab single-page layout. Upload to Storage at
   `reports/<yyyy>/<mm>/<yyyy-mm-dd>_executive_summary.pdf` and create a signed URL
   (7-day expiry) as a notification row so fan-out can email it under the
   pre-edge-gated email convention (see memory/email_alert_gating.md — weekly
   report distribution is whitelisted regardless of the pre-edge gate since the PDF
   is a curated digest, not a raw alert).

Deferred for a later pass (not blocking first weekly run):
- Per-candidate dossier PDFs (v1 generated one per active candidate)
- Weekly strategic PDF with scanner health trends + coverage maps
- Rendering from full dossier markdown rather than just rationales

v1 reference: `unified_system/unified_system/tools/report_generator.py::generate_weekly_strategic`.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from modal_workers.shared.supabase_client import SupabaseClient

NAME = "reporting_weekly"


# ---------------------------------------------------------------------------
# Integrity sweep (spec §7.7)
# ---------------------------------------------------------------------------

def integrity_sweep(client: SupabaseClient) -> Dict[str, Any]:
    """Pre-render data-integrity checks. Delegates to the SQL function
    `public.reporting_integrity_sweep()` (migration 23) which does the three
    joins + operator_flags UPSERTs in a single transaction. Never raises —
    returns an error summary if the RPC fails so rendering can still proceed.
    """
    try:
        result = client._rest("POST", "rpc/reporting_integrity_sweep", json_body={})
        # PostgREST returns the scalar jsonb directly for RPC-of-one-return.
        if isinstance(result, dict):
            return result
        if isinstance(result, list) and result and isinstance(result[0], dict):
            return result[0]
        return {"raw": result}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Executive-summary PDF
# ---------------------------------------------------------------------------

def _render_executive_summary(
    active_candidates: List[Dict[str, Any]],
    rationales: Dict[str, Dict[str, Any]],
    weekly_stats: Dict[str, Any],
    as_of: datetime,
) -> bytes:
    """Render a single-page reportlab PDF. Returns bytes suitable for Storage upload.

    Layout (top-to-bottom):
      - Title + date header
      - Summary stats (signals this week, active candidates, open flags, pending theses)
      - Active candidates table: ticker, state, one-liner, catalyst date
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
        topMargin=0.5 * inch, bottomMargin=0.5 * inch,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "Title", parent=styles["Title"], fontSize=18, spaceAfter=6, textColor=colors.HexColor("#0b3d91"),
    )
    subtitle = ParagraphStyle(
        "Subtitle", parent=styles["Normal"], fontSize=10, spaceAfter=14, textColor=colors.HexColor("#555"),
    )
    body = ParagraphStyle(
        "Body", parent=styles["Normal"], fontSize=9, leading=11,
    )

    story = [
        Paragraph("Conan — Weekly Executive Summary", title_style),
        Paragraph(
            f"As of {as_of.strftime('%Y-%m-%d %H:%M UTC')} · "
            f"v2 pre-edge AI-reviewed candidate pipeline",
            subtitle,
        ),
    ]

    # --- Summary bar.
    stat_rows = [
        ["Signals (7d)", str(weekly_stats.get("signals_7d", 0))],
        ["Active candidates", str(len([c for c in active_candidates if c["state"] == "active"]))],
        ["Watchlist candidates", str(len([c for c in active_candidates if c["state"] == "watch"]))],
        ["Open operator_flags", str(weekly_stats.get("open_flags", 0))],
        ["Pending thesis_jobs", str(weekly_stats.get("pending_theses", 0))],
    ]
    stat_table = Table(stat_rows, colWidths=[2.0 * inch, 1.0 * inch])
    stat_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f0f3fb")),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.black),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#c0c6d8")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#dfe3ec")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("PADDING", (0, 0), (-1, -1), 4),
    ]))
    story.extend([stat_table, Spacer(1, 14)])

    # --- Active + watch candidates table.
    story.append(Paragraph("<b>Candidates</b>", styles["Heading3"]))
    header = ["Ticker", "State", "Catalyst", "One-liner"]
    rows: List[List[Any]] = [header]
    for c in sorted(active_candidates, key=_sort_key):
        r = rationales.get(c["ticker"], {})
        one_liner = (r.get("one_liner") or "").strip() or "—"
        if len(one_liner) > 260:
            one_liner = one_liner[:258] + "…"
        catalyst = c.get("next_catalyst_date") or (r.get("catalyst_date_iso") or "—")
        rows.append([
            f"{c['ticker']}.{c.get('mic') or '?'}",
            c["state"].upper(),
            str(catalyst),
            Paragraph(_escape(one_liner), body),
        ])
    cand_table = Table(
        rows,
        colWidths=[0.9 * inch, 0.7 * inch, 0.9 * inch, 5.0 * inch],
        repeatRows=1,
    )
    cand_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b3d91")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("FONTSIZE", (0, 1), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cfd4df")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("PADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(cand_table)
    story.append(Spacer(1, 10))

    # Footer.
    footer = ParagraphStyle("Footer", parent=styles["Normal"], fontSize=7, textColor=colors.HexColor("#888"))
    story.append(Paragraph(
        "Generated by reporting_weekly Modal function · pre-edge gated · "
        "integrity sweep results in operator_flags",
        footer,
    ))

    doc.build(story)
    return buf.getvalue()


def _sort_key(c: Dict[str, Any]) -> tuple:
    # Sort active before watch, then by catalyst_date ASC (nulls last).
    state_rank = 0 if c["state"] == "active" else 1
    d = c.get("next_catalyst_date")
    return (state_rank, d if d else "9999-12-31", c["ticker"])


def _escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def reporting_weekly(client: Optional[SupabaseClient] = None) -> Dict[str, Any]:
    """Main entry. Runs integrity sweep + renders + uploads the weekly PDF.

    Returns a summary dict suitable for Modal return + logging.
    """
    client = client or SupabaseClient()
    now = datetime.now(timezone.utc)

    # 1. Integrity sweep (best-effort; don't let it block rendering).
    sweep = integrity_sweep(client)

    # 2. Load candidates + rationales + weekly stats.
    candidates = _load_active_candidates(client)
    rationales = _load_rationales(client)
    weekly_stats = _weekly_stats(client)

    # 3. Render PDF.
    pdf_bytes = _render_executive_summary(candidates, rationales, weekly_stats, now)

    # 4. Upload to Storage.
    yyyy = now.strftime("%Y")
    mm = now.strftime("%m")
    date_str = now.strftime("%Y-%m-%d")
    path = f"{yyyy}/{mm}/{date_str}_executive_summary.pdf"
    try:
        _upload_report(client, path, pdf_bytes)
    except Exception as e:  # noqa: BLE001
        return {
            "scanner": NAME, "status": "error", "sweep": sweep,
            "error": f"storage upload failed: {type(e).__name__}: {e}",
            "pdf_bytes": len(pdf_bytes),
        }

    return {
        "scanner": NAME,
        "status": "ok",
        "sweep": sweep,
        "candidate_count": len(candidates),
        "pdf_bytes": len(pdf_bytes),
        "storage_path": f"reports/{path}",
        "generated_at": now.isoformat(),
    }


# ---------------------------------------------------------------------------
# Internal helpers (thin wrappers on SupabaseClient._rest)
# ---------------------------------------------------------------------------

def _load_active_candidates(client: SupabaseClient) -> List[Dict[str, Any]]:
    rows = client._rest(
        "GET", "candidates",
        params={
            "select": "id,ticker,mic,state,current_score,current_band,"
                      "next_catalyst_date,next_catalyst_window,last_aging_evaluated_at",
            "state": "in.(active,watch)",
            "order": "current_score.desc,ticker.asc",
            "limit": "200",
        },
    )
    return rows or []


def _load_rationales(client: SupabaseClient) -> Dict[str, Dict[str, Any]]:
    rows = client._rest(
        "GET", "candidate_rationales",
        params={"select": "ticker,one_liner,thesis,kill_watch,catalyst_date_iso,archived",
                "archived": "eq.false", "limit": "500"},
    )
    return {r["ticker"]: r for r in (rows or [])}


def _weekly_stats(client: SupabaseClient) -> Dict[str, Any]:
    stats: Dict[str, Any] = {}
    seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    # Signals in the last 7 days. Use count=exact prefer header via _rest's params.
    try:
        rows = client._rest(
            "GET", "signals",
            params={"select": "signal_id", "scan_date": f"gte.{seven_days_ago}", "limit": "10000"},
        )
        stats["signals_7d"] = len(rows or [])
    except Exception:  # noqa: BLE001
        stats["signals_7d"] = 0
    # Open operator_flags.
    try:
        rows = client._rest(
            "GET", "operator_flags",
            params={"select": "id", "resolved_at": "is.null", "limit": "10000"},
        )
        stats["open_flags"] = len(rows or [])
    except Exception:  # noqa: BLE001
        stats["open_flags"] = 0
    # Pending thesis_jobs (queued or gate_failed_retrying).
    try:
        rows = client._rest(
            "GET", "thesis_jobs",
            params={"select": "id", "status": "in.(queued,gate_failed_retrying)", "limit": "1000"},
        )
        stats["pending_theses"] = len(rows or [])
    except Exception:  # noqa: BLE001
        stats["pending_theses"] = 0
    return stats


def _upload_report(client: SupabaseClient, path: str, body: bytes) -> None:
    """PUT the PDF into the reports bucket via Storage REST."""
    url = f"{client.url}/storage/v1/object/reports/{path.lstrip('/')}"
    r = client._session.put(
        url, data=body, timeout=client.timeout,
        headers={"Content-Type": "application/pdf", "x-upsert": "true"},
    )
    if r.status_code >= 400:
        from modal_workers.shared.supabase_client import SupabaseError
        raise SupabaseError(r.status_code, r.text)
