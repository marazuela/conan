from __future__ import annotations

from datetime import datetime

import pytest

from modal_workers.shared.supabase_client import SupabaseClient


def test_load_scanner_statuses_bulk_fetches_unique_names(monkeypatch):
    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        assert method == "GET"
        assert path == "scanners"
        assert params == {
            "name": 'in.("asx_scanner","edgar_filing_monitor")',
            "select": "name,status",
        }
        return [
            {"name": "edgar_filing_monitor", "status": "operational"},
            {"name": "asx_scanner", "status": "paused"},
        ]

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)

    client = SupabaseClient.__new__(SupabaseClient)
    statuses = client.load_scanner_statuses([
        "edgar_filing_monitor",
        "asx_scanner",
        "edgar_filing_monitor",
    ])

    assert statuses == {
        "edgar_filing_monitor": "operational",
        "asx_scanner": "paused",
    }


def test_reap_orphan_runs_reconciles_scanner_timeout(monkeypatch):
    rest_calls = []
    updates = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        rest_calls.append({
            "method": method,
            "path": path,
            "params": params,
            "json_body": json_body,
            "prefer": prefer,
        })
        if method == "GET" and path == "scanner_runs":
            return [{
                "id": "run-1",
                "scanner_id": "sc-1",
                "started_at": "2026-04-21T09:00:00+00:00",
            }]
        if method == "PATCH" and path == "scanner_runs":
            return None
        if method == "GET" and path == "scanners":
            return [{"id": "sc-1", "last_run_utc": "2026-04-21T08:00:00+00:00"}]
        raise AssertionError(f"unexpected call {method} {path}")

    def fake_update(self, scanner_id, last_run_utc, last_run_status, last_run_signals):
        updates.append({
            "scanner_id": scanner_id,
            "last_run_utc": last_run_utc,
            "last_run_status": last_run_status,
            "last_run_signals": last_run_signals,
        })

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    monkeypatch.setattr(SupabaseClient, "update_scanner_last_run", fake_update)

    client = SupabaseClient.__new__(SupabaseClient)

    reaped = client.reap_orphan_runs(max_age_seconds=1200)

    assert updates == [{
        "scanner_id": "sc-1",
        "last_run_utc": updates[0]["last_run_utc"],
        "last_run_status": "timeout",
        "last_run_signals": 0,
    }]
    assert reaped == [{
        "id": "run-1",
        "scanner_id": "sc-1",
        "started_at": "2026-04-21T09:00:00+00:00",
        "scanner_reconciled": True,
    }]


# ----------------------------------------------------------------------
# rubric lookup — exact code version vs active admin lookup
# ----------------------------------------------------------------------

def test_load_rubric_version_id_queries_exact_version_when_provided(monkeypatch):
    captured = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({"method": method, "path": path, "params": params})
        return [{"id": "rubric-v1"}]

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    client = SupabaseClient.__new__(SupabaseClient)

    assert client.load_rubric_version_id("takeover_candidate", rubric_version=1) == "rubric-v1"
    assert captured == [{
        "method": "GET",
        "path": "rubrics",
        "params": {
            "profile": "eq.takeover_candidate",
            "select": "id",
            "limit": 1,
            "rubric_version": "eq.1",
            "order": "effective_at.desc,id.desc",
        },
    }]


def test_load_rubric_version_id_orders_active_lookup_defensively(monkeypatch):
    captured = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append(params)
        return [{"id": "rubric-active"}]

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    client = SupabaseClient.__new__(SupabaseClient)

    assert client.load_rubric_version_id("litigation") == "rubric-active"
    assert captured[0]["superseded_at"] == "is.null"
    assert captured[0]["order"] == "rubric_version.desc,effective_at.desc,id.desc"


# ----------------------------------------------------------------------
# insert_signals — pre-filter on signal_id (PK 23505 guard)
# ----------------------------------------------------------------------

def _make_sig(sid: str, hash_seed: str = "") -> dict:
    return {
        "signal_id": sid,
        "source_content_hash": f"sha256:{hash_seed or sid}",
        "scoring_profile": "litigation",
        "scanner_id": "sc-1",
        "score": 30.0,
        "band": "watchlist",
        "auto_caps_triggered": [],
        "raw_payload": {},
        "extensions": {},
    }


def test_insert_signals_empty_batch_returns_empty(monkeypatch):
    rest_calls = []
    def fake_rest(self, method, path, **kw):
        rest_calls.append((method, path))
        return None
    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    client = SupabaseClient.__new__(SupabaseClient)
    assert client.insert_signals([]) == []
    assert rest_calls == []  # short-circuits before any HTTP call


def test_insert_signals_filters_already_existing_signal_ids(monkeypatch):
    """Deterministic-id scanner re-runs: existing signal_ids are filtered out
    BEFORE the bulk insert so PK 23505 can't fire even when source_content_hash
    bypasses the (hash, profile) on_conflict target."""
    captured_post: dict = {}
    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        if method == "GET" and path == "signals":
            return [{"signal_id": "sig_existing_1"}, {"signal_id": "sig_existing_2"}]
        if method == "POST" and path == "signals":
            captured_post["params"] = params
            captured_post["body"] = json_body
            captured_post["prefer"] = prefer
            return [{"signal_id": s["signal_id"]} for s in json_body]
        raise AssertionError(f"unexpected {method} {path}")
    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    client = SupabaseClient.__new__(SupabaseClient)

    inserted = client.insert_signals([
        _make_sig("sig_existing_1"),
        _make_sig("sig_new"),
        _make_sig("sig_existing_2"),
    ])

    assert inserted == ["sig_new"]
    assert captured_post["params"] == {"on_conflict": "source_content_hash,scoring_profile"}
    assert [s["signal_id"] for s in captured_post["body"]] == ["sig_new"]


def test_insert_signals_all_existing_skips_post(monkeypatch):
    """When every proposed signal_id already exists, no POST is emitted."""
    rest_calls = []
    def fake_rest(self, method, path, **kw):
        rest_calls.append((method, path))
        if method == "GET" and path == "signals":
            return [{"signal_id": "sig_a"}, {"signal_id": "sig_b"}]
        raise AssertionError(f"unexpected {method} {path}")
    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    client = SupabaseClient.__new__(SupabaseClient)

    inserted = client.insert_signals([_make_sig("sig_a"), _make_sig("sig_b")])
    assert inserted == []
    assert [c[0] for c in rest_calls] == ["GET"]  # no POST


def test_insert_signals_chunks_in_clause_for_large_batches(monkeypatch):
    """The pre-filter GET chunks at 200 ids to stay under PostgREST URL limits."""
    get_chunks: list = []
    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        if method == "GET" and path == "signals":
            in_clause = params["signal_id"]
            ids = in_clause[len("in.("):-1].split(",")
            get_chunks.append(len(ids))
            return []
        if method == "POST":
            return [{"signal_id": s["signal_id"]} for s in json_body]
        raise AssertionError
    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    client = SupabaseClient.__new__(SupabaseClient)

    batch = [_make_sig(f"sig_{i:04d}") for i in range(450)]
    inserted = client.insert_signals(batch)
    assert len(inserted) == 450
    assert get_chunks == [200, 200, 50]


# ----------------------------------------------------------------------
# upsert_price_snapshot — uses generated-column UNIQUE conflict target
# ----------------------------------------------------------------------

def test_upsert_price_snapshot_targets_subject_kind_key_horizon(monkeypatch):
    """PostgREST cannot infer a partial unique index as a conflict target;
    the upsert must target the (subject_kind, subject_key, horizon_days)
    UNIQUE constraint backed by the generated columns."""
    captured: dict = {}
    def fake_retry(self, method, path, *, params=None, json_body=None, prefer=None):
        captured["method"] = method
        captured["path"] = path
        captured["params"] = params
        captured["body"] = json_body
        captured["prefer"] = prefer
    monkeypatch.setattr(SupabaseClient, "_rest_with_retry", fake_retry)
    client = SupabaseClient.__new__(SupabaseClient)

    client.upsert_price_snapshot({
        "signal_id": "sig_1", "ticker": "AAPL", "thesis_direction": "long",
        "anchor_date": "2026-04-01", "horizon_days": 7, "fetch_status": "ok",
    })
    assert captured["method"] == "POST"
    assert captured["path"] == "signal_price_snapshots"
    assert captured["params"] == {"on_conflict": "subject_kind,subject_key,horizon_days"}
    assert "merge-duplicates" in captured["prefer"]


def test_upsert_price_snapshot_requires_a_subject(monkeypatch):
    monkeypatch.setattr(SupabaseClient, "_rest_with_retry",
                        lambda self, *a, **kw: pytest.fail("should not POST"))
    client = SupabaseClient.__new__(SupabaseClient)
    with pytest.raises(ValueError, match="signal_id OR candidate_id"):
        client.upsert_price_snapshot({"ticker": "AAPL", "horizon_days": 1})


# ----------------------------------------------------------------------
# update_outcome_realized_move — labeled_at must be a real ISO timestamp,
# not the literal string "now()" (which PostgREST writes as text into a
# timestamptz column).
# ----------------------------------------------------------------------

def test_update_outcome_realized_move_sends_iso_labeled_at(monkeypatch):
    captured: dict = {}
    def fake_retry(self, method, path, *, params=None, json_body=None, prefer=None):
        captured["method"] = method
        captured["path"] = path
        captured["params"] = params
        captured["body"] = json_body
    monkeypatch.setattr(SupabaseClient, "_rest_with_retry", fake_retry)
    client = SupabaseClient.__new__(SupabaseClient)

    client.update_outcome_realized_move("c1", 7, 12.5)

    assert captured["method"] == "PATCH"
    assert captured["path"] == "outcomes"
    assert captured["params"] == {"candidate_id": "eq.c1"}
    body = captured["body"]
    assert body["realized_move_7d"] == 12.5
    # Critical: must be a real timestamp, NOT the literal string "now()".
    assert body["labeled_at"] != "now()"
    parsed = datetime.fromisoformat(body["labeled_at"])
    assert parsed.tzinfo is not None  # UTC-aware
