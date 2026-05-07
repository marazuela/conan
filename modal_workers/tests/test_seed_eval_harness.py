"""Tests for `seed_eval_harness_from_export` — staging record shape +
skip-category mapping + summary stats.

Run: python -m pytest modal_workers/tests/test_seed_eval_harness.py -v
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")


@pytest.fixture
def fake_events_file(tmp_path) -> Path:
    p = tmp_path / "binary_catalyst.json"
    p.write_text(json.dumps({
        "_meta": {"profile": "binary_catalyst"},
        "events": [
            {"event_id": "e1", "ticker": "AXSM", "filed_at": "2024-08-01"},
            {"event_id": "e2", "ticker": "VRDN", "filed_at": "2024-09-15"},
            {"event_id": "e3", "ticker": "?", "filed_at": "2024-10-01"},
            {"event_id": "e4", "ticker": "BAD", "filed_at": "not-a-date"},
        ],
    }))
    return p


@pytest.fixture
def fake_labeler(monkeypatch):
    """Stub label_ledger to return one canned label per event input. Order
    matches the input event order — that's the contract the seed script
    relies on."""
    canned: List[Dict[str, Any]] = [
        # e1 — HIT
        {"event_id": "e1", "ticker": "AXSM", "filed_at": "2024-08-01",
         "profile": "binary_catalyst", "hit": True, "hit_window_days": 30,
         "anchor_close": 100.0, "windows": []},
        # e2 — MISS
        {"event_id": "e2", "ticker": "VRDN", "filed_at": "2024-09-15",
         "profile": "binary_catalyst", "hit": False, "hit_window_days": 30,
         "miss_reason": "no_hit_30d", "windows": []},
        # e3 — UNRESOLVED with sentinel ticker
        {"event_id": "e3", "ticker": "?", "filed_at": "2024-10-01",
         "profile": "binary_catalyst", "hit": None,
         "miss_reason": "unresolved_ticker_sentinel:?"},
        # e4 — UNRESOLVED with bad date
        {"event_id": "e4", "ticker": "BAD", "filed_at": "not-a-date",
         "profile": "binary_catalyst", "hit": None,
         "miss_reason": "unparseable_filed_at"},
    ]
    seen: Dict[str, int] = {"calls": 0}

    def _stub(events, profile, *, limit=None):
        seen["calls"] += 1
        return canned[: len(list(events))]

    monkeypatch.setattr(
        "modal_workers.scripts.label_forward_returns.label_ledger", _stub,
    )
    return seen


def test_seed_emits_staging_with_summary(
    fake_events_file: Path, fake_labeler, tmp_path: Path,
):
    from modal_workers.scripts.seed_eval_harness_from_export import seed

    out = tmp_path / "staging.json"
    summary = seed(
        events_path=fake_events_file, output_path=out, dry_run=False,
    )
    assert summary["events_seen"] == 4
    assert summary["labels_resolved"] == 2  # e1 HIT + e2 MISS
    assert summary["by_hit"]["HIT"] == 1
    assert summary["by_hit"]["MISS"] == 1
    assert summary["by_hit"]["UNRESOLVED"] == 2
    # Skip categories: e4 unparseable_date, e3 'other' (sentinel ticker)
    assert summary["by_skip"].get("unparseable_date", 0) >= 1


def test_seed_writes_staging_records_with_correct_shape(
    fake_events_file: Path, fake_labeler, tmp_path: Path,
):
    from modal_workers.scripts.seed_eval_harness_from_export import seed

    out = tmp_path / "staging.json"
    seed(events_path=fake_events_file, output_path=out, dry_run=False)
    payload = json.loads(out.read_text())
    assert payload["_meta"]["profile"] == "binary_catalyst"
    assert len(payload["staging"]) == 4
    rec = payload["staging"][0]
    assert set(rec.keys()) >= {
        "event_id", "ticker", "filed_at", "profile", "label",
        "skip_category", "asset_id", "document_set",
        "tradeable_filter_pass",
    }
    # Phase 4B contract: these are null on staging output.
    assert rec["asset_id"] is None
    assert rec["document_set"] is None
    assert rec["tradeable_filter_pass"] is None


def test_seed_dry_run_does_not_write(
    fake_events_file: Path, fake_labeler, tmp_path: Path,
):
    from modal_workers.scripts.seed_eval_harness_from_export import seed

    out = tmp_path / "should_not_exist.json"
    summary = seed(
        events_path=fake_events_file, output_path=out, dry_run=True,
    )
    assert summary["events_seen"] == 4
    assert not out.exists()


def test_seed_limit_truncates_events(
    fake_events_file: Path, fake_labeler, tmp_path: Path,
):
    from modal_workers.scripts.seed_eval_harness_from_export import seed

    out = tmp_path / "staging.json"
    summary = seed(
        events_path=fake_events_file, output_path=out, limit=2,
    )
    assert summary["events_seen"] == 2
    payload = json.loads(out.read_text())
    assert len(payload["staging"]) == 2


def test_categorize_skip_maps_known_reasons():
    from modal_workers.scripts.seed_eval_harness_from_export import (
        _categorize_skip,
    )
    assert _categorize_skip({"hit": True}) is None
    assert _categorize_skip({"hit": False}) is None
    assert _categorize_skip(
        {"hit": None, "miss_reason": "no_price_data"}) == "no_price_data"
    assert _categorize_skip(
        {"hit": None, "miss_reason": "anchor_unresolved"}) == "anchor_unresolved"
    assert _categorize_skip(
        {"hit": None, "miss_reason": "unparseable_filed_at"}) == "unparseable_date"
    assert _categorize_skip(
        {"hit": None, "miss_reason": "no_spy_data"}) == "no_spy"
    assert _categorize_skip(
        {"hit": None, "miss_reason": "ma_outcome_pending"}) == "ma_outcome_pending"
    assert _categorize_skip(
        {"hit": None, "miss_reason": "totally_unknown"}) == "other"
