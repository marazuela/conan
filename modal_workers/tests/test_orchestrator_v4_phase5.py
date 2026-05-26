"""Phase 5 tests: rubric overhaul.

Three deliverables locked down here:

1. RUBRIC_VERSION bumped to 2 + binary_catalyst extended with the new
   insider_pressure (1.0) and shareholder_structure (0.5) dimensions
   sourced from the Phase 4 scanner work. Other five profiles in WEIGHTS
   carry byte-identical weights to v1.

2. get_active_weights() DB-driven lookup helper with Python WEIGHTS
   fallback. Not yet wired into score_signal (scanner hot path stays
   pure); Phase 7's agentic retrospective is the primary consumer.

3. Price-gate lint — invariant test grepping rubric_engine.py to confirm
   no market_cap / stock_price / price_pct reference participates in
   classify_band or discard selection. Stock-price hard-kill gates are
   the explicit anti-feature Pedro's v4 vision called out.

Plan: ~/.claude/plans/proud-booping-seal.md (Phase 5).
Run: python3 -m pytest modal_workers/tests/test_orchestrator_v4_phase5.py -v
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")


REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# RUBRIC_VERSION + WEIGHTS dimensions
# ---------------------------------------------------------------------------

def test_rubric_version_bumped_to_v2():
    from modal_workers.shared.rubric_engine import RUBRIC_VERSION

    assert RUBRIC_VERSION == 2, (
        f"Phase 5 expects RUBRIC_VERSION=2; got {RUBRIC_VERSION}"
    )


def test_binary_catalyst_gains_insider_pressure_and_shareholder_structure():
    from modal_workers.shared.rubric_engine import WEIGHTS

    bc = WEIGHTS["binary_catalyst"]
    assert bc.get("insider_pressure") == 1.0, (
        "Phase 5: binary_catalyst must gain insider_pressure with weight 1.0 "
        "to score Form 4 cluster reroute signals"
    )
    assert bc.get("shareholder_structure") == 0.5, (
        "Phase 5: binary_catalyst must gain shareholder_structure with weight "
        "0.5 to score 13D/13G signals"
    )


def test_binary_catalyst_preserves_v1_dimensions():
    """v1's six dims must survive intact — new dims are additive only."""
    from modal_workers.shared.rubric_engine import WEIGHTS

    bc = WEIGHTS["binary_catalyst"]
    # Byte-identical v1 weights — preservation covenant (rubric_engine.py
    # docstring lines 4-13).
    assert bc.get("approval_probability") == 2.5
    assert bc.get("market_mispricing") == 2.5
    assert bc.get("magnitude") == 1.5
    assert bc.get("competitive_landscape") == 1.5
    assert bc.get("catalyst_timeline") == 1.0
    assert bc.get("liquidity") == 1.0


def test_other_profiles_unchanged_from_v1():
    """Only binary_catalyst gains dims in v2. Others stay byte-identical
    so the rubric_version bump doesn't surprise non-FDA signals."""
    from modal_workers.shared.rubric_engine import WEIGHTS

    # Spot-check each non-binary_catalyst profile retains its v1 dim set.
    assert set(WEIGHTS["merger_arb"].keys()) == {
        "spread_size", "deal_certainty", "annualized_return",
        "break_risk", "liquidity",
    }
    assert set(WEIGHTS["short_positioning"].keys()) == {
        "crowding_intensity", "trend_direction", "catalyst_proximity",
        "size_vs_float", "historical_analog", "liquidity",
    }
    assert set(WEIGHTS["litigation"].keys()) == {
        "financial_materiality", "legal_outcome_probability",
        "market_pricing", "resolution_timeline", "liquidity",
        "party_resolution_confidence",
    }


# ---------------------------------------------------------------------------
# get_active_weights helper
# ---------------------------------------------------------------------------

class _StubClient:
    """Minimal SupabaseClient surrogate that returns canned _rest responses."""

    def __init__(self, response=None, raise_exc=None):
        self._response = response
        self._raise_exc = raise_exc
        self.calls: List[Dict[str, Any]] = []

    def _rest(self, method, path, params=None, **kw):
        self.calls.append({"method": method, "path": path, "params": params})
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response


def test_get_active_weights_returns_db_weights_when_available():
    from modal_workers.shared.rubric_engine import (
        clear_active_weights_cache,
        get_active_weights,
    )

    clear_active_weights_cache()
    stub = _StubClient(response=[{
        "dimension_weights": {
            "approval_probability": 3.0,  # different from Python WEIGHTS to prove DB win
            "market_mispricing": 2.0,
        }
    }])
    weights = get_active_weights("binary_catalyst", client=stub)

    assert weights == {"approval_probability": 3.0, "market_mispricing": 2.0}
    # Query targeted the active row for the profile.
    params = stub.calls[0]["params"]
    assert params["profile"] == "eq.binary_catalyst"
    assert params["superseded_at"] == "is.null"


def test_get_active_weights_falls_back_to_python_on_query_failure():
    from modal_workers.shared.rubric_engine import (
        WEIGHTS,
        clear_active_weights_cache,
        get_active_weights,
    )

    clear_active_weights_cache()
    stub = _StubClient(raise_exc=RuntimeError("db down"))
    weights = get_active_weights("binary_catalyst", client=stub)

    # Fallback returns the Python WEIGHTS dict for this profile.
    assert weights == WEIGHTS["binary_catalyst"]


def test_get_active_weights_falls_back_when_no_client():
    """No client passed → Python fallback. Pure-function callers (scanners)
    rely on this so they can stay synchronous + DB-free."""
    from modal_workers.shared.rubric_engine import (
        WEIGHTS,
        clear_active_weights_cache,
        get_active_weights,
    )

    clear_active_weights_cache()
    weights = get_active_weights("binary_catalyst", client=None)
    assert weights == WEIGHTS["binary_catalyst"]


def test_get_active_weights_caches_within_process():
    """The DB read is cached so scanner-tight loops don't re-query.
    Cache scope is process-lifetime; bypass_cache=True forces a re-read."""
    from modal_workers.shared.rubric_engine import (
        clear_active_weights_cache,
        get_active_weights,
    )

    clear_active_weights_cache()
    stub = _StubClient(response=[{
        "dimension_weights": {"approval_probability": 3.0}
    }])
    first = get_active_weights("binary_catalyst", client=stub)
    second = get_active_weights("binary_catalyst", client=stub)

    assert first == second
    # Only one DB call despite two calls (cache hit on second).
    assert len(stub.calls) == 1, (
        f"expected single DB hit (cache); got {len(stub.calls)}"
    )

    # bypass_cache=True forces a re-read.
    third = get_active_weights(
        "binary_catalyst", client=stub, bypass_cache=True,
    )
    assert third == first
    assert len(stub.calls) == 2


def test_get_active_weights_rejects_malformed_db_row():
    """DB row with non-dict / empty / wrong-type weights → don't cache,
    fall back to Python."""
    from modal_workers.shared.rubric_engine import (
        WEIGHTS,
        clear_active_weights_cache,
        get_active_weights,
    )

    clear_active_weights_cache()
    # Empty dict.
    stub_empty = _StubClient(response=[{"dimension_weights": {}}])
    assert get_active_weights("binary_catalyst", client=stub_empty) == \
        WEIGHTS["binary_catalyst"]

    clear_active_weights_cache()
    # Strings where numbers expected — filtered out.
    stub_bad_types = _StubClient(response=[{
        "dimension_weights": {"approval_probability": "high", "magnitude": 2.0}
    }])
    weights = get_active_weights("binary_catalyst", client=stub_bad_types)
    # Only the numeric entry survives the validator.
    assert weights == {"magnitude": 2.0}


# ---------------------------------------------------------------------------
# Migration shape
# ---------------------------------------------------------------------------

def test_v2_rubric_migration_exists():
    path = REPO_ROOT / "supabase" / "migrations" / "20260613007000_v4_rubrics_v2_seed.sql"
    assert path.exists(), f"Phase 5 migration missing at {path}"


def test_v2_migration_supersedes_v1_and_inserts_six_profiles():
    path = REPO_ROOT / "supabase" / "migrations" / "20260613007000_v4_rubrics_v2_seed.sql"
    sql = path.read_text()

    # Supersession step (must come before inserts, BEGIN/COMMIT wraps).
    assert "UPDATE public.rubrics" in sql
    assert "SET superseded_at = now()" in sql
    assert "WHERE rubric_version = 1" in sql

    # All six v2 rows present.
    for profile in (
        "merger_arb",
        "activist_governance",
        "binary_catalyst",
        "short_positioning",
        "litigation",
        "takeover_candidate",
    ):
        assert f"'{profile}', 2," in sql, (
            f"migration missing v2 INSERT for profile {profile}"
        )

    # Idempotency guard on (profile, rubric_version) — re-running the
    # migration must be a no-op.
    assert "ON CONFLICT (profile, rubric_version) DO NOTHING" in sql

    # binary_catalyst v2 specifically must carry the new dims.
    assert "'insider_pressure', 1.0" in sql
    assert "'shareholder_structure', 0.5" in sql


# ---------------------------------------------------------------------------
# Price-gate lint — the explicit anti-feature
# ---------------------------------------------------------------------------

def test_price_does_not_participate_in_band_assignment():
    """v4 covenant: no stock_price / market_cap / price_pct hard gate in
    classify_band or discard selection. Price stays a contextual input,
    used only in post-catalyst fallback (pre_edge_monitor.py).

    Implementation: read rubric_engine.py source, locate the body of
    classify_band, assert no price-related identifier appears in it.
    """
    import inspect

    from modal_workers.shared import rubric_engine

    band_source = inspect.getsource(rubric_engine.classify_band)
    forbidden_in_band = [
        "market_cap",
        "stock_price",
        "price_pct",
        "current_price",
        "share_price",
    ]
    for term in forbidden_in_band:
        assert term not in band_source, (
            f"classify_band must not reference '{term}' — that would be a "
            f"price-based hard gate, banned by v4 covenant. See plan §Phase 5."
        )


def test_price_does_not_drive_discard_in_weighted_total():
    """weighted_total computes the raw score from dim×weight only. No
    price-based subtraction / floor / ceiling allowed."""
    import inspect

    from modal_workers.shared import rubric_engine

    total_source = inspect.getsource(rubric_engine.weighted_total)
    forbidden = ["market_cap", "stock_price", "price_pct", "current_price"]
    for term in forbidden:
        assert term not in total_source, (
            f"weighted_total must not reference '{term}'"
        )


def test_rubric_engine_documents_no_price_gate_policy():
    """The policy comment must live at the top of rubric_engine.py so
    future editors don't reintroduce a price gate by accident."""
    path = REPO_ROOT / "modal_workers" / "shared" / "rubric_engine.py"
    head = path.read_text()[:3000]  # check the top of the file

    assert "Stock price" in head or "stock price" in head.lower(), (
        "rubric_engine.py header must document the no-price-gate covenant"
    )
    assert "pre_edge_monitor" in head, (
        "header must reference pre_edge_monitor as the legitimate exception "
        "(post-catalyst fallback, operator-overridable)"
    )


# ---------------------------------------------------------------------------
# Bands semantics — keep 4-band taxonomy (corrected from earlier plan)
# ---------------------------------------------------------------------------

def test_bands_remain_four_taxonomy():
    """Plan's earlier critique of band/lifecycle as redundant was wrong;
    research showed they serve different purposes (band = immutable signal
    provenance, lifecycle state = mutable operational status). Phase 5
    keeps both — this test guards against accidental reduction."""
    from modal_workers.shared.rubric_engine import classify_band

    # Four named bands, monotone thresholds.
    assert classify_band(40.0) == "immediate"
    assert classify_band(35.0) == "immediate"
    assert classify_band(34.9) == "watchlist"
    assert classify_band(25.0) == "watchlist"
    assert classify_band(24.9) == "archive"
    assert classify_band(15.0) == "archive"
    assert classify_band(14.9) == "discard"
    assert classify_band(0.0) == "discard"
