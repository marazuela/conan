"""Tests for modal_workers.shared.cost_budget — 24h rollup + operator_flag.

Run: python -m pytest modal_workers/tests/test_cost_budget.py -v
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")

from modal_workers.shared.cost_budget import (
    ASSET_24H_SOFT_USD,
    ASSET_FLAG_KIND,
    ASSET_LINKER_24H_HARD_USD,
    ASSET_LINKER_FLAG_KIND,
    GLOBAL_24H_SOFT_USD,
    GLOBAL_FLAG_KIND,
    OPERATOR_FLAG_SOURCE,
    PER_RUN_HARD_KILL_USD,
    asset_24h_cost_usd,
    asset_linker_24h_cost_usd,
    check_24h_thresholds,
    check_asset_linker_hard_halt,
    global_24h_cost_usd,
    upsert_cost_flag,
)


def _now_iso(offset_hours: float = 0) -> str:
    return (datetime.now(timezone.utc)
            + timedelta(hours=offset_hours)).isoformat()


def _fallback_sb(rows):
    """SupabaseClient stub: RPC raises (forcing fallback to GET); GET returns
    `rows`."""
    sb = MagicMock()

    def _rest(method, path, **_kwargs):
        if path.startswith("rpc/"):
            raise Exception("RPC not available")
        return rows
    sb._rest = MagicMock(side_effect=_rest)
    return sb


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def test_constants_match_plan():
    assert PER_RUN_HARD_KILL_USD == 15.0
    assert ASSET_24H_SOFT_USD == 20.0
    assert GLOBAL_24H_SOFT_USD == 500.0
    assert OPERATOR_FLAG_SOURCE == "orchestrator_cost"
    assert ASSET_FLAG_KIND == "asset_24h_budget_breached"
    assert GLOBAL_FLAG_KIND == "global_24h_budget_breached"


# ---------------------------------------------------------------------------
# asset_24h_cost_usd — fallback path (RPC unavailable)
# ---------------------------------------------------------------------------

def test_asset_24h_sums_recent_rows():
    rows = [
        {"cost_usd": "5.50", "created_at": _now_iso(-1)},   # within 24h
        {"cost_usd": "3.25", "created_at": _now_iso(-2)},   # within 24h
        {"cost_usd": "7.10", "created_at": _now_iso(-23)},  # within 24h
        {"cost_usd": "9.99", "created_at": _now_iso(-25)},  # outside 24h
    ]
    sb = _fallback_sb(rows)
    total = asset_24h_cost_usd(sb, "asset-1")
    # Note: rows arrive sorted desc; the >24h row stops the scan early.
    assert total == 5.50 + 3.25 + 7.10


def test_asset_24h_handles_empty():
    sb = _fallback_sb([])
    assert asset_24h_cost_usd(sb, "asset-1") == 0.0


def test_asset_24h_handles_null_cost():
    rows = [{"cost_usd": None, "created_at": _now_iso(-1)}]
    sb = _fallback_sb(rows)
    assert asset_24h_cost_usd(sb, "asset-1") == 0.0


def test_asset_24h_uses_rpc_when_available():
    sb = MagicMock()
    sb._rest = MagicMock(return_value=[{"total_cost_usd": 42.50}])
    total = asset_24h_cost_usd(sb, "asset-1")
    assert total == 42.50
    # First call should be the RPC; we don't fall back to GET.
    args, kwargs = sb._rest.call_args
    assert args[0] == "POST"
    assert "rpc/" in args[1]


# ---------------------------------------------------------------------------
# global_24h_cost_usd — sums BOTH convergence_assessments and asset_linker_runs
# ---------------------------------------------------------------------------

def _split_sb(orch_rows: list, linker_rows: list, rpc_raises: bool = True):
    """Table-aware SupabaseClient stub for global_24h_cost_usd. Returns
    `orch_rows` for convergence_assessments and `linker_rows` for
    asset_linker_runs. RPC path raises by default to force the fallback."""
    sb = MagicMock()

    def _rest(method, path, **_kwargs):
        if path.startswith("rpc/"):
            if rpc_raises:
                raise Exception("RPC not available")
            return []
        if path == "convergence_assessments":
            return orch_rows
        if path == "asset_linker_runs":
            return linker_rows
        return []
    sb._rest = MagicMock(side_effect=_rest)
    return sb


def test_global_24h_sums_orchestrator_only_when_linker_empty():
    sb = _split_sb(
        orch_rows=[{"cost_usd": "1.00"}, {"cost_usd": "2.50"}, {"cost_usd": "0.10"}],
        linker_rows=[],
    )
    assert global_24h_cost_usd(sb) == 3.60


def test_global_24h_sums_linker_only_when_orchestrator_empty():
    """The 2026-05-11 incident burned ~$27 in asset_linker spend while
    convergence_assessments showed only $5.18. The new UNION must surface
    linker-only days."""
    sb = _split_sb(
        orch_rows=[],
        linker_rows=[{"cost_usd": "2.40"}, {"cost_usd": "2.45"}, {"cost_usd": "2.41"}],
    )
    assert global_24h_cost_usd(sb) == pytest.approx(7.26)


def test_global_24h_sums_both_tables():
    sb = _split_sb(
        orch_rows=[{"cost_usd": "5.00"}, {"cost_usd": "0.50"}],
        linker_rows=[{"cost_usd": "1.20"}, {"cost_usd": "0.80"}],
    )
    assert global_24h_cost_usd(sb) == pytest.approx(7.50)


def test_global_24h_treats_missing_linker_table_as_zero():
    """Pre-migration deploys won't have asset_linker_runs. Function must
    gracefully treat missing-table as zero, not crash."""
    sb = MagicMock()

    def _rest(method, path, **_kwargs):
        if path.startswith("rpc/"):
            raise Exception("RPC not available")
        if path == "convergence_assessments":
            return [{"cost_usd": "3.00"}]
        # asset_linker_runs not yet migrated
        raise Exception('relation "asset_linker_runs" does not exist')
    sb._rest = MagicMock(side_effect=_rest)
    assert global_24h_cost_usd(sb) == 3.00


def test_global_24h_handles_empty():
    sb = _split_sb(orch_rows=[], linker_rows=[])
    assert global_24h_cost_usd(sb) == 0.0


# ---------------------------------------------------------------------------
# asset_linker_24h_cost_usd / check_asset_linker_hard_halt — HARD halt
# Catches the 2026-05-11 incident vector at $10 instead of $40+.
# ---------------------------------------------------------------------------

def _linker_sb(rows, raise_missing_table: bool = False):
    sb = MagicMock()

    def _rest(method, path, **_kwargs):
        if path == "asset_linker_runs":
            if raise_missing_table:
                raise Exception('relation "asset_linker_runs" does not exist')
            return rows
        if path == "operator_flags":
            return []
        return []
    sb._rest = MagicMock(side_effect=_rest)
    return sb


def test_asset_linker_24h_sums_cost_usd():
    sb = _linker_sb([
        {"cost_usd": "4.50"}, {"cost_usd": "3.10"}, {"cost_usd": "0.40"},
    ])
    assert asset_linker_24h_cost_usd(sb) == pytest.approx(8.00)


def test_asset_linker_24h_missing_table_returns_zero():
    """Pre-migration safety — same pattern as global_24h_cost_usd."""
    sb = _linker_sb([], raise_missing_table=True)
    assert asset_linker_24h_cost_usd(sb) == 0.0


def test_hard_halt_does_not_fire_below_ceiling():
    sb = _linker_sb([{"cost_usd": "4.99"}])
    result = check_asset_linker_hard_halt(sb)
    assert result["halt"] is False
    assert result["total_24h_usd"] == pytest.approx(4.99)
    # No operator_flag insert on the non-halt path.
    inserts = [c for c in sb._rest.call_args_list
               if c.args == ("POST", "operator_flags")]
    assert inserts == []


def test_hard_halt_fires_at_ceiling_and_opens_flag():
    """At ASSET_LINKER_24H_HARD_USD ($10) the run loop must halt AND the
    breach must be visible in operator_flags so the dashboard surfaces it."""
    sb = _linker_sb([{"cost_usd": str(ASSET_LINKER_24H_HARD_USD)}])
    result = check_asset_linker_hard_halt(sb)
    assert result["halt"] is True
    assert result["total_24h_usd"] == pytest.approx(ASSET_LINKER_24H_HARD_USD)
    inserts = [c for c in sb._rest.call_args_list
               if c.args == ("POST", "operator_flags")]
    assert len(inserts) == 1
    body = inserts[0].kwargs["json_body"]
    assert body["source"] == OPERATOR_FLAG_SOURCE
    assert body["kind"] == ASSET_LINKER_FLAG_KIND
    # 'critical' (not 'error') to satisfy the operator_flags severity CHECK
    # constraint which whitelists info/warn/critical only.
    assert body["severity"] == "critical"


# ---------------------------------------------------------------------------
# upsert_cost_flag — schema correctness
# ---------------------------------------------------------------------------

def test_upsert_cost_flag_writes_orchestrator_cost_source():
    sb = MagicMock()
    sb._rest = MagicMock(return_value=[])
    upsert_cost_flag(
        sb, severity="warn", kind=ASSET_FLAG_KIND,
        title="t", body="b", asset_id="a-1",
        evidence={"x": 1},
    )
    call = sb._rest.call_args
    assert call.args == ("POST", "operator_flags")
    body = call.kwargs["json_body"]
    assert body["source"] == "orchestrator_cost"
    assert body["kind"] == ASSET_FLAG_KIND
    assert body["severity"] == "warn"
    assert body["evidence"]["asset_id"] == "a-1"
    assert body["evidence"]["x"] == 1
    # Resolution=ignore-duplicates relies on the partial unique index to
    # collapse repeat inserts at the same (source, kind, asset).
    assert "ignore-duplicates" in call.kwargs.get("prefer", "")


def test_upsert_cost_flag_swallows_failures():
    sb = MagicMock()
    sb._rest = MagicMock(side_effect=Exception("postgres down"))
    # Should not raise — observability must never break drain progression
    upsert_cost_flag(sb, severity="warn", kind="x", title="t", body="b")


# ---------------------------------------------------------------------------
# check_24h_thresholds — end-to-end
# ---------------------------------------------------------------------------

def test_check_24h_no_breach():
    # asset = $5, global = $50 — both under thresholds
    sb = MagicMock()
    posts: list = []

    def _rest(method, path, **kwargs):
        if path.startswith("rpc/"):
            raise Exception("no rpc")
        if method == "GET" and path == "convergence_assessments":
            params = kwargs.get("params", {})
            if "asset_id" in params:
                return [{"cost_usd": 5.00, "created_at": _now_iso(-1)}]
            return [{"cost_usd": 50.00}]
        if method == "POST" and path == "operator_flags":
            posts.append(kwargs)
            return []
        return []

    sb._rest = MagicMock(side_effect=_rest)
    result = check_24h_thresholds(sb, "asset-1")
    assert result["asset_breach"] is False
    assert result["global_breach"] is False
    # No flags written
    assert posts == []


def test_check_24h_asset_breach_fires_flag():
    sb = MagicMock()
    posts: list = []

    def _rest(method, path, **kwargs):
        if path.startswith("rpc/"):
            raise Exception("no rpc")
        if method == "GET" and path == "convergence_assessments":
            params = kwargs.get("params", {})
            if "asset_id" in params:
                return [{"cost_usd": 25.00, "created_at": _now_iso(-1)}]
            return [{"cost_usd": 50.00}]
        if method == "POST" and path == "operator_flags":
            posts.append(kwargs)
            return []
        return []

    sb._rest = MagicMock(side_effect=_rest)
    result = check_24h_thresholds(sb, "asset-1")
    assert result["asset_breach"] is True
    assert result["global_breach"] is False
    assert len(posts) == 1
    assert posts[0]["json_body"]["kind"] == ASSET_FLAG_KIND
    assert posts[0]["json_body"]["evidence"]["asset_id"] == "asset-1"


def test_check_24h_global_breach_fires_flag():
    sb = MagicMock()
    posts: list = []

    def _rest(method, path, **kwargs):
        if path.startswith("rpc/"):
            raise Exception("no rpc")
        if method == "GET" and path == "convergence_assessments":
            params = kwargs.get("params", {})
            if "asset_id" in params:
                return [{"cost_usd": 5.00, "created_at": _now_iso(-1)}]
            return [{"cost_usd": 600.00}]
        if method == "POST" and path == "operator_flags":
            posts.append(kwargs)
            return []
        return []

    sb._rest = MagicMock(side_effect=_rest)
    result = check_24h_thresholds(sb, "asset-1")
    assert result["asset_breach"] is False
    assert result["global_breach"] is True
    assert len(posts) == 1
    assert posts[0]["json_body"]["kind"] == GLOBAL_FLAG_KIND
