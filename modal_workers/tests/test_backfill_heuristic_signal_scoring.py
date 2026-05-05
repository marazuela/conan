"""
Tests for the heuristic-scoring backfill script contracts (4.1 / 4.3 / 4.4).

We do not exercise the full backfill() integration here — that requires a live
Supabase. These tests lock:
  - `extensions.backfill` stamping (batch_id, run_at, script_version)
  - metrics delta computation
  - operator_flags `backfill_metrics_snapshot` payload shape
  - `--mode` argument parsing
"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "migrations"
if str(MIGRATIONS) not in sys.path:
    sys.path.insert(0, str(MIGRATIONS))

import backfill_heuristic_signal_scoring as bf  # noqa: E402


# ----------------------------------------------------------------------
# 4.1 — batch_id stamping
# ----------------------------------------------------------------------

def test_merge_extensions_stamps_backfill_block_with_batch_id():
    merged = bf._merge_extensions(
        existing=None,
        scoring_meta={"provenance": "heuristic"},
        batch_id="batch-abc",
        run_at="2026-04-22T12:00:00Z",
    )
    assert merged["scoring_meta"] == {"provenance": "heuristic"}
    assert merged["backfill"]["batch_id"] == "batch-abc"
    assert merged["backfill"]["run_at"] == "2026-04-22T12:00:00Z"
    assert merged["backfill"]["script_version"] == bf.SCRIPT_VERSION


def test_merge_extensions_preserves_existing_keys():
    existing = {"custom_key": "value", "nested": {"a": 1}}
    merged = bf._merge_extensions(
        existing=existing,
        scoring_meta={"provenance": "unscored"},
        batch_id="batch-xyz",
        run_at="2026-04-22T12:00:00Z",
    )
    assert merged["custom_key"] == "value"
    assert merged["nested"] == {"a": 1}
    assert merged["scoring_meta"] == {"provenance": "unscored"}
    assert merged["backfill"]["batch_id"] == "batch-xyz"


def test_merge_extensions_overwrites_prior_backfill_block():
    """A later run's batch_id must overwrite the previous run's stamp — otherwise
    a replay would carry the OLDER batch_id and break resumability (4.2)."""
    existing = {
        "backfill": {"batch_id": "old-batch", "run_at": "2026-04-01T00:00:00Z",
                     "script_version": "v0"},
    }
    merged = bf._merge_extensions(
        existing=existing,
        scoring_meta={"provenance": "heuristic"},
        batch_id="new-batch",
        run_at="2026-04-22T12:00:00Z",
    )
    assert merged["backfill"]["batch_id"] == "new-batch"
    assert merged["backfill"]["script_version"] == bf.SCRIPT_VERSION


def test_build_backfill_patch_embeds_batch_id_in_extensions():
    row = {
        "signal_id": "sig-1",
        "scoring_profile": "takeover_candidate",
        "raw_payload": {
            "patterns_hit": 4,
            "pattern_names": ["strategic_review"],
            "primary_filing": {"file_date": "2026-04-20"},
            "pe_filer_type": "strategic",
            "pe_filer_name": "BigCorp",
        },
        "extensions": {},
    }
    # entity=None means no market_snapshot fetch; fine for this contract test.
    patch = bf._build_backfill_patch(
        row, entity=None, client=_NoopSupabase(),
        batch_id="batch-123", run_at="2026-04-22T12:00:00Z",
    )
    assert patch["extensions"]["backfill"]["batch_id"] == "batch-123"
    assert patch["extensions"]["backfill"]["run_at"] == "2026-04-22T12:00:00Z"
    assert patch["extensions"]["scoring_meta"]["provenance"] == "heuristic"


def test_build_backfill_patch_applies_live_heuristic_damper():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = {
        "signal_id": "sig-threshold",
        "scoring_profile": "takeover_candidate",
        "raw_payload": {
            "patterns_hit": 3,
            "pattern_names": ["pe_take_private"],
            "primary_filing": {"file_date": today},
            "pe_filer_type": "family_office",
            "pe_filer_name": "Named Buyer",
            "valuation_discount_pct": 22,
            "adv_usd": 5_000_000,
        },
        "extensions": {},
    }

    patch = bf._build_backfill_patch(
        row, entity=None, client=_NoopSupabase(),
        batch_id="batch-123", run_at="2026-04-22T12:00:00Z",
    )

    assert patch["extensions"]["scoring_meta"]["provenance"] == "heuristic"
    assert patch["extensions"]["scoring_meta"]["requires_resolution"] is False
    assert patch["score"] == 34.2
    assert patch["band"] == "watchlist"


# ----------------------------------------------------------------------
# 4.4 — metrics delta + snapshot flag
# ----------------------------------------------------------------------

def test_metrics_delta_returns_integer_diffs():
    before = {
        "scored_rows": 100, "exact_30_rows": 40, "scored_without_provenance": 25,
        "total_numeric_dims": 600, "numeric_dim_threes": 220,
    }
    after = {
        "scored_rows": 100, "exact_30_rows": 12, "scored_without_provenance": 0,
        "total_numeric_dims": 600, "numeric_dim_threes": 180,
    }
    delta = bf._metrics_delta(before, after)
    assert delta["scored_rows"] == 0
    assert delta["exact_30_rows"] == -28
    assert delta["scored_without_provenance"] == -25
    assert delta["numeric_dim_threes"] == -40


def test_metrics_delta_handles_missing_keys_as_zero():
    delta = bf._metrics_delta({}, {"scored_rows": 5})
    assert delta["scored_rows"] == 5
    assert delta["exact_30_rows"] == 0


def test_write_metrics_snapshot_flag_posts_expected_shape():
    captured: List[Dict[str, Any]] = []

    class FakeClient:
        def _rest(self, method, path, *, params=None, json_body=None, prefer=None):
            captured.append({
                "method": method, "path": path, "params": params,
                "json_body": json_body, "prefer": prefer,
            })
            return None

    bf._write_metrics_snapshot_flag(
        FakeClient(),
        batch_id="batch-42",
        run_at="2026-04-22T12:00:00Z",
        dry_run=False,
        mode=bf.MODE_ORPHANS_ONLY,
        metrics_before={"scored_rows": 10, "exact_30_rows": 5,
                        "scored_without_provenance": 3,
                        "total_numeric_dims": 60, "numeric_dim_threes": 20},
        metrics_after={"scored_rows": 10, "exact_30_rows": 2,
                       "scored_without_provenance": 0,
                       "total_numeric_dims": 60, "numeric_dim_threes": 15},
        summary={"rows_examined": 10, "rows_updated": 7},
    )

    assert len(captured) == 1
    call = captured[0]
    assert call["method"] == "POST"
    assert call["path"] == "operator_flags"
    body = call["json_body"]
    assert body["severity"] == "info"
    assert body["source"] == "backfill_heuristic_signal_scoring"
    assert body["kind"] == "backfill_metrics_snapshot"
    assert body["evidence"]["batch_id"] == "batch-42"
    assert body["evidence"]["mode"] == bf.MODE_ORPHANS_ONLY
    assert body["evidence"]["metrics_delta"]["exact_30_rows"] == -3
    assert body["evidence"]["metrics_delta"]["numeric_dim_threes"] == -5


def test_write_metrics_snapshot_flag_swallows_supabase_failures(capsys):
    """Flag write failure must never abort the backfill."""

    class BrokenClient:
        def _rest(self, *args, **kwargs):
            raise RuntimeError("PostgREST down")

    bf._write_metrics_snapshot_flag(
        BrokenClient(),
        batch_id="batch-9",
        run_at="2026-04-22T12:00:00Z",
        dry_run=True,
        mode=bf.MODE_ALL,
        metrics_before={}, metrics_after={}, summary={},
    )
    # No exception — warning printed instead.
    captured = capsys.readouterr()
    assert "failed to write backfill_metrics_snapshot" in captured.out


# ----------------------------------------------------------------------
# 4.3 — CLI mode handling
# ----------------------------------------------------------------------

def test_valid_modes_exposes_the_three_documented_modes():
    assert set(bf.VALID_MODES) == {bf.MODE_ALL, bf.MODE_CANDIDATE_LINKED_ONLY, bf.MODE_ORPHANS_ONLY}


def test_backfill_rejects_unknown_mode(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://fake")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake")
    with pytest.raises(SystemExit) as exc:
        bf.backfill(dry_run=True, include_candidate_linked=False,
                    limit=None, mode="nonsense")
    assert "--mode" in str(exc.value)


# ----------------------------------------------------------------------
# 4.2 — DB-backed replay resumability
# ----------------------------------------------------------------------

def test_fetch_batch_replay_candidates_uses_correct_postgrest_filters(monkeypatch):
    """The DB-authoritative query must filter on batch_id + finalizable state
    (non-null score, non-provisional, no convergence stamp)."""
    captured: List[Dict[str, Any]] = []

    class FakeClient:
        def _rest(self, method, path, *, params=None, json_body=None, prefer=None):
            captured.append({"method": method, "path": path, "params": dict(params or {})})
            return []

    bf._fetch_batch_replay_candidates(FakeClient(), batch_id="batch-99")

    assert len(captured) == 1
    params = captured[0]["params"]
    assert params["extensions->backfill->>batch_id"] == "eq.batch-99"
    assert params["score"] == "not.is.null"
    assert params["extensions->scoring_meta->>requires_resolution"] == "eq.false"
    assert params["band_with_bonus"] == "is.null"


def test_retry_reactor_failures_rejects_missing_source(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://fake")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake")
    with pytest.raises(SystemExit) as exc:
        bf._retry_reactor_failures(report_path=None, batch_id=None)
    assert "batch-id" in str(exc.value) or "batch_id" in str(exc.value)


def test_retry_reactor_failures_db_mode_replays_candidates(monkeypatch, tmp_path):
    """`--retry-batch` mode queries signals by batch_id + ready state, replays
    each one through reactor, and does not depend on the report file."""
    monkeypatch.setenv("SUPABASE_URL", "https://fake")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake")

    replay_calls: List[str] = []
    seeded_rows = [
        {"signal_id": "s-db-1", "scoring_profile": "short_positioning"},
        {"signal_id": "s-db-2", "scoring_profile": "takeover_candidate"},
    ]

    def fake_fetch(client, batch_id):
        assert batch_id == "batch-xyz"
        return list(seeded_rows)

    class FakeResp:
        def __init__(self):
            self.ok = True
            self.status_code = 200
            self.text = "ok"

    def fake_replay(client, record):
        replay_calls.append(record["signal_id"])
        return FakeResp()

    # Build a fake SupabaseClient without calling __init__.
    from modal_workers.shared.supabase_client import SupabaseClient
    fake_client = SupabaseClient.__new__(SupabaseClient)
    fake_client.url = "https://fake"
    fake_client.service_key = "fake"

    monkeypatch.setattr(bf, "SupabaseClient", lambda: fake_client)
    monkeypatch.setattr(bf, "_fetch_batch_replay_candidates", fake_fetch)
    monkeypatch.setattr(bf, "_replay_reactor", fake_replay)

    result = bf._retry_reactor_failures(batch_id="batch-xyz")

    assert result["mode"] == "retry_reactor_failures"
    assert result["batch_id"] == "batch-xyz"
    assert result["db_state_candidates"] == 2
    assert result["legacy_report_fallbacks"] == 0
    assert result["retried_ok"] == 2
    assert result["retried_error"] == 0
    assert replay_calls == ["s-db-1", "s-db-2"]


def test_retry_reactor_failures_legacy_path_still_works(monkeypatch, tmp_path):
    """A report file without batch_id must still drive replay via the
    `reactor_failures` list (back-compat for pre-4.1 runs)."""
    monkeypatch.setenv("SUPABASE_URL", "https://fake")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake")

    # Report has no batch_id — simulates a pre-4.1 run.
    report_path = tmp_path / "old_report.json"
    report_path.write_text(json.dumps({
        "reactor_failures": [
            {"signal_id": "s-legacy-1", "status_code": 500, "body": "boom"},
        ],
    }))

    captured_rest: List[Dict[str, Any]] = []

    class FakeClient:
        def __init__(self):
            self.url = "https://fake"
            self.service_key = "fake"

        def _rest(self, method, path, *, params=None, json_body=None, prefer=None):
            captured_rest.append({"method": method, "path": path, "params": params})
            if method == "GET" and path == "signals":
                return [{"signal_id": "s-legacy-1", "scoring_profile": "short_positioning"}]
            return None

    class FakeResp:
        def __init__(self):
            self.ok = False
            self.status_code = 502
            self.text = "still broken"

    replay_calls: List[str] = []

    def fake_replay(client, record):
        replay_calls.append(record["signal_id"])
        return FakeResp()

    monkeypatch.setattr(bf, "SupabaseClient", FakeClient)
    monkeypatch.setattr(bf, "_replay_reactor", fake_replay)

    result = bf._retry_reactor_failures(report_path=report_path)

    assert result["legacy_report_fallbacks"] == 1
    assert result["db_state_candidates"] == 0
    assert result["retried_error"] == 1
    assert replay_calls == ["s-legacy-1"]


def test_retry_reactor_failures_promotes_report_batch_id_to_db_mode(monkeypatch, tmp_path):
    """A report produced by a post-4.1 run carries batch_id. The retry
    function should lift that batch_id and use DB-authoritative replay
    even when the caller passed only --retry-reactor-failures-from."""
    monkeypatch.setenv("SUPABASE_URL", "https://fake")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake")

    report_path = tmp_path / "new_report.json"
    report_path.write_text(json.dumps({
        "batch_id": "batch-from-report",
        "reactor_failures": [],
    }))

    fetched_batches: List[str] = []

    def fake_fetch(client, batch_id):
        fetched_batches.append(batch_id)
        return [{"signal_id": "s-db-only", "scoring_profile": "binary_catalyst"}]

    class FakeClient:
        url = "https://fake"
        service_key = "fake"

        def _rest(self, *a, **k):
            return None

    class FakeResp:
        def __init__(self):
            self.ok = True
            self.status_code = 200
            self.text = "ok"

    monkeypatch.setattr(bf, "SupabaseClient", FakeClient)
    monkeypatch.setattr(bf, "_fetch_batch_replay_candidates", fake_fetch)
    monkeypatch.setattr(bf, "_replay_reactor", lambda c, r: FakeResp())

    result = bf._retry_reactor_failures(report_path=report_path)

    assert fetched_batches == ["batch-from-report"]
    assert result["batch_id"] == "batch-from-report"
    assert result["db_state_candidates"] == 1


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

class _NoopSupabase:
    url = "https://example"
    service_key = "fake"

    def _rest(self, *args, **kwargs):
        return None
