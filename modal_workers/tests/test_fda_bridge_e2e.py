"""
Bridge end-to-end tests with mocked providers.

Acceptance criteria covered:
  - Evidence -> event -> feature snapshot path is reproducible
    (canonical_inputs_hash matches across repeated runs).
  - market_implied_probability=None blocks Immediate eligibility
    (band demoted from immediate to watchlist).
  - feature_payload selects the right column set for each mode.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from unittest.mock import MagicMock

import pytest

from modal_workers.scanners.fda_event_features import FeatureSnapshot
from modal_workers.scanners.fda_signal_bridge import (
    MODE_OPERATIONAL,
    MODE_SHADOW,
    MODE_SHADOW_WITH_EMIT,
    feature_payload,
    gate_immediate_when_market_p_missing,
    process_event,
    upsert_feature_snapshot,
)


BASE_RATES = {"default": 0.58, "psychiatry_agitation": 0.55}


def _event(**overrides):
    base = {
        "id": "evt-001",
        "asset_id": "asset-001",
        "event_type": "pdufa",
        "event_date": "2026-09-15",
        "event_status": "pending",
        "source_content_hash": "sha-abc",
    }
    base.update(overrides)
    return base


def _asset(**overrides):
    base = {
        "id": "asset-001",
        "ticker": "AXSM",
        "drug_name": "AXS-05",
        "indication": "Agitation associated with Alzheimer's",
        "application_number": "",
    }
    base.update(overrides)
    return base


def _make_market_provider(market_cap=2_000_000_000, adv=15_000_000):
    m = MagicMock()
    m.get_market_cap.return_value = market_cap
    m.get_adv.return_value = adv
    return m


def _make_options_provider(*, straddle_implied_move=None, liquidity_score=4.0):
    m = MagicMock()
    if straddle_implied_move is None:
        m.get_straddle_implied_move.return_value = None
    else:
        m.get_straddle_implied_move.return_value = {
            "underlying_price": 50.0,
            "expiry": "2026-09-19",
            "call_mid": 4.5,
            "put_mid": 4.0,
            "straddle_price": 8.5,
            "implied_move_pct": straddle_implied_move,
            "call_iv": 0.92,
            "put_iv": 0.95,
        }
    m.get_event_window_liquidity.return_value = {
        "contract_count": 18,
        "total_open_interest": 6500,
        "liquidity_score": liquidity_score,
    }
    return m


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------


def test_repeat_processing_deterministic_hash_and_score():
    fixed_now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    market = _make_market_provider()
    options = _make_options_provider(straddle_implied_move=17.0)
    a = process_event(
        event=_event(),
        asset=_asset(),
        evidence_rows=[],
        base_rates=BASE_RATES,
        market=market,
        options=options,
        mode=MODE_SHADOW,
        snapshot_at=fixed_now,
    )
    b = process_event(
        event=_event(),
        asset=_asset(),
        evidence_rows=[],
        base_rates=BASE_RATES,
        market=market,
        options=options,
        mode=MODE_SHADOW,
        snapshot_at=fixed_now,
    )
    assert a.feature_snapshot.inputs_hash == b.feature_snapshot.inputs_hash
    assert a.feature_snapshot.score == b.feature_snapshot.score
    assert a.feature_snapshot.band == b.feature_snapshot.band


# ---------------------------------------------------------------------------
# Immediate gate when market_p missing
# ---------------------------------------------------------------------------


def _make_immediate_snapshot(*, market_p: Optional[float]) -> FeatureSnapshot:
    return FeatureSnapshot(
        fair_probability=0.85,
        market_implied_probability=market_p,
        upside_pct=60.0,
        downside_pct=40.0,
        expected_value_pct=27.0,
        pricing_edge=0.30 if market_p is not None else None,
        evidence_confidence=0.8,
        options_liquidity_score=5.0,
        market_cap_usd=200_000_000,
        adv_usd=20_000_000,
        implied_move_pct=18.0,
        score=42.0,
        band="immediate",
        raw_inputs={"x": 1},
        inputs_hash="abcd",
    )


def test_gate_demotes_immediate_when_market_p_missing():
    snap = _make_immediate_snapshot(market_p=None)
    out, demoted = gate_immediate_when_market_p_missing(snap)
    assert demoted is True
    assert out.band == "watchlist"
    # Score is not changed — only the band gate applies (calibration may
    # adjust score thresholds separately).
    assert out.score == 42.0
    assert out.raw_inputs.get("_immediate_demoted_no_market_p") is True


def test_gate_no_op_when_market_p_present():
    snap = _make_immediate_snapshot(market_p=0.55)
    out, demoted = gate_immediate_when_market_p_missing(snap)
    assert demoted is False
    assert out.band == "immediate"
    assert "_immediate_demoted_no_market_p" not in out.raw_inputs


def test_process_event_demotes_when_options_unavailable():
    """A pending PDUFA with no options data: market_p=None, so even a high
    score must not produce band='immediate' on persistence."""
    market = _make_market_provider()
    options = _make_options_provider(straddle_implied_move=None)  # illiquid -> None
    out = process_event(
        event=_event(),
        asset=_asset(),
        evidence_rows=[],
        base_rates=BASE_RATES,
        market=market,
        options=options,
        mode=MODE_OPERATIONAL,
    )
    assert out.feature_snapshot is not None
    assert out.feature_snapshot.market_implied_probability is None
    # Band must not be immediate (either demoted, or never reached due to score).
    if out.feature_snapshot.score >= 35:
        assert out.feature_snapshot.band == "watchlist"
        assert out.immediate_demoted is True


# ---------------------------------------------------------------------------
# feature_payload column selection
# ---------------------------------------------------------------------------


def test_feature_payload_shadow_only_has_no_canonical_columns():
    snap = _make_immediate_snapshot(market_p=0.55)
    body = feature_payload(snap, write_canonical=False, write_shadow=True)
    assert "score" not in body
    assert "band" not in body
    assert body["shadow_score"] == snap.score
    assert body["shadow_band"] == snap.band
    assert body["shadow_expected_value_pct"] == snap.expected_value_pct
    assert body["shadow_pricing_edge"] == snap.pricing_edge


def test_feature_payload_operational_has_no_shadow_columns():
    snap = _make_immediate_snapshot(market_p=0.55)
    body = feature_payload(snap, write_canonical=True, write_shadow=False)
    assert body["score"] == snap.score
    assert body["band"] == snap.band
    assert "shadow_score" not in body
    assert "shadow_band" not in body


def test_feature_payload_shadow_with_emit_writes_both_sides():
    snap = _make_immediate_snapshot(market_p=0.55)
    body = feature_payload(snap, write_canonical=True, write_shadow=True)
    assert body["score"] == snap.score
    assert body["band"] == snap.band
    assert body["shadow_score"] == snap.score
    assert body["shadow_band"] == snap.band


def test_feature_payload_descriptive_columns_always_present():
    snap = _make_immediate_snapshot(market_p=0.55)
    for write_c, write_s in [(True, True), (True, False), (False, True)]:
        body = feature_payload(snap, write_canonical=write_c, write_shadow=write_s)
        for key in (
            "fair_probability", "market_implied_probability",
            "upside_pct", "downside_pct", "expected_value_pct", "pricing_edge",
            "evidence_confidence", "options_liquidity_score", "market_cap_usd",
            "adv_usd", "implied_move_pct", "raw_inputs", "inputs_hash",
        ):
            assert key in body, f"{key} missing under canonical={write_c} shadow={write_s}"


# ---------------------------------------------------------------------------
# upsert_feature_snapshot wires through to Supabase REST
# ---------------------------------------------------------------------------


def test_upsert_feature_snapshot_uses_event_id_conflict_target():
    snap = _make_immediate_snapshot(market_p=0.55)
    client = MagicMock()
    upsert_feature_snapshot(client, "evt-xyz", snap, write_canonical=False, write_shadow=True)
    args, kwargs = client._rest_with_retry.call_args
    assert args[0] == "POST"
    assert args[1].startswith("fda_event_features?on_conflict=event_id")
    body = kwargs["json_body"][0]
    assert body["event_id"] == "evt-xyz"
    assert body["shadow_score"] == snap.score
    assert "score" not in body
