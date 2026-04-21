from __future__ import annotations

from typing import Any, Dict, List

from modal_workers.biotech_enricher import biotech_enrichment_sweep, enrich_biotech_signal
from modal_workers.legal_enricher import enrich_legal_signal, legal_enrichment_sweep
from modal_workers.shared.supabase_client import SupabaseClient


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


def test_enrich_legal_signal_builds_risk_and_regulations():
    signal = {
        "signal_type": "settlement",
        "raw_payload": {
            "title": "Antitrust class action settlement announced",
            "summary": "Major antitrust settlement",
            "nos": "410",
            "source_feed": "litrel",
            "ticker_hint": "ACME",
        },
    }

    out = enrich_legal_signal(signal)

    assert out["schema_version"] == "legal_enrichment_v1"
    assert out["risk_color"] == "red"
    assert out["risk_score"] >= 16
    assert "Antitrust" in out["regulations"]
    assert out["case_family"] == "sec_litigation"
    assert out["procedural_stage"] == "settlement"
    assert out["ticker_hint_present"] is True


def test_legal_enrichment_sweep_merges_extensions(monkeypatch):
    def dispatch(method, path, *, params=None, json_body=None, prefer=None):
        if method == "GET" and path == "signals":
            return [{
                "signal_id": "sig-1",
                "signal_type": "settlement",
                "raw_payload": {
                    "title": "Antitrust settlement filed",
                    "summary": "Settlement summary",
                    "nos": "410",
                    "source_feed": "litrel",
                },
                "extensions": {"existing": True},
            }]
        return None

    client = _make_client(monkeypatch, dispatch)
    result = legal_enrichment_sweep(client)

    assert result["updated"] == 1
    patches = [c for c in client.captured if c["method"] == "PATCH" and c["path"] == "signals"]
    assert len(patches) == 1
    extensions = patches[0]["json_body"]["extensions"]
    assert extensions["existing"] is True
    assert extensions["legal_enrichment"]["schema_version"] == "legal_enrichment_v1"


def test_enrich_biotech_signal_uses_trial_and_history():
    signal = {
        "raw_payload": {
            "status": "active",
            "adcom_vote": {"yes": 10, "no": 2},
            "primary_outcomes": ["Overall survival primary endpoint"],
            "base_rate_key": "oncology",
            "days_until_pdufa": 10,
            "upside_pct": 50.0,
            "downside_pct": 30.0,
            "approval_probability": 0.75,
            "enrichment": {
                "trial": {"status": "COMPLETED"},
                "fda_history": [
                    {"submissions": [{"status": "AP"}]},
                    {"submissions": [{"status": "AP"}]},
                ],
            },
        }
    }

    out = enrich_biotech_signal(signal)

    assert out["schema_version"] == "biotech_enrichment_v1"
    assert out["endpoint_strength_tier"] >= 4
    assert out["sponsor_track_record_tier"] >= 4
    assert out["approval_history_count"] == 2
    assert out["single_primary_endpoint"] is True
    assert out["hard_endpoint_present"] is True
    assert out["ev_inputs_complete"] is True
    assert out["expected_value_pct"] == 30.0


def test_biotech_enrichment_sweep_merges_extensions(monkeypatch):
    def dispatch(method, path, *, params=None, json_body=None, prefer=None):
        if method == "GET" and path == "signals":
            return [{
                "signal_id": "sig-2",
                "signal_type": "pre_phase3_readout",
                "raw_payload": {
                    "status": "ACTIVE_NOT_RECRUITING",
                    "sponsor_class": "INDUSTRY",
                    "primary_outcomes": ["Primary endpoint"],
                    "base_rate_key": "oncology",
                    "patterns_hit": 4,
                    "enrollment": 300,
                    "days_until_readout": 25,
                    "approval_probability": 0.7,
                    "upside_pct": 50.0,
                    "downside_pct": 35.0,
                    "enrichment": {"fda_history": [{"submissions": [{"status": "AP"}]}]},
                },
                "extensions": {"existing": True},
            }]
        return None

    client = _make_client(monkeypatch, dispatch)
    result = biotech_enrichment_sweep(client)

    assert result["updated"] == 1
    patches = [c for c in client.captured if c["method"] == "PATCH" and c["path"] == "signals"]
    assert len(patches) == 1
    extensions = patches[0]["json_body"]["extensions"]
    assert extensions["existing"] is True
    assert extensions["biotech_enrichment"]["schema_version"] == "biotech_enrichment_v1"
    assert extensions["biotech_enrichment"]["industry_sponsored"] is True
    assert extensions["biotech_enrichment"]["ev_inputs_complete"] is True
