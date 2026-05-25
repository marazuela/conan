from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")


def test_prediction_target_defaults_to_historical_forward_return_rule():
    from orchestrator_runtime.runtime import _coerce_prediction_target

    assert _coerce_prediction_target(None) == {
        "target_type": "price_move",
        "horizon_days": 30,
        "event_anchor": None,
        "label_rule": "forward_return_t30_calendar",
    }


def test_convergence_signature_buckets_conviction_and_sorts_ids():
    from orchestrator_runtime.runtime import compute_convergence_signature

    a = compute_convergence_signature(
        direction="long",
        calibrated_conviction_pct=81.9,
        cited_prose_blocks=[{"text": "same"}],
        key_facts=[{"fact_id": "f1"}],
        fact_ids=["b", "a"],
        document_ids=["d2", "d1"],
    )
    b = compute_convergence_signature(
        direction="long",
        calibrated_conviction_pct=82.1,
        cited_prose_blocks=[{"text": "same"}],
        key_facts=[{"fact_id": "f1"}],
        fact_ids=["a", "b"],
        document_ids=["d1", "d2"],
    )

    assert a == b


class _MarketSb:
    def _rest(self, method: str, path: str, *, params: Optional[Dict[str, str]] = None):
        if method == "GET" and path == "fda_regulatory_events":
            return [{
                "id": "evt-1",
                "event_date": "2026-09-15",
                "event_type": "pdufa",
                "event_status": "pending",
            }]
        return []


def test_market_side_gate_downgrades_low_ev(monkeypatch):
    import orchestrator_runtime.runtime as runtime

    monkeypatch.setattr(runtime, "MARKET_SIDE_GATE_ENABLED", True)
    monkeypatch.setattr(runtime, "MARKET_SIDE_GATE_EV_THRESHOLD_BPS", 500.0)

    fake_provider = MagicMock()
    fake_provider.get_straddle_implied_move.return_value = {
        "implied_move_pct": 10.0,
        "call_iv": 0.8,
        "put_iv": 1.0,
    }
    monkeypatch.setattr(
        "modal_workers.providers.polygon.base.PolygonClient",
        lambda: object(),
    )
    monkeypatch.setattr(
        "modal_workers.providers.polygon.options_data.PolygonOptionsData",
        lambda client: fake_provider,
    )

    run = runtime.AssessmentRun(
        asset_id="asset-1",
        trigger_type="manual",
        document_window_end=datetime(2026, 8, 15, tzinfo=timezone.utc),
    )
    context, band, reason = runtime.compute_market_side_context(
        _MarketSb(),
        asset_id="asset-1",
        asset={"ticker": "AXSM"},
        calibrated_conviction_pct=60.0,
        direction="long",
        current_band="immediate",
        run=run,
    )

    assert band == "watchlist"
    assert reason is not None and "low_ev_vs_market" in reason
    assert context["expected_value_bps"] == pytest.approx(100.0)
    assert context["options_iv"] == pytest.approx(90.0)
