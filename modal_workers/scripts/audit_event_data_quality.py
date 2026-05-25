"""WI-5 — Q1 confounder + coverage audit for eval_harness rows.

Port of v2_skills/assess-event-data-quality. Each eval_harness row gets one
of three verdicts:

  clean       — no confounder triggered, no coverage gap, tradeable filter passed
  confounded  — confounder triggered, coverage OK (still usable, but flagged)
  discard     — coverage failure OR tradeable_filter_pass=false (drop from cohort)

The audit writes back to eval_harness.q1_verdict/q1_reasons/q1_confounders/
q1_coverage/q1_audited_at. Q2 (audit_sample_balance.py) reads q1_verdict='clean'
to compute Herfindahl indices, and that pass is what gates curve promotion in
nightly_calibration_refit.

Confounder checks
-----------------
  earnings_within_5td        — same-ticker earnings ±5 trading days
  fomc_day                   — ref_date in fomc_calendar.fomc_date ±1 cal day
  spx_3sigma_during_window   — SPY |daily return| > 3σ in T+0..T+30
  material_8k_in_window      — naive: any 8-K for same ticker in T+0..T+30,
                               excluding the source doc itself (refined in
                               Phase 3b enhancement)

Coverage checks
---------------
  yfinance_window_gap        — any window in realized_outcome_data with
                               status='invalidated' on the T+30 window
  low_volume_days_pct        — Polygon volume < 25% of trailing 90td median
                               on >20% of window days (stub until Polygon
                               is wired; emits triggered=false for now)
  pre_window_delisting       — windows[].status='delisted' OR
                               eval_harness.issuer_status='delisted' AND
                               delist_date < ref+30d

Run locally:
    SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \\
    python3 -m modal_workers.scripts.audit_event_data_quality --all --apply
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from modal_workers.shared.supabase_client import SupabaseClient, SupabaseError  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_EARNINGS_WINDOW_TD = 5
DEFAULT_SPX_SIGMA = 3.0
LOW_VOLUME_RATIO = 0.25      # day's volume < 25% of trailing-90td median → low
LOW_VOLUME_DAY_PCT = 0.20    # >20% of window days low → coverage failure
T_PLUS_DAYS = 30             # confounder + coverage window length

Verdict = Literal["clean", "confounded", "discard"]


# ---------------------------------------------------------------------------
# Output shape
# ---------------------------------------------------------------------------


@dataclass
class Q1Verdict:
    verdict: Verdict
    reasons: List[str] = field(default_factory=list)
    confounders: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    coverage: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    audited_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def as_db_row(self) -> Dict[str, Any]:
        return {
            "q1_verdict": self.verdict,
            "q1_reasons": self.reasons,
            "q1_confounders": self.confounders,
            "q1_coverage": self.coverage,
            "q1_audited_at": self.audited_at,
        }


# ---------------------------------------------------------------------------
# Pure verdict assembly — no I/O. Tests drive this directly.
# ---------------------------------------------------------------------------


def assemble_verdict(
    *,
    tradeable_filter_pass: bool,
    confounders: Dict[str, Dict[str, Any]],
    coverage: Dict[str, Dict[str, Any]],
) -> Q1Verdict:
    """Apply the v2 verdict ladder to a set of per-check evidence dicts.

    discard rules fire first (tradeable filter or coverage gap), then
    confounded, then clean.
    """
    reasons: List[str] = []

    if not tradeable_filter_pass:
        reasons.append("tradeable_filter_failed")
        return Q1Verdict(
            verdict="discard", reasons=reasons,
            confounders=confounders, coverage=coverage,
        )

    coverage_triggered = [k for k, v in coverage.items() if v.get("triggered")]
    if coverage_triggered:
        reasons.extend(coverage_triggered)
        return Q1Verdict(
            verdict="discard", reasons=reasons,
            confounders=confounders, coverage=coverage,
        )

    confounder_triggered = [k for k, v in confounders.items() if v.get("triggered")]
    if confounder_triggered:
        return Q1Verdict(
            verdict="confounded", reasons=confounder_triggered,
            confounders=confounders, coverage=coverage,
        )

    return Q1Verdict(
        verdict="clean", reasons=[],
        confounders=confounders, coverage=coverage,
    )


# ---------------------------------------------------------------------------
# Confounder checks — each returns a {"triggered": bool, "evidence": dict}
# ---------------------------------------------------------------------------


def check_earnings_within_window(
    *, ticker: str, ref_date: date,
    earnings_dates: List[date],
    window_td: int = DEFAULT_EARNINGS_WINDOW_TD,
) -> Dict[str, Any]:
    """Earnings ±N trading days. Caller passes pre-filtered earnings_dates
    for the ticker so the helper stays pure (no Supabase coupling).

    Note: the spec calls for *trading* days. We approximate ±5 trading days
    as ±7 calendar days, which is close enough at the audit-flag level
    (false-positive rate ≤2% per the v2 export's calibration).
    """
    floor = ref_date - timedelta(days=window_td * 7 // 5)
    ceil = ref_date + timedelta(days=window_td * 7 // 5)
    hits = [d for d in earnings_dates if floor <= d <= ceil]
    return {
        "triggered": bool(hits),
        "evidence": {
            "ticker": ticker,
            "ref_date": ref_date.isoformat(),
            "window_td": window_td,
            "hits": [d.isoformat() for d in hits],
        },
    }


def check_fomc_day(
    *, ref_date: date, fomc_dates: List[date],
) -> Dict[str, Any]:
    """ref_date in fomc_dates ±1 calendar day. ±1 captures the overnight
    statement-release effect even when ref_date is the day after the meeting.
    """
    floor = ref_date - timedelta(days=1)
    ceil = ref_date + timedelta(days=1)
    hits = [d for d in fomc_dates if floor <= d <= ceil]
    return {
        "triggered": bool(hits),
        "evidence": {
            "ref_date": ref_date.isoformat(),
            "hits": [d.isoformat() for d in hits],
        },
    }


def check_spx_three_sigma(
    *, spy_daily_returns: List[float], threshold_sigma: float = DEFAULT_SPX_SIGMA,
) -> Dict[str, Any]:
    """Any day's |return| > N*σ over the trailing 60d ending at T+0. Inputs
    are the post-event daily SPY returns (T+1..T+30 typically).

    For tests: caller passes a list of decimal returns (e.g. 0.012 for +1.2%).
    """
    if not spy_daily_returns:
        return {"triggered": False, "evidence": {"reason": "no_spy_data"}}
    # σ over the input series itself when the caller didn't pre-compute a
    # trailing window. Audits in production will compute σ from the
    # baseline 60d, but the helper accepts the result directly.
    mean = sum(spy_daily_returns) / len(spy_daily_returns)
    var = sum((r - mean) ** 2 for r in spy_daily_returns) / max(1, len(spy_daily_returns) - 1)
    sigma = math.sqrt(var) if var > 0 else 0.0
    if sigma == 0:
        return {"triggered": False, "evidence": {"reason": "zero_variance"}}
    excess = [r for r in spy_daily_returns if abs(r) > threshold_sigma * sigma]
    return {
        "triggered": bool(excess),
        "evidence": {
            "sigma": round(sigma, 6),
            "threshold_sigma": threshold_sigma,
            "excess_returns": [round(r, 6) for r in excess],
        },
    }


def check_material_8k_in_window(
    *, ticker: str, in_window_8k_count: int, source_doc_excluded: bool = True,
) -> Dict[str, Any]:
    """Naive v1: any 8-K filed for same ticker in T+0..T+30 (excluding the
    source doc). Phase 3b enhancement filters to specific Items (1.01, 2.02,
    8.01) — for now we treat presence as a confounder.
    """
    return {
        "triggered": in_window_8k_count > 0,
        "evidence": {
            "ticker": ticker,
            "count": in_window_8k_count,
            "source_doc_excluded": source_doc_excluded,
        },
    }


# ---------------------------------------------------------------------------
# Coverage checks
# ---------------------------------------------------------------------------


def check_yfinance_window_gap(*, windows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Returns triggered=True when the T+30 window status is 'invalidated' or
    missing — the labeler couldn't price-anchor the event window.

    The downstream HIT/MISS classifier already handles 'invalidated'; here we
    promote it to a coverage-discard so Q2 can exclude the row from the
    Herfindahl computation."""
    t30 = next((w for w in windows if w.get("days") == T_PLUS_DAYS), None)
    if t30 is None:
        return {"triggered": True, "evidence": {"reason": "no_t30_window"}}
    if t30.get("status") in ("invalidated", "no_anchor"):
        return {"triggered": True, "evidence": {"reason": t30["status"]}}
    return {"triggered": False, "evidence": {"t30_status": t30.get("status")}}


def check_low_volume_days_pct(
    *, daily_volumes: Optional[List[float]] = None,
    trailing_90td_median: Optional[float] = None,
) -> Dict[str, Any]:
    """Polygon volume < LOW_VOLUME_RATIO * trailing-90td median on
    >LOW_VOLUME_DAY_PCT of window days. Returns triggered=False when the
    caller hasn't wired Polygon volume (which is the v1 state — see
    coverage_state='polygon_pending').
    """
    if daily_volumes is None or trailing_90td_median is None or trailing_90td_median <= 0:
        return {"triggered": False, "evidence": {"state": "polygon_pending"}}
    threshold = LOW_VOLUME_RATIO * trailing_90td_median
    low_days = sum(1 for v in daily_volumes if v < threshold)
    pct = low_days / max(1, len(daily_volumes))
    return {
        "triggered": pct > LOW_VOLUME_DAY_PCT,
        "evidence": {
            "low_days": low_days,
            "total_days": len(daily_volumes),
            "pct": round(pct, 4),
            "threshold_pct": LOW_VOLUME_DAY_PCT,
        },
    }


def check_pre_window_delisting(
    *, issuer_status: Optional[str], windows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Trigger when any window ≤T+30 carries status='delisted' OR
    eval_harness.issuer_status='delisted'.
    """
    window_delist = any(
        w.get("status") == "delisted" and (w.get("days") or 0) <= T_PLUS_DAYS
        for w in windows
    )
    issuer_delist = issuer_status == "delisted"
    return {
        "triggered": window_delist or issuer_delist,
        "evidence": {
            "window_delisted": window_delist,
            "issuer_status": issuer_status,
        },
    }


# ---------------------------------------------------------------------------
# Top-level audit_event — pulls inputs from DB then assembles the verdict.
# ---------------------------------------------------------------------------


def audit_event(
    sb: SupabaseClient,
    *,
    eval_harness_id: str,
    earnings_window_td: int = DEFAULT_EARNINGS_WINDOW_TD,
    spx_sigma_threshold: float = DEFAULT_SPX_SIGMA,
) -> Q1Verdict:
    """End-to-end audit for one eval_harness row. The fetch helpers below are
    swap-points; tests mock them to drive verdict logic without touching DB.
    """
    row = _load_eval_harness_row(sb, eval_harness_id)
    if not row:
        # No row → emit a discard verdict so the caller doesn't proceed.
        return Q1Verdict(
            verdict="discard",
            reasons=["eval_harness_row_missing"],
        )

    ticker = row.get("ticker")
    ref_date = _parse_iso_date(row.get("reference_assessment_date"))
    if ticker is None or ref_date is None:
        return Q1Verdict(
            verdict="discard",
            reasons=["missing_ticker_or_ref_date"],
        )

    earnings_dates = _load_earnings_dates_for_ticker(sb, ticker)
    fomc_dates = _load_fomc_dates(sb)
    spy_returns = _load_spy_returns_in_window(sb, ref_date)
    in_window_8k_count = _count_in_window_8k(sb, ticker=ticker, ref_date=ref_date)
    windows = _extract_windows_from_realized_outcome(row.get("realized_outcome_data"))

    confounders = {
        "earnings_within_5td": check_earnings_within_window(
            ticker=ticker, ref_date=ref_date, earnings_dates=earnings_dates,
            window_td=earnings_window_td,
        ),
        "fomc_day": check_fomc_day(ref_date=ref_date, fomc_dates=fomc_dates),
        "spx_3sigma_during_window": check_spx_three_sigma(
            spy_daily_returns=spy_returns,
            threshold_sigma=spx_sigma_threshold,
        ),
        "material_8k_in_window": check_material_8k_in_window(
            ticker=ticker, in_window_8k_count=in_window_8k_count,
        ),
    }
    coverage = {
        "yfinance_window_gap": check_yfinance_window_gap(windows=windows),
        "low_volume_days_pct": check_low_volume_days_pct(),
        "pre_window_delisting": check_pre_window_delisting(
            issuer_status=row.get("issuer_status"), windows=windows,
        ),
    }
    return assemble_verdict(
        tradeable_filter_pass=bool(row.get("tradeable_filter_pass")),
        confounders=confounders,
        coverage=coverage,
    )


# ---------------------------------------------------------------------------
# DB fetch helpers — small surface so tests can monkeypatch each one.
# ---------------------------------------------------------------------------


def _load_eval_harness_row(sb: SupabaseClient, eval_harness_id: str) -> Optional[Dict[str, Any]]:
    """Fetch the eval_harness row + ticker (joined from fda_assets)."""
    result = (
        sb.from_("eval_harness")
        .select(
            "id, reference_assessment_date, realized_outcome_data, "
            "tradeable_filter_pass, issuer_status, "
            "fda_assets!inner(ticker)"
        )
        .eq("id", eval_harness_id)
        .maybe_single()
        .execute()
    )
    data = result.data or {}
    if not data:
        return None
    ticker = (data.get("fda_assets") or {}).get("ticker")
    data["ticker"] = ticker
    return data


def _load_earnings_dates_for_ticker(sb: SupabaseClient, ticker: str) -> List[date]:
    result = (
        sb.from_("earnings_calendar")
        .select("earnings_date")
        .eq("ticker", ticker)
        .execute()
    )
    rows = result.data or []
    out: List[date] = []
    for r in rows:
        d = _parse_iso_date(r.get("earnings_date"))
        if d:
            out.append(d)
    return out


def _load_fomc_dates(sb: SupabaseClient) -> List[date]:
    """All FOMC scheduled + emergency dates. Minutes-release dates are
    excluded — they don't move markets enough to count as confounders.
    """
    result = (
        sb.from_("fomc_calendar")
        .select("fomc_date, meeting_type")
        .in_("meeting_type", ["scheduled", "emergency"])
        .execute()
    )
    rows = result.data or []
    out: List[date] = []
    for r in rows:
        d = _parse_iso_date(r.get("fomc_date"))
        if d:
            out.append(d)
    return out


def _load_spy_returns_in_window(
    sb: SupabaseClient, ref_date: date,
) -> List[float]:
    """Stub — Polygon SPY daily returns load goes here. v1 returns []
    (the check then short-circuits to triggered=False / 'no_spy_data').
    """
    return []


def _count_in_window_8k(
    sb: SupabaseClient, *, ticker: str, ref_date: date,
) -> int:
    """Stub — would join asset_documents → documents on edgar 8-K source.
    v1 returns 0 so the confounder doesn't fire spuriously while the
    naive-vs-Items-1.01/2.02/8.01 classifier is being designed.
    """
    return 0


def _extract_windows_from_realized_outcome(realized: Any) -> List[Dict[str, Any]]:
    if not isinstance(realized, dict):
        return []
    windows = realized.get("windows")
    if isinstance(windows, list):
        return windows
    return []


def _parse_iso_date(s: Any) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Writeback + CLI
# ---------------------------------------------------------------------------


def _persist_verdict(sb: SupabaseClient, *, eval_harness_id: str, verdict: Q1Verdict) -> None:
    sb.from_("eval_harness").update(verdict.as_db_row()).eq("id", eval_harness_id).execute()


def _audit_all(sb: SupabaseClient, *, re_audit: bool, apply: bool) -> Dict[str, int]:
    """Audit every eval_harness row that hasn't been audited yet (unless
    --re-audit is set)."""
    query = sb.from_("eval_harness").select("id")
    if not re_audit:
        query = query.is_("q1_audited_at", "null")
    rows = (query.execute().data) or []
    counts = {"clean": 0, "confounded": 0, "discard": 0, "errors": 0}
    for row in rows:
        try:
            verdict = audit_event(sb, eval_harness_id=row["id"])
            if apply:
                _persist_verdict(sb, eval_harness_id=row["id"], verdict=verdict)
            counts[verdict.verdict] += 1
        except (SupabaseError, KeyError, ValueError) as e:
            logger.warning("audit_event %s failed: %s", row.get("id"), e)
            counts["errors"] += 1
    return counts


def _cli() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--asset-id", help="Single asset_id to audit.")
    p.add_argument("--id", help="Single eval_harness id to audit.")
    p.add_argument("--all", action="store_true",
                   help="Audit every eval_harness row.")
    p.add_argument("--re-audit", action="store_true",
                   help="Re-audit rows that already have q1_audited_at.")
    p.add_argument("--apply", action="store_true",
                   help="Persist verdicts. Default is dry-run.")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    sb = SupabaseClient()
    if args.all:
        counts = _audit_all(sb, re_audit=args.re_audit, apply=args.apply)
        print(f"[Q1 audit] counts: {counts}")
        return 0
    target_id = args.id
    if not target_id:
        raise SystemExit("must pass --id <eval_harness_id> or --all")
    verdict = audit_event(sb, eval_harness_id=target_id)
    print(f"[Q1 audit] {target_id}: {verdict.verdict}; reasons={verdict.reasons}")
    if args.apply:
        _persist_verdict(sb, eval_harness_id=target_id, verdict=verdict)
        print(f"[Q1 audit] persisted q1_verdict={verdict.verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
