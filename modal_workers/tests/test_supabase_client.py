from __future__ import annotations

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
