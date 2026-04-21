"""
dim_estimator — heuristic dimension estimation from scanner `raw_payload`.

Background
----------
Scanners emit signals describing *that* something happened (a short position
changed, a filing mentioned a keyword, a PDUFA is approaching). Scoring
requires per-dimension ratings on a 1-5 scale. Historically every scanner
skipped `raw_payload["dimensions"]`, the rubric engine defaulted every dim
to 3, and every signal scored exactly 30 (every profile's weights sum to 10).

As of 2026-04-21 the rubric engine returns `score=None, band=None` when
dimensions are missing (see rubric_engine.score_signal). This module bridges
the gap: at signal-ingest time, `scanner_base._signal_to_row` calls
`estimate_dimensions(profile, raw_payload)` to produce a best-effort dim map
plus metadata describing which dimensions were truly supported by the scanner
payload and which were neutral fallbacks.

Design
------
- Scanners are *detectors*, not analysts. They know their source domain
  (EDGAR filings, ESMA short positions, PDUFA calendar) but not analyst-
  level context like "information_asymmetry" or "competitive_landscape".
- For each profile we implement a heuristic that:
    * confidently scores dims the scanner payload actually supports
      (e.g. crowding_intensity from ESMA total_disclosed_pct)
    * uses a conservative 3 for dims the payload can't support
    * marks rows with defaulted dims as `requires_resolution`, so ingest can
      persist them as provisional rather than pretending they are final
    * returns `None` for profiles where almost nothing is estimable from
      detector output alone (activist_governance, merger_arb, litigation).
      Signals under those profiles land in the DB unscored; AI review /
      human analyst fills dims in later.
- A scanner that already populates `raw_payload["dimensions"]` bypasses this
  module entirely — its values are respected as-is.

Graduation rule
---------------
When a profile moves from "returns None" to "produces estimates", add:
  1. An estimator function here.
  2. Unit tests in tests/test_dim_estimator.py covering full/partial/
     no-evidence paths.
  3. A note in the docstring explaining which raw_payload keys drive which
     dim, so scanner authors know what fields to preserve.
"""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Dict, Optional

from modal_workers.shared.rubric_engine import (
    WEIGHTS,
    build_scoring_meta,
    dimensions_with_provenance,
)


@dataclass(frozen=True)
class DimensionEstimate:
    dimensions: Dict[str, int]
    supported_dims: list[str]
    defaulted_dims: list[str]
    requires_resolution: bool

    def with_provenance(self, provenance: str = "heuristic") -> Dict[str, Any]:
        return dimensions_with_provenance(self.dimensions, provenance)

    def scoring_meta(self, provenance: str = "heuristic") -> Dict[str, Any]:
        return build_scoring_meta(
            provenance=provenance,
            supported_dims=self.supported_dims,
            defaulted_dims=self.defaulted_dims,
            requires_resolution=self.requires_resolution,
        )


# ---------------------------------------------------------------------------
# Public dispatch
# ---------------------------------------------------------------------------

def estimate_dimensions(
    profile: str,
    raw_payload: Dict[str, Any],
) -> Optional[DimensionEstimate]:
    """Return a heuristic dimension estimate for the profile, or None if the
    scanner payload doesn't support heuristic estimation.

    `None` is a deliberate signal: downstream rubric_engine.score_signal will
    return score=None/band=None, and the row lands in `signals` with NULL
    score/band (per migration 20260421000000). AI review is expected to fill
    the dims in for unscored profiles.
    """
    fn = _ESTIMATORS.get(profile)
    if fn is None:
        return None
    return fn(raw_payload or {})


# ---------------------------------------------------------------------------
# Clamp helper
# ---------------------------------------------------------------------------

def _clamp(v: float) -> int:
    if v < 1:
        return 1
    if v > 5:
        return 5
    return int(round(v))


def _coerce_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _materialize_estimate(
    profile: str,
    supported_values: Dict[str, int],
) -> DimensionEstimate:
    required = list(WEIGHTS[profile].keys())
    dimensions: Dict[str, int] = {}
    supported_dims: list[str] = []
    defaulted_dims: list[str] = []

    for dim in required:
        if dim in supported_values:
            dimensions[dim] = _clamp(supported_values[dim])
            supported_dims.append(dim)
        else:
            dimensions[dim] = 3
            defaulted_dims.append(dim)

    return DimensionEstimate(
        dimensions=dimensions,
        supported_dims=supported_dims,
        defaulted_dims=defaulted_dims,
        requires_resolution=bool(defaulted_dims),
    )


# ---------------------------------------------------------------------------
# short_positioning
#
# ESMA / FCA short-position scanner payload (see esma_short_scanner.py):
#   position_pct, previous_position_pct, change_pct, regulator, holder_name
#   (for crowding signals: total_disclosed_pct, regulators list, holder_count)
#
# Weights: crowding_intensity 2.5, trend_direction 2.0, catalyst_proximity 2.0,
#          size_vs_float 1.5, historical_analog 1.0, liquidity 1.0
#
# Single-position payloads support:
#   - crowding_intensity from position_pct / total_disclosed_pct
#   - trend_direction from change_pct (+ relative-change bumper)
#   - size_vs_float from position_pct
#
# Aggregate crowding payloads emitted by esma_short_scanner support:
#   - crowding_intensity from holder_count / total_disclosed_pct / regulators
#   - trend_direction from clustered disclosure recency in holders[].position_date
#   - size_vs_float from aggregate disclosed pct
# ---------------------------------------------------------------------------

def _aggregate_disclosed_pct(raw: Dict[str, Any]) -> Optional[float]:
    total_pct = _coerce_float(raw.get("total_disclosed_pct"))
    if total_pct is not None:
        return total_pct
    holders = raw.get("holders") or []
    total = 0.0
    saw_numeric = False
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        pct = _coerce_float(holder.get("position_pct"))
        if pct is None:
            continue
        total += pct
        saw_numeric = True
    return round(total, 2) if saw_numeric else None


def _single_short_size_tier(position_pct: float) -> int:
    if position_pct >= 3.0:
        return 5
    if position_pct >= 1.5:
        return 4
    if position_pct >= 0.8:
        return 3
    return 2


def _aggregate_short_size_tier(total_pct: float) -> int:
    if total_pct > 10.0:
        return 5
    if total_pct >= 5.0:
        return 4
    if total_pct >= 2.0:
        return 3
    if total_pct >= 1.0:
        return 2
    return 1


def _aggregate_crowding_tier(
    holder_count: Optional[int],
    total_pct: Optional[float],
    regulators: list[Any],
) -> int:
    crowding = 1
    if holder_count is not None:
        if holder_count >= 6:
            crowding = 5
        elif holder_count >= 4:
            crowding = 4
        elif holder_count >= 3:
            crowding = 3
        elif holder_count >= 2:
            crowding = 2
    if total_pct is not None:
        if total_pct >= 10.0:
            crowding = max(crowding, 5)
        elif total_pct >= 5.0:
            crowding = max(crowding, 4)
        elif total_pct >= 2.0:
            crowding = max(crowding, 3)
        elif total_pct >= 1.0:
            crowding = max(crowding, 2)
    if len(regulators) >= 2:
        crowding = _clamp(crowding + 1)
    return crowding


def _aggregate_trend_tier(holders: list[Any]) -> Optional[int]:
    ages: list[int] = []
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        age = _days_since(holder.get("position_date"))
        if age is not None:
            ages.append(age)
    if not ages:
        return None

    recent_7 = sum(age <= 7 for age in ages)
    recent_30 = sum(age <= 30 for age in ages)
    freshest = min(ages)

    if recent_7 >= 3 or recent_30 >= 4:
        return 5
    if recent_30 >= 2:
        return 4
    if freshest > 120:
        return 1
    if freshest > 60:
        return 2
    return 3


def _estimate_short_positioning(raw: Dict[str, Any]) -> Optional[DimensionEstimate]:
    holders = raw.get("holders") if isinstance(raw.get("holders"), list) else []
    regulators = raw.get("regulators") if isinstance(raw.get("regulators"), list) else []
    holder_count = raw.get("holder_count")
    if not isinstance(holder_count, int):
        holder_count = len(holders) or None

    total_pct = _aggregate_disclosed_pct(raw)
    position_pct = _coerce_float(raw.get("position_pct"))
    change_pct = _coerce_float(raw.get("change_pct"))
    prev_pct = _coerce_float(raw.get("previous_position_pct"))

    supported: Dict[str, int] = {}

    # Aggregate crowding payloads (holders/holder_count) deserve their own path.
    if holders or holder_count is not None:
        if holder_count is None and total_pct is None:
            return None
        supported["crowding_intensity"] = _aggregate_crowding_tier(
            holder_count,
            total_pct,
            regulators,
        )
        if total_pct is not None:
            supported["size_vs_float"] = _aggregate_short_size_tier(total_pct)
        trend = _aggregate_trend_tier(holders)
        if trend is not None:
            supported["trend_direction"] = trend
        return _materialize_estimate("short_positioning", supported)

    # Need at least one of these to produce a meaningful single-position estimate.
    if total_pct is None and position_pct is None:
        return None

    aggregate = total_pct if total_pct is not None else position_pct
    if aggregate is not None:
        if aggregate >= 10.0:
            crowding = 5
        elif aggregate >= 6.0:
            crowding = 4
        elif aggregate >= 3.0:
            crowding = 3
        elif aggregate >= 1.5:
            crowding = 2
        else:
            crowding = 1
        if len(regulators) >= 2:
            crowding = _clamp(crowding + 1)
        supported["crowding_intensity"] = crowding

    if change_pct is not None:
        if change_pct >= 0.5:
            trend = 5
        elif change_pct >= 0.1:
            trend = 4
        elif change_pct > -0.1:
            trend = 3
        elif change_pct > -0.5:
            trend = 2
        else:
            trend = 1

        if prev_pct is not None and prev_pct >= 0.3:
            rel = change_pct / prev_pct
            if rel >= 1.0:
                trend = _clamp(trend + 1)
            elif rel <= -0.5:
                trend = _clamp(trend - 1)
        supported["trend_direction"] = trend

    if position_pct is not None:
        supported["size_vs_float"] = _single_short_size_tier(position_pct)

    adv_usd = _raw_adv_usd(raw)
    if adv_usd is not None:
        supported["liquidity"] = _liquidity_tier(adv_usd)

    if not supported:
        return None
    return _materialize_estimate("short_positioning", supported)


# ---------------------------------------------------------------------------
# takeover_candidate
#
# Payload (see takeover_candidate_scanner.py): patterns_hit, pattern_names,
#   primary_filing.file_date, pe_filer_type, pe_filer_name.
#
# Weights: setup_strength 3.0, edge_freshness 2.0, valuation_cushion 2.0,
#          strategic_buyer_clarity 2.0, liquidity 1.0
# ---------------------------------------------------------------------------

def _days_since(date_str: Optional[str]) -> Optional[int]:
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - d).days


def _valuation_cushion_tier(discount_pct: float) -> int:
    if discount_pct > 35:
        return 5
    if discount_pct >= 20:
        return 4
    if discount_pct >= 5:
        return 3
    if discount_pct >= 0:
        return 2
    return 1


def _liquidity_tier(adv_usd: float) -> int:
    if adv_usd > 50_000_000:
        return 5
    if adv_usd >= 15_000_000:
        return 4
    if adv_usd >= 5_000_000:
        return 3
    if adv_usd >= 1_000_000:
        return 2
    return 1


def _raw_adv_usd(raw: Dict[str, Any]) -> Optional[float]:
    return (
        _coerce_float(raw.get("adv_usd"))
        or _coerce_float(raw.get("average_daily_dollar_volume"))
        or _coerce_float(raw.get("liquidity_usd"))
    )


def _estimate_takeover_candidate(raw: Dict[str, Any]) -> Optional[DimensionEstimate]:
    patterns_hit = raw.get("patterns_hit")
    if patterns_hit is None:
        return None

    # setup_strength — patterns_hit on 0-5 scale maps directly.
    # The profile's rubric awards bonus for explicit strategic-review
    # language; we approximate by bumping if pattern_names contains a
    # "strategic_review" marker.
    setup = _clamp(patterns_hit)
    names = raw.get("pattern_names") or []
    if any("strategic" in (n or "").lower() for n in names) and setup < 5:
        setup += 1

    # edge_freshness — file_date on primary filing.
    primary = raw.get("primary_filing") or {}
    age_days = _days_since(primary.get("file_date"))
    if age_days is None:
        freshness = 3
    elif age_days <= 30:
        freshness = 5
    elif age_days <= 90:
        freshness = 4
    elif age_days <= 180:
        freshness = 3
    elif age_days <= 365:
        freshness = 2
    else:
        freshness = 1

    # strategic_buyer_clarity — named strategic buyer > named PE > generic > unknown.
    buyer_type = (raw.get("pe_filer_type") or "").lower()
    if buyer_type in ("strategic", "strategic_acquirer"):
        buyer = 5
    elif raw.get("pe_filer_name"):
        buyer = 3 if buyer_type in ("pe", "private_equity") else 4
    else:
        buyer = 2

    supported: Dict[str, int] = {
        "setup_strength": _clamp(setup),
        "edge_freshness": _clamp(freshness),
        "strategic_buyer_clarity": _clamp(buyer),
    }

    valuation_discount = (
        _coerce_float(raw.get("valuation_discount_pct"))
        or _coerce_float(raw.get("valuation_cushion_pct"))
        or _coerce_float(raw.get("discount_to_5y_median_pct"))
    )
    if valuation_discount is not None:
        supported["valuation_cushion"] = _valuation_cushion_tier(valuation_discount)

    adv_usd = _raw_adv_usd(raw)
    if adv_usd is not None:
        supported["liquidity"] = _liquidity_tier(adv_usd)

    return _materialize_estimate("takeover_candidate", supported)


# ---------------------------------------------------------------------------
# binary_catalyst
#
# Payload (see fda_pdufa_pipeline.py): days_until_pdufa, is_resubmission,
#   adcom_date, adcom_vote, crl_date, status, enrichment.fda_history.
#
# Weights: approval_probability 2.5, market_mispricing 2.5, magnitude 1.5,
#          competitive_landscape 1.5, catalyst_timeline 1.0, liquidity 1.0
# ---------------------------------------------------------------------------

def _normalized_probability(value: Any) -> Optional[float]:
    prob = _coerce_float(value)
    if prob is None:
        return None
    if prob > 1.0 and prob <= 100.0:
        prob /= 100.0
    if 0.0 <= prob <= 1.0:
        return prob
    return None


def _approval_probability_tier(prob: float) -> int:
    if prob >= 0.75:
        return 5
    if prob >= 0.65:
        return 4
    if prob >= 0.55:
        return 3
    if prob >= 0.45:
        return 2
    return 1


def _adcom_vote_ratio(raw: Dict[str, Any]) -> Optional[float]:
    ratio = _normalized_probability(raw.get("adcom_support_ratio"))
    if ratio is not None:
        return ratio

    vote = raw.get("adcom_vote")
    if isinstance(vote, dict):
        yes = _coerce_float(vote.get("yes")) or 0.0
        no = _coerce_float(vote.get("no")) or 0.0
        total = yes + no
        return (yes / total) if total > 0 else None

    if isinstance(vote, str):
        parts = vote.split("-")
        if len(parts) != 2:
            return None
        yes = _coerce_float(parts[0])
        no = _coerce_float(parts[1])
        if yes is None or no is None:
            return None
        total = yes + no
        return (yes / total) if total > 0 else None
    return None


def _magnitude_tier(raw: Dict[str, Any]) -> Optional[int]:
    upside = _coerce_float(raw.get("upside_pct"))
    downside = _coerce_float(raw.get("downside_pct"))
    if upside is None and downside is None:
        return None
    move = max(abs(upside or 0.0), abs(downside or 0.0))
    if move > 50.0:
        return 5
    if move >= 30.0:
        return 4
    if move >= 15.0:
        return 3
    if move >= 5.0:
        return 2
    return 1


def _competitive_landscape_tier(raw: Dict[str, Any]) -> Optional[int]:
    history_count = _coerce_float(raw.get("approval_history_count"))
    if history_count is None:
        enrichment = raw.get("enrichment") or {}
        if isinstance(enrichment, dict):
            fda_history = enrichment.get("fda_history")
            if isinstance(fda_history, list):
                history_count = float(len(fda_history))
    if history_count is None:
        return None
    if history_count <= 0:
        return 5
    if history_count <= 1:
        return 4
    if history_count <= 3:
        return 3
    if history_count <= 6:
        return 2
    return 1


def _estimate_binary_catalyst(raw: Dict[str, Any]) -> Optional[DimensionEstimate]:
    days = _coerce_float(raw.get("days_until_pdufa"))
    if days is None:
        days = _coerce_float(raw.get("days_until_readout"))
    if days is None:
        return None

    if days <= 14:
        timeline = 5
    elif days <= 30:
        timeline = 4
    elif days <= 60:
        timeline = 3
    elif days <= 120:
        timeline = 2
    else:
        timeline = 1

    supported: Dict[str, int] = {"catalyst_timeline": _clamp(timeline)}

    status = (raw.get("status") or "").lower()
    if status in ("approved", "resolved_approved"):
        supported["approval_probability"] = 5
    elif status in ("rejected", "crl", "resolved_crl"):
        supported["approval_probability"] = 1
    else:
        raw_prob = _normalized_probability(raw.get("approval_probability"))
        if raw_prob is not None:
            supported["approval_probability"] = _approval_probability_tier(raw_prob)
        else:
            ratio = _adcom_vote_ratio(raw)
            if ratio is not None:
                supported["approval_probability"] = _approval_probability_tier(ratio)
            elif raw.get("is_resubmission"):
                supported["approval_probability"] = 2

    magnitude = _magnitude_tier(raw)
    if magnitude is not None:
        supported["magnitude"] = magnitude

    competitive = _competitive_landscape_tier(raw)
    if competitive is not None:
        supported["competitive_landscape"] = competitive

    adv_usd = _raw_adv_usd(raw)
    if adv_usd is not None:
        supported["liquidity"] = _liquidity_tier(adv_usd)

    return _materialize_estimate("binary_catalyst", supported)


# ---------------------------------------------------------------------------
# Unscored profiles
#
# For these, scanner output is genuinely insufficient to estimate dims
# honestly. Returning None flags the signal as unscored; AI review /
# human analyst fills dims in via the v2 thesis-authoring flow.
# ---------------------------------------------------------------------------

def _estimate_none(_: Dict[str, Any]) -> None:
    return None


_ESTIMATORS = {
    "short_positioning": _estimate_short_positioning,
    "takeover_candidate": _estimate_takeover_candidate,
    "binary_catalyst": _estimate_binary_catalyst,
    "activist_governance": _estimate_none,
    "merger_arb": _estimate_none,
    "litigation": _estimate_none,
}
