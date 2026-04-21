"""Regression tests for migrations/import_candidates.py.

These lock the legacy-candidate reconcile path so curated dossiers like AXSM,
VERA, and VRDN remain recoverable in Supabase after accidental row removal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from migrations import import_candidates
from modal_workers.shared.supabase_client import SupabaseClient


def _specs_by_ticker(*tickers: str) -> Dict[str, Dict[str, Any]]:
    return {
        spec["ticker"]: spec
        for spec in import_candidates.load_legacy_candidate_specs(list(tickers))
    }


def _make_client(monkeypatch, dispatcher):
    captured: List[Dict[str, Any]] = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append(
            {
                "method": method,
                "path": path,
                "params": params,
                "json_body": json_body,
                "prefer": prefer,
            }
        )
        return dispatcher(method, path, params=params, json_body=json_body, prefer=prefer)

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    client = SupabaseClient.__new__(SupabaseClient)
    client.captured = captured  # type: ignore[attr-defined]
    return client


def test_live_specs_extract_state_date_and_profile_from_legacy_files():
    specs = _specs_by_ticker("AXSM", "VERA", "VRDN")

    assert specs["AXSM"]["state"] == "active"
    assert specs["AXSM"]["next_catalyst_date"] == "2026-04-30"
    assert specs["AXSM"]["scoring_profile"] == "binary_catalyst"
    assert specs["AXSM"]["current_score"] == 30.75

    assert specs["VERA"]["state"] == "active"
    assert specs["VERA"]["next_catalyst_date"] == "2026-07-07"
    assert specs["VERA"]["scoring_profile"] == "binary_catalyst"
    assert specs["VERA"]["current_score"] == 30.5

    # Curated legacy state stays authoritative even though the dossier text was
    # later demoted to watchlist in the legacy file bus.
    assert specs["VRDN"]["state"] == "active"
    assert specs["VRDN"]["next_catalyst_date"] == "2026-06-30"
    assert specs["VRDN"]["scoring_profile"] == "binary_catalyst"
    assert specs["VRDN"]["current_score"] == 26.0


def test_build_candidate_upsert_rows_reuses_existing_mic_and_preserves_kills():
    spec = _specs_by_ticker("AXSM")["AXSM"]
    existing = {
        "AXSM": [
            {
                "ticker": "AXSM",
                "mic": "XNAS",
                "entity_id": "ent-axsm",
                "state": "killed",
                "scoring_profile": "binary_catalyst",
                "current_score": 29.0,
                "current_band": "watchlist",
                "kill_conditions": [{"id": "K1", "status": "pending"}],
                "extensions": {"foo": "bar"},
                "next_catalyst_date": "2026-01-01",
                "next_catalyst_window": None,
                "dossier_markdown": "stale",
                "updated_at": "2026-04-01T00:00:00Z",
            }
        ]
    }

    rows, summary = import_candidates.build_candidate_upsert_rows([spec], existing, {})

    assert len(rows) == 1
    row = rows[0]
    assert row["ticker"] == "AXSM"
    assert row["mic"] == "XNAS"
    assert row["entity_id"] == "ent-axsm"
    assert row["state"] == "active"
    assert row["current_band"] == "watchlist"
    assert row["kill_conditions"] == [{"id": "K1", "status": "pending"}]
    assert row["extensions"]["foo"] == "bar"
    assert row["extensions"]["legacy_import"]["authoritative_state"] == "active"

    assert summary["restored"] == []
    assert summary["state_resets"] == ["AXSM"]
    assert summary["updated"] == ["AXSM"]


def test_reconcile_candidates_upserts_candidates_only(monkeypatch, tmp_path: Path):
    spec = _specs_by_ticker("AXSM")["AXSM"]
    monkeypatch.setattr(import_candidates, "WORKING", tmp_path)
    monkeypatch.setattr(import_candidates, "load_legacy_candidate_specs", lambda tickers=None: [spec])

    def dispatch(method, path, *, params=None, json_body=None, prefer=None):
        if method == "GET" and path == "candidates":
            return []
        if method == "GET" and path == "entities":
            return [{"id": "ent-axsm", "primary_ticker": "AXSM", "primary_mic": "XNAS"}]
        if method == "POST" and path == "candidates":
            return json_body
        raise AssertionError(f"unexpected call: {method} {path}")

    client = _make_client(monkeypatch, dispatch)
    result = import_candidates.reconcile_candidates(client=client, dry_run=False, tickers=["AXSM"])

    post_calls = [call for call in client.captured if call["method"] == "POST"]
    assert len(post_calls) == 1
    assert post_calls[0]["path"] == "candidates"
    assert post_calls[0]["params"]["on_conflict"] == "ticker,mic"
    assert all(call["path"] != "candidate_events" for call in client.captured)
    assert result["restored"] == ["AXSM"]
    assert result["upserted"] == 1
    assert Path(result["report_path"]).exists()
