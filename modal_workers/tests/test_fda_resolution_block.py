"""
Resolution events MUST NOT promote as new opportunities.

This is the most important Phase 3 invariant: per the FDA Event-Investing
Cockpit V1 plan, "Block resolution events from new-opportunity promotion. They
flow through signal_resolver/candidate_aging to deliver/kill existing
candidates only."

Acceptance check:
  zero signals rows emitted for event_type IN ('approval','crl','presumed_crl','withdrawal').
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from modal_workers.scanners.fda_signal_bridge import (
    MODE_OPERATIONAL,
    MODE_SHADOW,
    MODE_SHADOW_WITH_EMIT,
    RESOLUTION_EVENT_TYPES,
    is_resolution_event,
    process_event,
    write_flags_for_mode,
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


# ---------------------------------------------------------------------------
# Pure resolution-block invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(RESOLUTION_EVENT_TYPES))
def test_is_resolution_event_recognizes_each_kind(kind):
    assert is_resolution_event(kind)


def test_is_resolution_event_lowercases_input():
    assert is_resolution_event("APPROVAL")
    assert is_resolution_event("CRL")


@pytest.mark.parametrize("kind", ["pdufa", "adcom", "phase3_readout", "eop2", "date_change"])
def test_non_resolution_event_types_are_not_blocked(kind):
    assert not is_resolution_event(kind)


@pytest.mark.parametrize("mode", [MODE_SHADOW, MODE_SHADOW_WITH_EMIT, MODE_OPERATIONAL])
@pytest.mark.parametrize("kind", sorted(RESOLUTION_EVENT_TYPES))
def test_resolution_events_emit_no_feature_no_signal_in_any_mode(kind, mode):
    out = process_event(
        event=_event(event_type=kind, event_status="resolved", event_date="2026-04-13"),
        asset=_asset(),
        evidence_rows=[],
        base_rates=BASE_RATES,
        market=None,
        options=None,
        mode=mode,
    )
    assert out.skipped_reason == "resolution_event"
    assert out.feature_snapshot is None
    assert out.emit_signal is False
    assert out.write_canonical is False
    assert out.write_shadow is False


def test_non_pending_pdufa_skipped():
    """A PDUFA row with event_status='superseded' (replaced by a newer date)
    must not score — only the latest pending row in the chain gets evaluated.
    """
    out = process_event(
        event=_event(event_status="superseded"),
        asset=_asset(),
        evidence_rows=[],
        base_rates=BASE_RATES,
        market=None,
        options=None,
        mode=MODE_SHADOW,
    )
    assert out.skipped_reason and out.skipped_reason.startswith("non_pending_status")
    assert out.emit_signal is False


# ---------------------------------------------------------------------------
# Mode wiring
# ---------------------------------------------------------------------------


def test_write_flags_for_mode_shadow():
    write_canonical, write_shadow, emit_signal = write_flags_for_mode(MODE_SHADOW)
    assert (write_canonical, write_shadow, emit_signal) == (False, True, False)


def test_write_flags_for_mode_shadow_with_emit():
    write_canonical, write_shadow, emit_signal = write_flags_for_mode(MODE_SHADOW_WITH_EMIT)
    assert (write_canonical, write_shadow, emit_signal) == (True, True, True)


def test_write_flags_for_mode_operational():
    write_canonical, write_shadow, emit_signal = write_flags_for_mode(MODE_OPERATIONAL)
    assert (write_canonical, write_shadow, emit_signal) == (True, False, True)


def test_write_flags_unknown_mode_raises():
    with pytest.raises(ValueError):
        write_flags_for_mode("turbo")


def test_pending_pdufa_in_shadow_mode_writes_shadow_only_no_signal():
    out = process_event(
        event=_event(),
        asset=_asset(),
        evidence_rows=[],
        base_rates=BASE_RATES,
        market=None,
        options=None,
        mode=MODE_SHADOW,
    )
    assert out.skipped_reason is None
    assert out.feature_snapshot is not None
    assert out.write_shadow is True
    assert out.write_canonical is False
    assert out.emit_signal is False


def test_pending_pdufa_in_operational_mode_writes_canonical_emits():
    out = process_event(
        event=_event(),
        asset=_asset(),
        evidence_rows=[],
        base_rates=BASE_RATES,
        market=None,
        options=None,
        mode=MODE_OPERATIONAL,
    )
    assert out.feature_snapshot is not None
    assert out.write_canonical is True
    assert out.write_shadow is False
    assert out.emit_signal is True
