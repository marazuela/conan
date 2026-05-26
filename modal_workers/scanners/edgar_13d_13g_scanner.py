"""edgar_13d_13g_scanner — shareholder structure signals for FDA-tracked assets.

Phase 4 of the v4 architecture simplification (~/.claude/plans/proud-booping-seal.md).
Companion to the Form 4 binary_catalyst reroute (insider_form4_scanner.py).

What this scans:
  EDGAR Schedule 13D / 13D/A / 13G / 13G/A filings in the trailing window.
  Schedule 13D: filed by anyone acquiring >5% of a company's stock WITH
    intent to influence (activist threshold).
  Schedule 13G: filed by passive >5% holders (investment companies, banks
    with no influence intent).
  /A suffixes are amendments — typically incremental position changes.

Why it matters for v4:
  Insider activity (Form 4) and shareholder structure (13D/13G) are two
  signal categories that Pedro's target architecture wants feeding the
  binary_catalyst rubric. Form 4 was already scanned but routed to
  short_positioning; the Phase 4 reroute fixed that. 13D/13G had no
  scanner at all. This file closes that gap.

Routing:
  Like the Form 4 reroute, this scanner only emits when the subject
  issuer's ticker matches a row in `fda_assets`. Filings on non-tracked
  issuers are skipped — there are thousands of 13D/13G filings per
  month across all US equities; tracking the whole tape is wasted work
  for a biotech-focused thesis system.

Scoring:
  Emits UNSCORED binary_catalyst signals (no dimensions). The downstream
  scoring path (Phase 5 rubric + AI resolver) fills in the dims. Same
  pattern as fda_event signals — see fda_signals_unscored_by_design.md.

Signal types:
  shareholder_13d_filing       — new SC 13D (activist threshold crossed)
  shareholder_13d_amendment    — SC 13D/A (existing activist updates stake)
  shareholder_13g_filing       — new SC 13G (passive ≥5% holder)
  shareholder_13g_amendment    — SC 13G/A (passive holder updates stake)

MVP scope: parses EFTS metadata only (form type + subject CIK + accession +
filing date + reporter name). Does NOT parse the document body to extract
percent_of_class. Phase 4b can add doc-body parsing when there's demand —
for now, "a 13D was filed on our tracked asset" is enough signal to alert
the orchestrator.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from modal_workers.scanners.edgar_filing_monitor import (
    _efts_search,
    _rate_limiter,
)
from modal_workers.shared.scanner_base import MissingAuthError, ScannerResult, Signal
from modal_workers.shared.supabase_client import (
    EntityHints,
    ScannerConfig,
    SupabaseClient,
)

NAME = "edgar_13d_13g_scanner"

DEFAULT_WALL_CLOCK_S = 90
REQUEST_TIMEOUT = 10

# SC 13D = >5% with intent to influence; SC 13G = >5% passive.
# /A = amendment. Per SEC, both initial and amendments are material.
FORM_TYPES = "SC 13D,SC 13D/A,SC 13G,SC 13G/A"

# EDGAR EFTS returns hits ordered by file_date desc. 7-day lookback is enough
# to catch filings on subsequent scans (typical scanner cadence is hourly or
# 6x daily), and short enough to keep the per-run hit count under 500.
LOOKBACK_DAYS = 7

# Hard cap on filings processed per run. Most days see ~50-150 13D/13G
# filings across US equities; 500 is generous headroom.
MAX_FILINGS_PER_RUN = 500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_fda_tracked_tickers(client: SupabaseClient) -> set[str]:
    """Mirror of insider_form4_scanner._load_fda_tracked_tickers — keeps the
    two scanners' routing logic visually parallel. Query failure → empty set
    → scanner emits no signals (safe degraded mode)."""
    try:
        rows = client._rest(
            "GET",
            "fda_assets",
            params={
                "select": "ticker",
                "is_active": "eq.true",
                "ticker": "not.is.null",
            },
        ) or []
    except Exception:  # noqa: BLE001
        return set()
    return {
        (r.get("ticker") or "").strip().upper()
        for r in rows
        if r.get("ticker")
    }


def _signal_type_for_form(form_type: str) -> str:
    """Map EFTS form_type string to our signal_type enum.

    EFTS returns the form type with whitespace exactly as the SEC encodes
    it (e.g. "SC 13D"). Amendment variants carry the "/A" suffix.
    """
    f = (form_type or "").upper().replace(" ", "")
    if f == "SC13D":
        return "shareholder_13d_filing"
    if f == "SC13D/A":
        return "shareholder_13d_amendment"
    if f == "SC13G":
        return "shareholder_13g_filing"
    if f == "SC13G/A":
        return "shareholder_13g_amendment"
    return "shareholder_unknown"


def _strength_for_signal_type(signal_type: str) -> int:
    """Default strength assignment — 13D > 13G (intent matters), filings >
    amendments (initial threshold crossing is more material than an
    update). Phase 4b can refine this by parsing percent_of_class deltas."""
    return {
        "shareholder_13d_filing": 5,       # activist position established
        "shareholder_13d_amendment": 4,    # activist updates stake
        "shareholder_13g_filing": 4,       # large passive position established
        "shareholder_13g_amendment": 3,    # passive position updated
    }.get(signal_type, 2)


def _content_hash(adsh: str, subject_cik: str, form_type: str) -> str:
    """Dedup key: an accession + subject + form_type tuple is unique."""
    blob = f"{adsh}|{subject_cik}|{form_type}".encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _signal_id(adsh: str, subject_cik: str, form_type: str) -> str:
    """Stable signal_id derived from the same tuple — the scanner is purely
    additive (every accession is its own filing), so signal_id = content_hash
    keeps the math simple."""
    return _content_hash(adsh, subject_cik, form_type)


def _resolve_ticker_for_cik(cik: str, *, user_agent: str) -> Optional[str]:
    """Resolve issuer CIK → primary ticker via SEC's company_tickers endpoint.

    Returns None when the CIK isn't a publicly-traded issuer (some 13D/13G
    subjects are private or recently delisted) or when the lookup fails.
    """
    try:
        from modal_workers.scanners.edgar_filing_monitor import _get_company_tickers
        tickers, _exchange = _get_company_tickers(cik, user_agent=user_agent)
        return tickers[0] if tickers else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# scan entrypoint
# ---------------------------------------------------------------------------


def scan(cfg: ScannerConfig) -> ScannerResult:
    user_agent = os.environ.get("SEC_USER_AGENT")
    if not user_agent:
        raise MissingAuthError(
            "SEC_USER_AGENT env var missing — SEC requires a valid contact "
            "email in the User-Agent header. Set via Modal secret "
            "`scanner-secrets`."
        )

    client = SupabaseClient()
    fda_tracked_tickers = _load_fda_tracked_tickers(client)
    if not fda_tracked_tickers:
        # Degraded mode: no tracked assets to match against. Return ok-empty
        # rather than partial — this is "nothing to emit", not "something
        # broke". The metric below makes the empty-set case visible.
        return ScannerResult(
            scanner=NAME,
            status="ok",
            signals=[],
            warnings=["no fda_assets to match — emitting nothing"],
            fetched_records=0,
            run_metrics={
                "fda_tracked_tickers_loaded": 0,
                "filings_listed": 0,
                "filings_on_tracked_assets": 0,
                "signals_emitted": 0,
            },
        )

    budget_s = max(20, (cfg.timeout_soft_s or DEFAULT_WALL_CLOCK_S) - 10)
    scan_start = time.time()
    scan_date = datetime.now(timezone.utc)
    date_from = (scan_date - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    date_to = scan_date.strftime("%Y-%m-%d")

    warnings: List[str] = []
    budget_exhausted = False

    # ---- 1. List 13D/13G filings in window ---------------------------------
    try:
        hits = _efts_search(
            query="the",  # EFTS requires non-empty q; forms filter is the real selector.
            date_from=date_from,
            date_to=date_to,
            form_type=FORM_TYPES,
            max_results=MAX_FILINGS_PER_RUN,
            user_agent=user_agent,
        )
    except Exception as e:  # noqa: BLE001
        warnings.append(f"13d/13g efts list: {type(e).__name__}: {e}")
        hits = []
    fetched = len(hits)

    # ---- 2. Filter to tracked assets + emit one signal per filing ----------
    signals: List[Signal] = []
    filings_on_tracked = 0
    skipped_private_or_unresolved = 0
    skipped_dup_hash: set[str] = set()

    for hit in hits:
        if time.time() - scan_start > budget_s * 0.85:
            budget_exhausted = True
            warnings.append("13d/13g processing: soft budget reached")
            break

        ciks = hit.get("ciks") or hit.get("cik") or []
        if isinstance(ciks, str):
            ciks = [ciks]
        # Subject CIK is the first; reporter CIKs come after. We route on the
        # subject (the company being filed against), not the reporter.
        subject_cik = ciks[0] if ciks else hit.get("cik", "")
        if not subject_cik:
            continue

        ticker = _resolve_ticker_for_cik(subject_cik, user_agent=user_agent)
        if not ticker:
            skipped_private_or_unresolved += 1
            continue
        if ticker.upper() not in fda_tracked_tickers:
            continue

        filings_on_tracked += 1
        adsh = hit.get("adsh") or ""
        form_type = hit.get("form") or ""
        file_date = hit.get("file_date") or ""
        if not adsh:
            continue

        # Dedup within this run (defensive — EFTS shouldn't return duplicates).
        src_content_hash = _content_hash(adsh, subject_cik, form_type)
        if src_content_hash in skipped_dup_hash:
            continue
        skipped_dup_hash.add(src_content_hash)

        signal_type = _signal_type_for_form(form_type)
        strength = _strength_for_signal_type(signal_type)

        try:
            source_date = datetime.strptime(file_date, "%Y-%m-%d").replace(
                tzinfo=timezone.utc,
            )
        except ValueError:
            source_date = scan_date

        # Build the EDGAR filing index URL — what the dashboard links to.
        cs = subject_cik.lstrip("0") or "0"
        ac = adsh.replace("-", "")
        source_url = f"https://www.sec.gov/Archives/edgar/data/{cs}/{ac}"

        display_names = hit.get("display_names") or []
        subject_name = display_names[0] if display_names else None
        reporter_names = display_names[1:] if len(display_names) > 1 else []

        raw_payload: Dict[str, Any] = {
            "signal_category": "shareholder_structure",
            "form_type": form_type,
            "accession": adsh,
            "subject_cik": subject_cik,
            "subject_name": subject_name,
            "subject_ticker": ticker.upper(),
            "reporter_names": reporter_names,
            "file_date": file_date,
            # Phase 4b enrichment will populate these from doc-body parse:
            "percent_of_class": None,
            "percent_of_class_change": None,
            "regulators": ["SEC"],
        }

        entity_hints = EntityHints(
            ticker=ticker.upper(),
            mic=None,
            cik=subject_cik,
            name=subject_name,
            country="US",
        )

        signals.append(Signal(
            signal_id=_signal_id(adsh, subject_cik, form_type),
            source_content_hash=src_content_hash,
            source_date=source_date,
            scan_date=scan_date,
            signal_type=signal_type,
            raw_payload=raw_payload,
            source_url=source_url,
            entity_hints=entity_hints,
            # 13D filings carry directional intent (activist push for change),
            # but 13G passive holders don't. Conservative default: neutral.
            # Stage 1 + scoring layer interpret based on form_type + payload.
            thesis_direction="neutral",
            strength_estimate=strength,
            scoring_profile="binary_catalyst",
            extensions={
                "signal_category": "shareholder_structure",
            },
        ))

    status = "partial" if (budget_exhausted or warnings) else "ok"

    return ScannerResult(
        scanner=NAME,
        status=status,
        signals=signals,
        warnings=warnings,
        fetched_records=fetched,
        run_metrics={
            "fda_tracked_tickers_loaded": len(fda_tracked_tickers),
            "filings_listed": fetched,
            "filings_on_tracked_assets": filings_on_tracked,
            "filings_skipped_unresolved_ticker": skipped_private_or_unresolved,
            "signals_emitted": len(signals),
        },
    )
