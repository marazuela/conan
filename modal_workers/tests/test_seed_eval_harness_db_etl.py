"""Tests for seed_eval_harness_db_etl — Phase 4B subset ETL.

Covers:
  - Ticker resolution under all three multi-asset modes (newest, skip, active).
  - Idempotency: existing (asset_id, reference_assessment_date) pairs skipped.
  - Outcome derivation from the label dict.
  - Dry-run vs apply: inserted=0 in dry-run, inserted=N in apply.
  - Skip categories from the staged ledger are passed through unchanged.

Stubbed SupabaseClient avoids live DB hits.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from modal_workers.scripts.seed_eval_harness_db_etl import (
    EtlSummary,
    _build_eval_harness_row,
    _derive_realized_outcome,
    _resolve_tickers,
    run_etl,
)


# ---------------------------------------------------------------------------
# Stub SupabaseClient
# ---------------------------------------------------------------------------


class _StubSb:
    """Minimal SupabaseClient stand-in. Records every _rest call's
    (method, table, params, json_body) so tests can assert what would have
    been written.

    Pre-populate `assets_by_ticker` and `existing_eval_keys` to script the
    GET responses; everything else returns empty lists.
    """

    def __init__(
        self,
        *,
        assets_by_ticker: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        existing_eval_keys: Optional[set[tuple[str, str]]] = None,
    ):
        self.assets_by_ticker = assets_by_ticker or {}
        self.existing_eval_keys = existing_eval_keys or set()
        self.calls: List[Dict[str, Any]] = []

    def _rest(
        self, method: str, table: str, *,
        params: Optional[Dict[str, str]] = None,
        json: Any = None,
        headers: Optional[Dict[str, str]] = None,
    ):
        self.calls.append({
            "method": method, "table": table, "params": params, "json": json,
        })
        if method != "GET":
            return []

        if table == "fda_assets":
            # params['ticker'] = 'in.(AXSM,AZN,...)'
            in_filter = (params or {}).get("ticker") or ""
            tickers = (
                in_filter.removeprefix("in.(").removesuffix(")").split(",")
                if in_filter.startswith("in.(") else []
            )
            rows: List[Dict[str, Any]] = []
            for tk in tickers:
                rows.extend(self.assets_by_ticker.get(tk, []))
            return rows

        if table == "eval_harness":
            in_filter = (params or {}).get("asset_id") or ""
            asset_ids = (
                in_filter.removeprefix("in.(").removesuffix(")").split(",")
                if in_filter.startswith("in.(") else []
            )
            return [
                {"asset_id": aid, "reference_assessment_date": d}
                for (aid, d) in self.existing_eval_keys
                if aid in asset_ids
            ]

        return []

    def _rest_with_retry(
        self, method: str, table: str, *,
        params: Optional[Dict[str, str]] = None,
        json_body: Any = None,
        prefer: Optional[str] = None,
    ):
        self.calls.append({
            "method": method, "table": table, "params": params,
            "json_body": json_body, "prefer": prefer,
        })
        return []


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def staging_file(tmp_path: Path) -> Path:
    """Synthetic staging ledger with three resolved rows and one skip."""
    blob = {
        "_meta": {"source_events": "synthetic", "profile": "binary_catalyst"},
        "staging": [
            {
                "event_id": "evt1", "ticker": "AXSM", "filed_at": "2024-01-15",
                "profile": "binary_catalyst",
                "label": {
                    "hit": True, "hit_window_days": 30,
                    "windows": [{"days": 30, "return_pct": 28.5}],
                },
                "skip_category": None,
                "asset_id": None, "document_set": None,
                "tradeable_filter_pass": None,
            },
            {
                "event_id": "evt2", "ticker": "AXSM", "filed_at": "2024-06-20",
                "profile": "binary_catalyst",
                "label": {
                    "hit": False,
                    "miss_reason": "t30_return_pct=+5.0_below_+20",
                },
                "skip_category": None,
                "asset_id": None, "document_set": None,
                "tradeable_filter_pass": None,
            },
            {
                "event_id": "evt3", "ticker": "PFE", "filed_at": "2024-03-01",
                "profile": "binary_catalyst",
                "label": {"hit": False},
                "skip_category": None,
                "asset_id": None, "document_set": None,
                "tradeable_filter_pass": None,
            },
            {
                "event_id": "evt4", "ticker": "PRIVATE_DISCARD",
                "filed_at": "2024-04-04",
                "profile": "binary_catalyst",
                "label": {"hit": None, "miss_reason": "private"},
                "skip_category": "private_or_unresolvable",
                "asset_id": None, "document_set": None,
                "tradeable_filter_pass": None,
            },
        ],
    }
    p = tmp_path / "staging.json"
    p.write_text(json.dumps(blob))
    return p


# ---------------------------------------------------------------------------
# Outcome derivation
# ---------------------------------------------------------------------------


def test_derive_outcome_hit_with_window():
    assert _derive_realized_outcome({"hit": True, "hit_window_days": 60}) \
        == "binary_catalyst_hit_60d"


def test_derive_outcome_hit_no_window():
    assert _derive_realized_outcome({"hit": True}) == "binary_catalyst_hit"


def test_derive_outcome_miss():
    assert _derive_realized_outcome({"hit": False, "miss_reason": "x"}) \
        == "binary_catalyst_miss"


def test_derive_outcome_unresolved():
    assert _derive_realized_outcome({"hit": None}) == "unresolved"


# ---------------------------------------------------------------------------
# Row builder
# ---------------------------------------------------------------------------


def test_build_row_populates_required_columns():
    staged = {
        "event_id": "x1", "ticker": "AXSM", "filed_at": "2024-09-15",
        "label": {"hit": True, "hit_window_days": 30},
    }
    row = _build_eval_harness_row(asset_id="aaaa", staged=staged)
    # NOT NULL cols all present:
    assert row["asset_id"] == "aaaa"
    assert row["reference_assessment_date"] == "2024-09-15"
    assert row["realized_outcome"] == "binary_catalyst_hit_30d"
    assert row["realized_outcome_data"] == staged["label"]
    assert row["document_set"] == []
    # Defaults:
    assert row["is_holdout"] is False
    assert row["tradeable_filter_pass"] is False
    assert row["difficulty"] is None
    assert "phase4b_seed:" in row["notes"]
    assert "x1" in row["notes"]


# ---------------------------------------------------------------------------
# Ticker resolution
# ---------------------------------------------------------------------------


def test_resolve_tickers_single_asset_picks_unique_row():
    sb = _StubSb(assets_by_ticker={
        "AXSM": [{"id": "a-axsm", "ticker": "AXSM",
                  "is_active": True, "created_at": "2024-01-01"}],
    })
    tmap = _resolve_tickers(sb, ["AXSM"], mode="newest")
    assert tmap.chosen == {"AXSM": "a-axsm"}
    assert tmap.ambiguous == {}
    assert tmap.skipped == []


def test_resolve_tickers_newest_picks_max_created_at():
    sb = _StubSb(assets_by_ticker={
        "PFE": [
            {"id": "p1", "ticker": "PFE", "is_active": True,  "created_at": "2024-01-01"},
            {"id": "p2", "ticker": "PFE", "is_active": False, "created_at": "2025-01-01"},
            {"id": "p3", "ticker": "PFE", "is_active": True,  "created_at": "2023-06-01"},
        ],
    })
    tmap = _resolve_tickers(sb, ["PFE"], mode="newest")
    assert tmap.chosen == {"PFE": "p2"}
    assert tmap.ambiguous == {"PFE": ["p1", "p2", "p3"]}
    assert tmap.skipped == []


def test_resolve_tickers_active_prefers_active_then_newest():
    sb = _StubSb(assets_by_ticker={
        "PFE": [
            {"id": "p1", "ticker": "PFE", "is_active": True,  "created_at": "2024-01-01"},
            {"id": "p2", "ticker": "PFE", "is_active": False, "created_at": "2025-01-01"},
            {"id": "p3", "ticker": "PFE", "is_active": True,  "created_at": "2023-06-01"},
        ],
    })
    tmap = _resolve_tickers(sb, ["PFE"], mode="active")
    # Active pool = {p1, p3}; newest of those = p1.
    assert tmap.chosen == {"PFE": "p1"}


def test_resolve_tickers_active_falls_back_to_newest_if_no_active():
    sb = _StubSb(assets_by_ticker={
        "PFE": [
            {"id": "p1", "ticker": "PFE", "is_active": False, "created_at": "2024-01-01"},
            {"id": "p2", "ticker": "PFE", "is_active": False, "created_at": "2025-01-01"},
        ],
    })
    tmap = _resolve_tickers(sb, ["PFE"], mode="active")
    assert tmap.chosen == {"PFE": "p2"}


def test_resolve_tickers_skip_drops_multi_asset():
    sb = _StubSb(assets_by_ticker={
        "AXSM": [{"id": "a-axsm", "ticker": "AXSM", "is_active": True, "created_at": "2024"}],
        "PFE": [
            {"id": "p1", "ticker": "PFE", "is_active": True, "created_at": "2024"},
            {"id": "p2", "ticker": "PFE", "is_active": True, "created_at": "2025"},
        ],
    })
    tmap = _resolve_tickers(sb, ["AXSM", "PFE"], mode="skip")
    assert tmap.chosen == {"AXSM": "a-axsm"}
    assert "PFE" in tmap.skipped
    assert tmap.ambiguous == {"PFE": ["p1", "p2"]}


def test_resolve_tickers_unmatched_ticker_absent_from_map():
    sb = _StubSb(assets_by_ticker={"AXSM": [
        {"id": "a", "ticker": "AXSM", "is_active": True, "created_at": "2024"},
    ]})
    tmap = _resolve_tickers(sb, ["AXSM", "UNKNOWN"], mode="newest")
    assert tmap.chosen == {"AXSM": "a"}
    assert "UNKNOWN" not in tmap.chosen


# ---------------------------------------------------------------------------
# End-to-end ETL
# ---------------------------------------------------------------------------


def test_etl_dry_run_inserts_nothing(staging_file: Path):
    sb = _StubSb(assets_by_ticker={
        "AXSM": [{"id": "a-axsm", "ticker": "AXSM",
                  "is_active": True, "created_at": "2024"}],
        "PFE": [{"id": "p-pfe", "ticker": "PFE",
                 "is_active": True, "created_at": "2024"}],
    })
    summary = run_etl(staging_path=staging_file, sb=sb, apply=False)

    assert summary.staged_total == 4
    assert summary.staged_resolved == 3
    assert summary.matched_tickers == 2
    assert summary.matched_records == 3   # 2 AXSM + 1 PFE
    assert summary.inserted == 0
    assert summary.by_hit["HIT"] == 1
    assert summary.by_hit["MISS"] == 2

    inserts = [c for c in sb.calls if c["method"] == "POST"]
    assert inserts == [], "dry-run must not POST"


def test_etl_apply_writes_eval_harness_rows(staging_file: Path):
    sb = _StubSb(assets_by_ticker={
        "AXSM": [{"id": "a-axsm", "ticker": "AXSM",
                  "is_active": True, "created_at": "2024"}],
        "PFE": [{"id": "p-pfe", "ticker": "PFE",
                 "is_active": True, "created_at": "2024"}],
    })
    summary = run_etl(staging_path=staging_file, sb=sb, apply=True)

    assert summary.inserted == 3
    assert summary.errors == 0

    # Check the POST batch included the right shapes.
    inserts = [c for c in sb.calls if c["method"] == "POST"]
    assert len(inserts) == 1
    batch = inserts[0]["json_body"]
    assert isinstance(batch, list)
    assert len(batch) == 3
    asset_ids = {row["asset_id"] for row in batch}
    assert asset_ids == {"a-axsm", "p-pfe"}
    # All rows have required NOT NULL fields:
    for row in batch:
        assert row["realized_outcome"]
        assert row["realized_outcome_data"]
        assert row["document_set"] == []
        assert row["is_holdout"] is False


def test_etl_skips_already_existing_pairs(staging_file: Path):
    """Re-running the ETL should be a no-op on rows that already exist."""
    sb = _StubSb(
        assets_by_ticker={
            "AXSM": [{"id": "a-axsm", "ticker": "AXSM",
                      "is_active": True, "created_at": "2024"}],
            "PFE": [{"id": "p-pfe", "ticker": "PFE",
                     "is_active": True, "created_at": "2024"}],
        },
        existing_eval_keys={
            ("a-axsm", "2024-01-15"),  # evt1 already in
            ("p-pfe", "2024-03-01"),    # evt3 already in
        },
    )
    summary = run_etl(staging_path=staging_file, sb=sb, apply=True)

    # Only evt2 (AXSM 2024-06-20) is new.
    assert summary.matched_records == 1
    assert summary.skipped_existing == 2
    assert summary.inserted == 1


def test_etl_unmatched_ticker_increments_no_asset_skip(staging_file: Path):
    """When the ticker isn't in fda_assets at all, count as skipped_no_asset."""
    sb = _StubSb(assets_by_ticker={
        # Only AXSM matches; PFE absent.
        "AXSM": [{"id": "a-axsm", "ticker": "AXSM",
                  "is_active": True, "created_at": "2024"}],
    })
    summary = run_etl(staging_path=staging_file, sb=sb, apply=False)

    assert summary.matched_records == 2  # both AXSM rows
    assert summary.skipped_no_asset == 1  # the PFE row
    assert summary.skipped_multi_asset == 0


def test_etl_skip_mode_drops_multi_asset_tickers(staging_file: Path):
    """In --multi-asset=skip, PFE's two-asset case is dropped entirely."""
    sb = _StubSb(assets_by_ticker={
        "AXSM": [{"id": "a-axsm", "ticker": "AXSM",
                  "is_active": True, "created_at": "2024"}],
        "PFE": [
            {"id": "p1", "ticker": "PFE", "is_active": True, "created_at": "2024"},
            {"id": "p2", "ticker": "PFE", "is_active": True, "created_at": "2025"},
        ],
    })
    summary = run_etl(
        staging_path=staging_file, sb=sb,
        multi_asset="skip", apply=False,
    )
    # AXSM × 2 matched; PFE skipped.
    assert summary.matched_records == 2
    assert summary.skipped_multi_asset == 1


def test_etl_limit_caps_resolved_rows(staging_file: Path):
    sb = _StubSb(assets_by_ticker={
        "AXSM": [{"id": "a-axsm", "ticker": "AXSM",
                  "is_active": True, "created_at": "2024"}],
        "PFE": [{"id": "p-pfe", "ticker": "PFE",
                 "is_active": True, "created_at": "2024"}],
    })
    summary = run_etl(
        staging_path=staging_file, sb=sb, limit=1, apply=False,
    )
    # Only the first resolved row (AXSM evt1 HIT) counted.
    assert summary.matched_records == 1
    assert summary.by_hit["HIT"] == 1
    assert summary.by_hit["MISS"] == 0
