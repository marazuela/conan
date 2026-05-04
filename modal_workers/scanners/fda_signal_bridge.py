"""
FDA signal bridge — turns canonical fda_regulatory_events rows into either
shadow-only feature snapshots (Phase 3) or live signals + features (post-cutover).

Operating modes (read from public.scanners.config.mode, defaulting to 'shadow'):

  shadow            Write shadow_* columns on fda_event_features only. No signals
                    row emission. Existing binary_catalyst flow continues
                    untouched. This is the default while the new pipeline is
                    being calibrated against catalyst_universe ground truth.

  shadow_with_emit  Cutover-interim. Write shadow_* AND canonical score/band on
                    fda_event_features, plus emit signals rows with
                    scoring_profile='fda_event'. Used to confirm zero divergence
                    between feature snapshots and emitted signal rows over a
                    short window before flipping fully operational.

  operational       Write only canonical score/band on fda_event_features and
                    emit signals rows. Phase 6 final state.

Two hard gates are enforced before any signal would be considered for emission:

  1. Resolution events (event_type IN approval/crl/presumed_crl/withdrawal) are
     never new opportunities. They flow through signal_resolver / candidate_aging
     to deliver/kill existing candidates instead. The bridge skips them entirely.

  2. Immediate eligibility requires market_implied_probability NOT NULL. Without
     a market probability, pricing edge is undefined; the rubric cannot certify
     that a position is mispriced enough to warrant Immediate. Bridge demotes
     these from 'immediate' to 'watchlist' before persisting score/band.

Design contract: process_event() is pure given (event, asset, evidence_rows,
providers, mode). Tests can drive it without a database.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from modal_workers.providers.polygon.market_data import MarketDataProvider
from modal_workers.providers.polygon.options_data import OptionsDataProvider
from modal_workers.scanners.fda_event_features import (
    FeatureSnapshot,
    build_features,
)

logger = logging.getLogger(__name__)

NAME = "fda_signal_bridge"

# Event types that resolve a prior milestone — must NOT promote as new
# opportunities. signal_resolver / candidate_aging handle these.
RESOLUTION_EVENT_TYPES = frozenset({"approval", "crl", "presumed_crl", "withdrawal"})

MODE_SHADOW = "shadow"
MODE_SHADOW_WITH_EMIT = "shadow_with_emit"
MODE_OPERATIONAL = "operational"
VALID_MODES = (MODE_SHADOW, MODE_SHADOW_WITH_EMIT, MODE_OPERATIONAL)


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------


@dataclass
class ProcessOutcome:
    """Pure outcome of processing one fda_regulatory_events row.

    Persistence is the caller's concern — the Modal scan() entry point owns DB
    writes and respects the scanner config. Tests check this dataclass directly
    without touching the network or the DB.
    """
    event_id: str
    skipped_reason: Optional[str] = None
    feature_snapshot: Optional[FeatureSnapshot] = None
    write_canonical: bool = False
    write_shadow: bool = False
    emit_signal: bool = False
    immediate_demoted: bool = False


# ---------------------------------------------------------------------------
# Pure pipeline
# ---------------------------------------------------------------------------


def is_resolution_event(event_type: Optional[str]) -> bool:
    return (event_type or "").lower() in RESOLUTION_EVENT_TYPES


def gate_immediate_when_market_p_missing(
    snapshot: FeatureSnapshot,
) -> tuple[FeatureSnapshot, bool]:
    """If band == 'immediate' and market_implied_probability is None, demote
    to 'watchlist'. Returns (possibly-replaced snapshot, demoted_flag).
    """
    if snapshot.band == "immediate" and snapshot.market_implied_probability is None:
        return (
            FeatureSnapshot(
                fair_probability=snapshot.fair_probability,
                market_implied_probability=snapshot.market_implied_probability,
                upside_pct=snapshot.upside_pct,
                downside_pct=snapshot.downside_pct,
                expected_value_pct=snapshot.expected_value_pct,
                pricing_edge=snapshot.pricing_edge,
                evidence_confidence=snapshot.evidence_confidence,
                options_liquidity_score=snapshot.options_liquidity_score,
                market_cap_usd=snapshot.market_cap_usd,
                adv_usd=snapshot.adv_usd,
                implied_move_pct=snapshot.implied_move_pct,
                score=snapshot.score,
                band="watchlist",
                raw_inputs={
                    **snapshot.raw_inputs,
                    "_immediate_demoted_no_market_p": True,
                },
                inputs_hash=snapshot.inputs_hash,
            ),
            True,
        )
    return snapshot, False


def write_flags_for_mode(mode: str) -> tuple[bool, bool, bool]:
    """Return (write_canonical, write_shadow, emit_signal) for a given mode."""
    if mode == MODE_SHADOW:
        return (False, True, False)
    if mode == MODE_SHADOW_WITH_EMIT:
        return (True, True, True)
    if mode == MODE_OPERATIONAL:
        return (True, False, True)
    raise ValueError(f"unknown bridge mode: {mode!r}")


def process_event(
    *,
    event: Mapping[str, Any],
    asset: Mapping[str, Any],
    evidence_rows: Sequence[Mapping[str, Any]],
    base_rates: Mapping[str, float],
    market: Optional[MarketDataProvider],
    options: Optional[OptionsDataProvider],
    mode: str,
    snapshot_at: Optional[datetime] = None,
    designations: Optional[Mapping[str, Any]] = None,
) -> ProcessOutcome:
    """Pure single-event pipeline.

    The caller is responsible for: looking up the asset/evidence rows,
    instantiating providers, and writing the resulting FeatureSnapshot back to
    fda_event_features (canonical + shadow_* per write_* flags) plus emitting a
    signals row when emit_signal=True.
    """
    if mode not in VALID_MODES:
        raise ValueError(f"unknown bridge mode: {mode!r}")

    event_id = str(event.get("id") or "")
    event_type = event.get("event_type")
    event_status = (event.get("event_status") or "pending").lower()

    # Gate 1: resolution events never produce new-opportunity signals.
    if is_resolution_event(event_type):
        return ProcessOutcome(
            event_id=event_id,
            skipped_reason="resolution_event",
        )

    # Gate 0: only pending events are scored. Superseded rows have been replaced
    # by a newer event with the same identity (e.g. PDUFA date moved); the new
    # event is what the bridge sees.
    if event_status != "pending":
        return ProcessOutcome(
            event_id=event_id,
            skipped_reason=f"non_pending_status:{event_status}",
        )

    snapshot = build_features(
        event_id=event_id,
        asset=dict(asset),
        event=dict(event),
        evidence_rows=[dict(r) for r in evidence_rows],
        base_rates=base_rates,
        market=market,
        options=options,
        snapshot_at=snapshot_at,
        designations=dict(designations) if designations else None,
    )

    # Gate 2: no auto-Immediate without market_implied_probability.
    snapshot, demoted = gate_immediate_when_market_p_missing(snapshot)

    write_canonical, write_shadow, emit_signal = write_flags_for_mode(mode)
    return ProcessOutcome(
        event_id=event_id,
        feature_snapshot=snapshot,
        write_canonical=write_canonical,
        write_shadow=write_shadow,
        emit_signal=emit_signal,
        immediate_demoted=demoted,
    )


# ---------------------------------------------------------------------------
# Persistence helpers (talk to Supabase)
# ---------------------------------------------------------------------------


def feature_payload(snapshot: FeatureSnapshot, *, write_canonical: bool, write_shadow: bool) -> Dict[str, Any]:
    """Build the upsert payload for fda_event_features given the write flags.

    Canonical columns: score, band, expected_value_pct, pricing_edge, etc.
    Shadow columns:    shadow_score, shadow_band, shadow_expected_value_pct,
                       shadow_pricing_edge, shadow_recorded_at.

    Both sides write the same `raw_inputs`/`inputs_hash` and the descriptive
    columns (fair_probability, market_implied_probability, ...) so a shadow-only
    run still leaves a complete feature snapshot for the dashboard event detail.
    """
    payload: Dict[str, Any] = {
        "fair_probability": snapshot.fair_probability,
        "market_implied_probability": snapshot.market_implied_probability,
        "upside_pct": snapshot.upside_pct,
        "downside_pct": snapshot.downside_pct,
        "expected_value_pct": snapshot.expected_value_pct,
        "pricing_edge": snapshot.pricing_edge,
        "evidence_confidence": snapshot.evidence_confidence,
        "options_liquidity_score": snapshot.options_liquidity_score,
        "market_cap_usd": snapshot.market_cap_usd,
        "adv_usd": snapshot.adv_usd,
        "implied_move_pct": snapshot.implied_move_pct,
        "raw_inputs": snapshot.raw_inputs,
        "inputs_hash": snapshot.inputs_hash,
        "snapshot_at": snapshot.raw_inputs.get("snapshot_at"),
    }
    if write_canonical:
        payload["score"] = snapshot.score
        payload["band"] = snapshot.band
    if write_shadow:
        payload["shadow_score"] = snapshot.score
        payload["shadow_band"] = snapshot.band
        payload["shadow_expected_value_pct"] = snapshot.expected_value_pct
        payload["shadow_pricing_edge"] = snapshot.pricing_edge
        payload["shadow_recorded_at"] = snapshot.raw_inputs.get("snapshot_at")
    return payload


def upsert_feature_snapshot(
    client: Any,  # SupabaseClient
    event_id: str,
    snapshot: FeatureSnapshot,
    *,
    write_canonical: bool,
    write_shadow: bool,
) -> None:
    body = feature_payload(snapshot, write_canonical=write_canonical, write_shadow=write_shadow)
    body["event_id"] = event_id
    client._rest_with_retry(
        "POST",
        "fda_event_features?on_conflict=event_id",
        json_body=[body],
        prefer="resolution=merge-duplicates,return=minimal",
    )
