"""label_forward_returns — D-116 (2026-05-07) forward-return labeling helper.

Computes signed forward returns at the per-profile windows specified by the
Investment_engine_v2 export's ``methodology_spec.md §forward-return-windows``:

  - Windows: T+30, T+60, T+90, T+180, T+360 calendar days from event filing.
  - binary_catalyst HIT/MISS: absolute return at T+30, threshold +20%.
  - activist_governance HIT/MISS: SPY-relative return at T+180, threshold +15%.

T (anchor) = last trading-day close STRICTLY BEFORE the event's ``filed_at``
(no look-ahead; the close at the moment of public disclosure is part of the
post-event window). Forward closes are the FIRST trading-day close at or
after each ``T + N calendar-day`` target.

Edge cases (per export methodology):
  - Ticker delisted before window completes → return = -100.0 (involuntary
    total loss).
  - yfinance has no data for the window → window flagged INVALIDATED, label
    cannot be computed.
  - Anchor not resolvable (event predates available history) → label
    cannot be computed.
  - M&A close inside the window → handled via deal-terms by an upstream
    pass; this helper only computes price-based returns and flags
    M&A_OUTCOME_PENDING when target_close is missing but anchor was found.

Public API:
  ``label_event(ticker, filed_at, profile, *, prefetch_closes=None,
                spy_closes=None) -> ForwardReturnLabel``

CLI:
  ``python -m modal_workers.scripts.label_forward_returns
       --events path/to/binary_catalyst.json
       --profile binary_catalyst
       --output  out.json
       [--limit N] [--dry-run]``

The CLI consumes the export's events-ledger JSON shape (top-level "events" or
"_meta", per-event keys ``event_id, ticker, filed_at, _profile``) and emits an
output ledger of ``ForwardReturnLabel`` rows ready for D-109's eval_harness
seed script.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from modal_workers.scripts.backfill_realized_move import (
    fetch_daily_closes,
    date_from_ms,
    find_anchor_close,
    find_first_close_at_or_after,
    compute_move_pct,
)

logger = logging.getLogger(__name__)

WINDOWS_DAYS: Tuple[int, ...] = (30, 60, 90, 180, 360)
SPY_TICKER = "SPY"
PRICE_MOVE_LABEL_RULE = "forward_return_t30_calendar"

# HIT thresholds per export methodology_spec.md.
BINARY_CATALYST_HIT_PCT = 20.0  # absolute return at T+30
ACTIVIST_GOVERNANCE_HIT_PCT = 15.0  # SPY-relative return at T+180


# ---------------------------------------------------------------------------
# Output shape
# ---------------------------------------------------------------------------

@dataclass
class WindowReturn:
    days: int
    target_date: Optional[str] = None        # ISO date of resolved trading day
    return_pct: Optional[float] = None        # signed % move; -100.0 = delist
    spy_return_pct: Optional[float] = None    # set only for profile='activist_governance'
    relative_pct: Optional[float] = None      # return_pct - spy_return_pct (activist only)
    status: str = "ok"                        # 'ok' | 'invalidated' | 'delisted' | 'no_anchor'

    def as_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None or k in ("status", "days")}


@dataclass
class ForwardReturnLabel:
    event_id: Optional[str]
    ticker: str
    filed_at: str
    profile: str
    anchor_date: Optional[str] = None
    anchor_close: Optional[float] = None
    windows: List[WindowReturn] = field(default_factory=list)
    hit_window_days: Optional[int] = None     # which window the HIT/MISS verdict reads from
    hit: Optional[bool] = None                # True/False/None — None = cannot be evaluated
    miss_reason: Optional[str] = None         # populated when hit=False or None
    label_rule: str = PRICE_MOVE_LABEL_RULE
    label_method: str = "yfinance_v0.1"
    labeled_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def as_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["windows"] = [w if isinstance(w, dict) else WindowReturn(**w).as_dict()
                          for w in [w_dict if isinstance(w_dict, dict) else w_dict.as_dict()
                                    for w_dict in self.windows]]
        return out


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _parse_filed_at(filed_at: str) -> date:
    """Accept ISO date or ISO 8601 datetime; return a calendar date."""
    if not filed_at:
        raise ValueError("filed_at is empty")
    s = filed_at.strip()
    if "T" in s:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    return date.fromisoformat(s[:10])


def _window_end(filed: date) -> date:
    """Last calendar day we need price data for. T + 360 days + a small slack."""
    return filed + timedelta(days=max(WINDOWS_DAYS) + 14)


def _window_start(filed: date) -> date:
    """Earliest calendar day for the anchor lookup. T - 21 days covers
    most stock-exchange holiday clusters around an event date."""
    return filed - timedelta(days=21)


# ---------------------------------------------------------------------------
# Core labeling
# ---------------------------------------------------------------------------

def label_event(
    ticker: str,
    filed_at: str,
    profile: str,
    *,
    prefetch_closes: Optional[List[Dict[str, Any]]] = None,
    spy_closes: Optional[List[Dict[str, Any]]] = None,
    event_id: Optional[str] = None,
) -> ForwardReturnLabel:
    """Compute forward returns + HIT/MISS for a single event.

    Parameters
    ----------
    ticker : str
        Public ticker. Must be resolvable on Polygon or yfinance.
    filed_at : str
        ISO date or ISO 8601 datetime of the event filing.
    profile : str
        'binary_catalyst' or 'activist_governance'. Determines HIT window
        and threshold.
    prefetch_closes, spy_closes : optional
        Pre-fetched daily aggregates (Polygon shape: {t (ms), c, ...}). Use
        when batch-labeling a list of events that share a ticker/SPY span,
        to avoid redundant API calls.
    event_id : optional
        Carried through to the output for downstream join with the source
        ledger.
    """
    out = ForwardReturnLabel(
        event_id=event_id, ticker=ticker, filed_at=filed_at, profile=profile,
    )

    try:
        filed = _parse_filed_at(filed_at)
    except ValueError as exc:
        out.miss_reason = f"unparseable_filed_at:{exc}"
        return out

    if profile not in ("binary_catalyst", "activist_governance"):
        out.miss_reason = f"unsupported_profile:{profile}"
        return out

    # 1. Fetch ticker closes spanning anchor pre-window through last forward window.
    closes = prefetch_closes if prefetch_closes is not None else fetch_daily_closes(
        ticker, window_start=_window_start(filed), window_end=_window_end(filed),
    )
    if not closes:
        out.miss_reason = "no_price_data"
        for n in WINDOWS_DAYS:
            out.windows.append(WindowReturn(days=n, status="invalidated"))
        return out

    # 2. Resolve anchor (last close strictly before filed).
    anchor = find_anchor_close(closes, filed)
    if not anchor:
        out.miss_reason = "no_anchor"
        for n in WINDOWS_DAYS:
            out.windows.append(WindowReturn(days=n, status="no_anchor"))
        return out

    out.anchor_date = date_from_ms(anchor["t"]).isoformat()
    out.anchor_close = float(anchor["c"])

    # 3. SPY closes only if needed (activist_governance).
    spy = None
    if profile == "activist_governance":
        spy = spy_closes if spy_closes is not None else fetch_daily_closes(
            SPY_TICKER, window_start=_window_start(filed), window_end=_window_end(filed),
        )
        if not spy:
            # Without SPY we cannot compute relative return; downgrade to
            # absolute-only label and continue (better than full invalidation).
            logger.warning("label_forward_returns: SPY data unavailable for %s @ %s; "
                           "absolute returns only", ticker, filed_at)
        else:
            spy_anchor = find_anchor_close(spy, filed)
            if not spy_anchor:
                spy = None  # SPY anchor missing — same fallback as above

    spy_anchor_close = float(spy_anchor["c"]) if (spy and spy_anchor) else None

    # 4. Per-window resolution.
    last_close_date = date_from_ms(closes[-1]["t"])
    for n in WINDOWS_DAYS:
        target = filed + timedelta(days=n)
        wr = WindowReturn(days=n)

        # Window past available history — only invalidate if the LAST trading
        # day in the fetched window is itself before the target. This catches
        # halted/delisted tickers cleanly.
        if last_close_date < target:
            # Distinguish delisted (no trading at all near target) from
            # incomplete coverage. The export rule: involuntary delist within
            # window → -100%. Here we cannot tell intent reliably from prices
            # alone, so flag invalidated; an upstream pass classifies delist
            # vs incomplete using SEC filings.
            wr.status = "invalidated"
            wr.target_date = None
            out.windows.append(wr)
            continue

        post = find_first_close_at_or_after(closes, target)
        if not post:
            wr.status = "invalidated"
            out.windows.append(wr)
            continue

        wr.target_date = date_from_ms(post["t"]).isoformat()
        wr.return_pct = compute_move_pct(out.anchor_close, float(post["c"]))

        if spy_anchor_close is not None:
            spy_post = find_first_close_at_or_after(spy, target)
            if spy_post:
                wr.spy_return_pct = compute_move_pct(spy_anchor_close, float(spy_post["c"]))
                wr.relative_pct = round(wr.return_pct - wr.spy_return_pct, 4)

        out.windows.append(wr)

    # 5. HIT/MISS classification per profile.
    if profile == "binary_catalyst":
        out.hit_window_days = 30
        w30 = next((w for w in out.windows if w.days == 30 and w.status == "ok"), None)
        if w30 is None or w30.return_pct is None:
            out.hit = None
            out.miss_reason = "t30_invalidated"
        else:
            out.hit = w30.return_pct >= BINARY_CATALYST_HIT_PCT
            if not out.hit:
                out.miss_reason = f"t30_return_pct={w30.return_pct:+.2f}_below_+{BINARY_CATALYST_HIT_PCT:.0f}"

    elif profile == "activist_governance":
        out.hit_window_days = 180
        w180 = next((w for w in out.windows if w.days == 180 and w.status == "ok"), None)
        if w180 is None:
            out.hit = None
            out.miss_reason = "t180_invalidated"
        elif w180.relative_pct is None:
            out.hit = None
            out.miss_reason = "t180_no_spy_relative"
        else:
            out.hit = w180.relative_pct >= ACTIVIST_GOVERNANCE_HIT_PCT
            if not out.hit:
                out.miss_reason = (f"t180_relative_pct={w180.relative_pct:+.2f}_"
                                   f"below_+{ACTIVIST_GOVERNANCE_HIT_PCT:.0f}")

    return out


# ---------------------------------------------------------------------------
# Batch labeling (CLI)
# ---------------------------------------------------------------------------

def _load_events(path: Path) -> List[Dict[str, Any]]:
    """Accept the export's binary_catalyst.json shape: either a top-level list
    or {events: [...], _meta: {...}}."""
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "events" in data:
        return data["events"]
    raise ValueError(
        f"{path}: expected a top-level list or {{events: [...]}} — got {type(data).__name__}"
    )


def _serializable_label(label: ForwardReturnLabel) -> Dict[str, Any]:
    return {
        "event_id": label.event_id,
        "ticker": label.ticker,
        "filed_at": label.filed_at,
        "profile": label.profile,
        "anchor_date": label.anchor_date,
        "anchor_close": label.anchor_close,
        "windows": [w.as_dict() for w in label.windows],
        "hit_window_days": label.hit_window_days,
        "hit": label.hit,
        "miss_reason": label.miss_reason,
        "label_rule": label.label_rule,
        "label_method": label.label_method,
        "labeled_at": label.labeled_at,
    }


def label_ledger(
    events: Iterable[Dict[str, Any]],
    profile: str,
    *,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Label every event in the ledger and return a list of serialized labels."""
    out: List[Dict[str, Any]] = []
    for i, ev in enumerate(events):
        if limit is not None and i >= limit:
            break
        ticker = ev.get("ticker") or ev.get("ticker_local")
        filed_at = ev.get("filed_at") or ev.get("filing_date")
        ev_id = ev.get("event_id") or ev.get("id")
        if not ticker or not filed_at:
            out.append({
                "event_id": ev_id, "ticker": ticker, "filed_at": filed_at,
                "profile": profile, "hit": None,
                "miss_reason": "missing_ticker_or_filed_at",
            })
            continue
        ticker_str = str(ticker).strip()
        if ticker_str in ("?", "PRIVATE_DISCARD", "UNRESOLVABLE", ""):
            out.append({
                "event_id": ev_id, "ticker": ticker_str, "filed_at": filed_at,
                "profile": profile, "hit": None,
                "miss_reason": f"unresolved_ticker_sentinel:{ticker_str}",
            })
            continue
        label = label_event(ticker_str, str(filed_at), profile, event_id=ev_id)
        out.append(_serializable_label(label))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="label_forward_returns",
        description="D-116 forward-return labeling helper "
                    "(unblocks D-109 eval_harness seed).",
    )
    p.add_argument("--events", type=Path, required=True,
                   help="Path to the export's binary_catalyst.json or a list of events.")
    p.add_argument("--profile", choices=("binary_catalyst", "activist_governance"),
                   required=True)
    p.add_argument("--output", type=Path, required=True,
                   help="Path to write the labeled ledger.")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap on number of events labeled (smoke-test friendly).")
    p.add_argument("--dry-run", action="store_true",
                   help="Compute summary stats but do not write the output file.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    events = _load_events(args.events)
    logger.info("loaded %d events from %s", len(events), args.events)

    labels = label_ledger(events, args.profile, limit=args.limit)

    n = len(labels)
    n_hit = sum(1 for r in labels if r.get("hit") is True)
    n_miss = sum(1 for r in labels if r.get("hit") is False)
    n_invalid = sum(1 for r in labels if r.get("hit") is None)
    logger.info("labeled %d / hit=%d / miss=%d / invalid=%d",
                n, n_hit, n_miss, n_invalid)

    if args.dry_run:
        logger.info("dry-run: skipping write to %s", args.output)
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        json.dump({
            "profile": args.profile,
            "source": str(args.events),
            "n_events_input": len(events),
            "n_events_labeled": n,
            "n_hit": n_hit,
            "n_miss": n_miss,
            "n_invalid": n_invalid,
            "labeled_at": datetime.now(timezone.utc).isoformat(),
            "labels": labels,
        }, f, indent=2, sort_keys=True)
    logger.info("wrote %s", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
