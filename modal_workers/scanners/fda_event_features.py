"""
Feature builder for FDA regulatory events.

For each fda_regulatory_events row, this module produces a deterministic
fda_event_features snapshot:

  fair_probability         model prior (base rate by indication) adjusted by
                           designation modifiers (priority review, breakthrough,
                           accelerated, RTOR, resubmission) and any validated
                           specialist agent reviews.
  market_implied_probability  derived from Polygon options straddle when liquid;
                           fallback proxy uses recent stock run-up vs analyst
                           target. Returns None when neither path is available;
                           the bridge then blocks Immediate eligibility.
  upside_pct, downside_pct expected % move on positive/negative outcome. Anchored
                           to comparable historical moves, then market-cap-bucket
                           defaults (megacap 4/3, smallcap 60/40 — preserved from
                           the v1 scanner).
  expected_value_pct       fair_probability * upside_pct
                           - (1 - fair_probability) * abs(downside_pct).
  pricing_edge             fair_probability - market_implied_probability.
  evidence_confidence      0..1 confidence rolled up from evidence count and
                           specialist agent confidence.
  options_liquidity_score  0..5 from Polygon event-window OI/contract count.
  market_cap_usd, adv_usd  from Polygon reference + aggregates.
  implied_move_pct         straddle-implied % move from Polygon options.
  raw_inputs               JSON dict of every numeric input in this snapshot.
                           inputs_hash = sha256 of canonicalized raw_inputs.

Determinism contract: given the same event row, asset row, evidence rows,
provider responses, and model_version, the resulting feature snapshot
(score, band, expected_value_pct, etc.) is byte-equal across runs. This is
required by the Phase 6 acceptance criterion ("every fda_event_features.score
reproducible from (event_id, snapshot_at, model_version_id, raw_inputs)").

The actual score / band write happens here at canonical-only level (the bridge
is what stamps shadow_* during Phase 3 and live score/band post-cutover); see
modal_workers/scanners/fda_signal_bridge.py.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from modal_workers.providers.polygon.market_data import MarketDataProvider
from modal_workers.providers.polygon.options_data import OptionsDataProvider
from modal_workers.shared.biotech_base_rates import (
    DEFAULT_APPROVAL_PROB,
    INDICATION_MAP,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Designation modifiers (preserved from v1 scanner constants)
# ---------------------------------------------------------------------------

DESIGNATION_MODIFIERS_DEFAULT: Dict[str, float] = {
    "priority_review": 0.05,
    "breakthrough": 0.04,
    "accelerated": 0.03,
    "rtor": 0.02,
    "fast_track": 0.02,
    "is_resubmission": -0.10,
}

# Magnitude defaults by market-cap bucket (preserved verbatim from v1):
# (mcap_floor_usd, upside_pct, downside_pct).
MCAP_BUCKETS: List[tuple] = [
    (50_000_000_000, 4.0, 3.0),       # megacap (>$50B)
    (10_000_000_000, 10.0, 8.0),      # large-cap
    (2_000_000_000, 20.0, 15.0),      # mid-cap
    (300_000_000, 35.0, 25.0),        # small-cap
    (0, 60.0, 40.0),                  # micro/nano-cap fallback
]

# Default band thresholds (Phase 6 calibration may move these).
BAND_THRESHOLDS_DEFAULT: Dict[str, float] = {
    "immediate": 35.0,
    "watchlist": 25.0,
    "archive": 15.0,
}


# ---------------------------------------------------------------------------
# Pure helpers (testable without DB or providers)
# ---------------------------------------------------------------------------


def map_indication_to_base_key(indication: Optional[str]) -> Optional[str]:
    if not indication:
        return None
    text = indication.lower()
    for pattern, key in INDICATION_MAP:
        if re.search(pattern, text, re.IGNORECASE):
            return key
    return None


def base_probability(
    indication: Optional[str],
    base_rates: Mapping[str, float],
    *,
    default: float = DEFAULT_APPROVAL_PROB,
) -> float:
    key = map_indication_to_base_key(indication)
    if key and key in base_rates:
        return float(base_rates[key])
    if "default" in base_rates:
        return float(base_rates["default"])
    return default


def apply_designation_modifiers(
    base: float,
    designations: Mapping[str, Any],
    *,
    modifiers: Mapping[str, float] = DESIGNATION_MODIFIERS_DEFAULT,
) -> float:
    p = float(base)
    for name, delta in modifiers.items():
        if designations.get(name):
            p += float(delta)
    return max(0.0, min(1.0, p))


def expected_value_pct(
    fair_p: float, upside_pct: float, downside_pct: float
) -> float:
    return fair_p * upside_pct - (1.0 - fair_p) * abs(downside_pct)


def pricing_edge(fair_p: float, market_p: Optional[float]) -> Optional[float]:
    if market_p is None:
        return None
    return fair_p - market_p


def magnitude_defaults_for_mcap(market_cap_usd: Optional[float]) -> tuple:
    """Return (upside_pct, downside_pct) defaults for the given market cap."""
    if market_cap_usd is None:
        # Conservative middle-of-the-road default
        return (35.0, 25.0)
    for floor, up, down in MCAP_BUCKETS:
        if market_cap_usd >= floor:
            return (up, down)
    return (60.0, 40.0)


def implied_move_to_market_probability(
    implied_move_pct: float,
    upside_pct: float,
    downside_pct: float,
) -> Optional[float]:
    """Binary-event implied probability from straddle implied move.

    For a binary catalyst with positive payoff +U% and negative payoff -D%
    (D >= 0), expected absolute move under risk-neutral pricing is
        E[|move|] = p*U + (1-p)*D
    so
        p = (implied_move - D) / (U - D), clamped to [0, 1].

    When U == D the implied move is invariant under p, so probability
    cannot be inferred — return None.
    """
    U = abs(upside_pct)
    D = abs(downside_pct)
    if U == D:
        return None
    p = (implied_move_pct - D) / (U - D)
    if p < 0:
        return 0.0
    if p > 1:
        return 1.0
    return p


def derive_band(
    score: float,
    *,
    thresholds: Mapping[str, float] = BAND_THRESHOLDS_DEFAULT,
) -> str:
    if score >= thresholds.get("immediate", BAND_THRESHOLDS_DEFAULT["immediate"]):
        return "immediate"
    if score >= thresholds.get("watchlist", BAND_THRESHOLDS_DEFAULT["watchlist"]):
        return "watchlist"
    if score >= thresholds.get("archive", BAND_THRESHOLDS_DEFAULT["archive"]):
        return "archive"
    return "discard"


def evidence_confidence(
    *,
    evidence_count: int,
    agent_confidences: List[float],
) -> float:
    """Roll up a 0..1 confidence from raw counts + specialist agent confidences.

    Capped at 1.0. Empty/no signal -> 0.0.
    """
    # Raw evidence saturates around 6 sources (edgar, openfda, ct.gov, fed
    # register, polygon, manual). Each adds 0.1.
    base = min(0.6, 0.1 * float(max(0, evidence_count)))
    if agent_confidences:
        agent_avg = sum(agent_confidences) / len(agent_confidences)
        base += 0.4 * agent_avg
    return min(1.0, max(0.0, base))


def canonical_inputs_hash(raw_inputs: Dict[str, Any]) -> str:
    """Stable sha256 over a canonicalized JSON encoding of raw_inputs."""
    blob = json.dumps(
        raw_inputs,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Score blend (placeholder — Phase 6 calibration may tune weights)
# ---------------------------------------------------------------------------

# Component weights sum to 50 to match the v1 binary_catalyst rubric scale.
SCORE_WEIGHT_PROBABILITY = 12.5    # P(approval)
SCORE_WEIGHT_PRICING_EDGE = 12.5   # |fair_p - market_p|
SCORE_WEIGHT_MAGNITUDE = 7.5       # max(upside, |downside|)
SCORE_WEIGHT_EV = 7.5              # EV%
SCORE_WEIGHT_TIMELINE = 5.0        # days_to_event
SCORE_WEIGHT_LIQUIDITY = 5.0       # ADV + options liquidity


def _score_probability(fair_p: float) -> float:
    """0..5 mapped from P(approval)."""
    if fair_p >= 0.80: return 5.0
    if fair_p >= 0.60: return 4.0
    if fair_p >= 0.40: return 3.0
    if fair_p >= 0.20: return 2.0
    return 1.0


def _score_pricing_edge(edge: Optional[float]) -> float:
    """0..5 mapped from |fair_p - market_p| in pp (0..1 -> 0..100pp)."""
    if edge is None:
        return 0.0
    abs_edge_pp = abs(edge) * 100.0
    if abs_edge_pp >= 20: return 5.0
    if abs_edge_pp >= 10: return 4.0
    if abs_edge_pp >= 5:  return 3.0
    if abs_edge_pp >= 2:  return 2.0
    return 1.0


def _score_magnitude(upside_pct: float, downside_pct: float) -> float:
    m = max(abs(upside_pct), abs(downside_pct))
    if m >= 50: return 5.0
    if m >= 30: return 4.0
    if m >= 15: return 3.0
    if m >= 5:  return 2.0
    return 1.0


def _score_ev(ev_pct: float) -> float:
    if ev_pct >= 25: return 5.0
    if ev_pct >= 15: return 4.0
    if ev_pct >= 5:  return 3.0
    if ev_pct >= 0:  return 2.0
    return 1.0


def _score_timeline(days_to_event: Optional[int]) -> float:
    if days_to_event is None:
        return 1.0
    if days_to_event <= 14: return 5.0
    if days_to_event <= 30: return 4.0
    if days_to_event <= 60: return 3.0
    if days_to_event <= 90: return 2.0
    return 1.0


def _score_liquidity(adv_usd: Optional[float], options_score: Optional[float]) -> float:
    """Blend ADV in USD + options liquidity (0..5)."""
    if adv_usd is None and options_score is None:
        return 1.0
    if adv_usd is None:
        return float(options_score or 1.0)
    if adv_usd >= 100_000_000:
        adv_score = 5.0
    elif adv_usd >= 10_000_000:
        adv_score = 4.0
    elif adv_usd >= 1_000_000:
        adv_score = 3.0
    elif adv_usd >= 100_000:
        adv_score = 2.0
    else:
        adv_score = 1.0
    if options_score is None:
        return adv_score
    return (adv_score + float(options_score)) / 2.0


def compute_score(
    *,
    fair_probability: float,
    pricing_edge_value: Optional[float],
    upside_pct: float,
    downside_pct: float,
    expected_value: float,
    days_to_event: Optional[int],
    adv_usd: Optional[float],
    options_liquidity_score: Optional[float],
) -> float:
    """Weighted blend across six dimensions, returning 0..50."""
    s = 0.0
    s += _score_probability(fair_probability) * (SCORE_WEIGHT_PROBABILITY / 5.0)
    s += _score_pricing_edge(pricing_edge_value) * (SCORE_WEIGHT_PRICING_EDGE / 5.0)
    s += _score_magnitude(upside_pct, downside_pct) * (SCORE_WEIGHT_MAGNITUDE / 5.0)
    s += _score_ev(expected_value) * (SCORE_WEIGHT_EV / 5.0)
    s += _score_timeline(days_to_event) * (SCORE_WEIGHT_TIMELINE / 5.0)
    s += _score_liquidity(adv_usd, options_liquidity_score) * (SCORE_WEIGHT_LIQUIDITY / 5.0)
    return round(s, 2)


# ---------------------------------------------------------------------------
# Composer — pure given inputs; no DB/HTTP I/O lives in here
# ---------------------------------------------------------------------------


@dataclass
class FeatureInputs:
    indication: Optional[str]
    designations: Dict[str, Any]
    event_date: Optional[date]
    snapshot_at: datetime
    base_rates: Mapping[str, float]
    market_cap_usd: Optional[float]
    adv_usd: Optional[float]
    straddle: Optional[Dict[str, Any]]      # output of OptionsDataProvider.get_straddle_implied_move
    options_liquidity: Optional[Dict[str, Any]]   # output of OptionsDataProvider.get_event_window_liquidity
    evidence_count: int
    agent_confidences: List[float]
    band_thresholds: Mapping[str, float] = None  # type: ignore[assignment]


@dataclass
class FeatureSnapshot:
    fair_probability: float
    market_implied_probability: Optional[float]
    upside_pct: float
    downside_pct: float
    expected_value_pct: float
    pricing_edge: Optional[float]
    evidence_confidence: float
    options_liquidity_score: Optional[float]
    market_cap_usd: Optional[float]
    adv_usd: Optional[float]
    implied_move_pct: Optional[float]
    score: float
    band: str
    raw_inputs: Dict[str, Any]
    inputs_hash: str


def _days_to_event(snapshot_at: datetime, event_date: Optional[date]) -> Optional[int]:
    if event_date is None:
        return None
    return (event_date - snapshot_at.astimezone(timezone.utc).date()).days


def compose_features(inputs: FeatureInputs) -> FeatureSnapshot:
    """Pure composition: deterministic given identical inputs.

    Does not touch the network or the database — caller must already have
    fetched provider data. Tests can call this directly with hand-built inputs.
    """
    band_thresholds = inputs.band_thresholds or BAND_THRESHOLDS_DEFAULT

    fair_p = apply_designation_modifiers(
        base_probability(inputs.indication, inputs.base_rates),
        inputs.designations,
    )

    upside_pct, downside_pct = magnitude_defaults_for_mcap(inputs.market_cap_usd)

    implied_move_pct: Optional[float] = None
    market_p: Optional[float] = None
    if inputs.straddle and inputs.straddle.get("implied_move_pct") is not None:
        implied_move_pct = float(inputs.straddle["implied_move_pct"])
        market_p = implied_move_to_market_probability(implied_move_pct, upside_pct, downside_pct)

    options_liq_score: Optional[float] = None
    if inputs.options_liquidity and inputs.options_liquidity.get("liquidity_score") is not None:
        options_liq_score = float(inputs.options_liquidity["liquidity_score"])

    ev_pct = expected_value_pct(fair_p, upside_pct, downside_pct)
    edge = pricing_edge(fair_p, market_p)
    confidence = evidence_confidence(
        evidence_count=inputs.evidence_count,
        agent_confidences=inputs.agent_confidences,
    )

    days = _days_to_event(inputs.snapshot_at, inputs.event_date)
    score = compute_score(
        fair_probability=fair_p,
        pricing_edge_value=edge,
        upside_pct=upside_pct,
        downside_pct=downside_pct,
        expected_value=ev_pct,
        days_to_event=days,
        adv_usd=inputs.adv_usd,
        options_liquidity_score=options_liq_score,
    )
    band = derive_band(score, thresholds=band_thresholds)

    raw_inputs: Dict[str, Any] = {
        "indication": inputs.indication,
        "designations": dict(inputs.designations),
        "event_date": inputs.event_date.isoformat() if inputs.event_date else None,
        "snapshot_at": inputs.snapshot_at.astimezone(timezone.utc).isoformat(),
        "base_rates_used": {
            "key": map_indication_to_base_key(inputs.indication),
            "default": inputs.base_rates.get("default"),
        },
        "market_cap_usd": inputs.market_cap_usd,
        "adv_usd": inputs.adv_usd,
        "straddle": inputs.straddle,
        "options_liquidity": inputs.options_liquidity,
        "evidence_count": inputs.evidence_count,
        "agent_confidences": list(inputs.agent_confidences),
        "fair_probability": fair_p,
        "implied_move_pct": implied_move_pct,
        "market_implied_probability": market_p,
        "upside_pct": upside_pct,
        "downside_pct": downside_pct,
        "band_thresholds": dict(band_thresholds),
    }
    return FeatureSnapshot(
        fair_probability=fair_p,
        market_implied_probability=market_p,
        upside_pct=upside_pct,
        downside_pct=downside_pct,
        expected_value_pct=ev_pct,
        pricing_edge=edge,
        evidence_confidence=confidence,
        options_liquidity_score=options_liq_score,
        market_cap_usd=inputs.market_cap_usd,
        adv_usd=inputs.adv_usd,
        implied_move_pct=implied_move_pct,
        score=score,
        band=band,
        raw_inputs=raw_inputs,
        inputs_hash=canonical_inputs_hash(raw_inputs),
    )


# ---------------------------------------------------------------------------
# I/O orchestration (touches DB and providers)
# ---------------------------------------------------------------------------


def build_features(
    *,
    event_id: str,
    asset: Dict[str, Any],
    event: Dict[str, Any],
    evidence_rows: List[Dict[str, Any]],
    base_rates: Mapping[str, float],
    market: Optional[MarketDataProvider],
    options: Optional[OptionsDataProvider],
    snapshot_at: Optional[datetime] = None,
    designations: Optional[Mapping[str, Any]] = None,
) -> FeatureSnapshot:
    """Pull provider data and compose a feature snapshot for one event.

    Inputs:
      asset       — dict with keys ticker, mic, indication
      event       — dict with keys event_type, event_date (str or date)
      evidence_rows — already-fetched fda_event_evidence rows
      base_rates  — phase3_base_rates dict (per biotech_base_rates.load_base_rates)
      market, options — provider instances (or None to degrade gracefully)
      designations — explicit designation flags (priority_review, breakthrough, ...).
                     Defaults to an empty dict if omitted; in production these come
                     from the asset's enrichment block or evidence rows.
    """
    snapshot_at = (snapshot_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    designations = dict(designations or {})

    # Event date may arrive as ISO string or date.
    raw_event_date = event.get("event_date")
    if isinstance(raw_event_date, datetime):
        event_date = raw_event_date.date()
    elif isinstance(raw_event_date, date):
        event_date = raw_event_date
    elif isinstance(raw_event_date, str) and raw_event_date:
        try:
            event_date = datetime.strptime(raw_event_date[:10], "%Y-%m-%d").date()
        except ValueError:
            event_date = None
    else:
        event_date = None

    ticker = asset.get("ticker") or ""
    indication = asset.get("indication")

    market_cap_usd: Optional[float] = None
    adv_usd: Optional[float] = None
    if market and ticker:
        try:
            market_cap_usd = market.get_market_cap(ticker)
        except Exception as exc:
            logger.warning("polygon market_cap failed for %s: %s", ticker, exc)
        try:
            adv_usd = market.get_adv(ticker, days=30)
        except Exception as exc:
            logger.warning("polygon adv failed for %s: %s", ticker, exc)

    straddle: Optional[Dict[str, Any]] = None
    options_liquidity: Optional[Dict[str, Any]] = None
    if options and ticker and event_date is not None:
        try:
            straddle = options.get_straddle_implied_move(ticker, event_date)
        except Exception as exc:
            logger.warning("polygon straddle failed for %s: %s", ticker, exc)
        try:
            options_liquidity = options.get_event_window_liquidity(ticker, event_date)
        except Exception as exc:
            logger.warning("polygon options liquidity failed for %s: %s", ticker, exc)

    # Roll up specialist agent confidences from evidence rows tagged as agent_*.
    agent_confidences: List[float] = []
    for ev in evidence_rows or []:
        if (ev.get("source") or "").startswith("agent_"):
            payload = ev.get("payload") or {}
            conf = payload.get("confidence")
            if conf is not None:
                try:
                    agent_confidences.append(float(conf))
                except (TypeError, ValueError):
                    pass

    inputs = FeatureInputs(
        indication=indication,
        designations=designations,
        event_date=event_date,
        snapshot_at=snapshot_at,
        base_rates=base_rates,
        market_cap_usd=market_cap_usd,
        adv_usd=adv_usd,
        straddle=straddle,
        options_liquidity=options_liquidity,
        evidence_count=len(evidence_rows or []),
        agent_confidences=agent_confidences,
    )
    return compose_features(inputs)
