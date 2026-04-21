from __future__ import annotations

from typing import Any, Dict, List

from modal_workers.pre_edge_monitor import pre_edge_monitor
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


def test_pre_edge_monitor_delivers_binary_candidate_on_approval(monkeypatch):
    def dispatch(method, path, *, params=None, json_body=None, prefer=None):
        if method == "GET" and path == "candidates":
            return [{
                "id": "cand-1",
                "ticker": "AXSM",
                "entity_id": "ent-1",
                "state": "active",
                "scoring_profile": "binary_catalyst",
                "current_score": 42,
                "current_band": "immediate",
            }]
        if method == "GET" and path == "signals":
            return [{
                "signal_id": "sig-approved",
                "signal_type": "fda_decision",
                "scoring_profile": "binary_catalyst",
                "source_url": "https://example.com/fda",
                "scan_date": "2026-04-21T00:00:00Z",
                "raw_payload": {"status": "approved"},
            }]
        if method == "POST" and path == "rpc/candidate_transition_apply":
            return {"applied": True}
        return None

    client = _make_client(monkeypatch, dispatch)
    result = pre_edge_monitor(client)

    assert result["transition_count"] == 1
    transition = result["transitions"][0]
    assert transition["to_state"] == "delivered"
    assert transition["reason"] == "binary_catalyst_approved"

    rpc_calls = [c for c in client.captured if c["method"] == "POST" and c["path"] == "rpc/candidate_transition_apply"]
    assert len(rpc_calls) == 1
    body = rpc_calls[0]["json_body"]
    assert body["p_candidate_id"] == "cand-1"
    assert body["p_new_state"] == "delivered"
    assert body["p_outcome_type"] == "delivered"
    assert body["p_source"] == "pre_edge_monitor"


def test_pre_edge_monitor_delivers_takeover_candidate_on_merger_arb_signal(monkeypatch):
    def dispatch(method, path, *, params=None, json_body=None, prefer=None):
        if method == "GET" and path == "candidates":
            return [{
                "id": "cand-2",
                "ticker": "RPAY",
                "entity_id": "ent-2",
                "state": "watch",
                "scoring_profile": "takeover_candidate",
                "current_score": 36,
                "current_band": "immediate",
            }]
        if method == "GET" and path == "signals":
            return [{
                "signal_id": "sig-mna",
                "signal_type": "rule_2_7_firm_offer",
                "scoring_profile": "merger_arb",
                "source_url": "https://example.com/mna",
                "scan_date": "2026-04-21T00:00:00Z",
                "raw_payload": {},
            }]
        if method == "POST" and path == "rpc/candidate_transition_apply":
            return {"applied": True}
        return None

    client = _make_client(monkeypatch, dispatch)
    result = pre_edge_monitor(client)

    assert result["transition_count"] == 1
    transition = result["transitions"][0]
    assert transition["to_state"] == "delivered"
    assert transition["reason"] == "takeover_candidate_promoted_to_merger_arb"


def test_pre_edge_monitor_flags_ambiguous_binary_resolution(monkeypatch):
    def dispatch(method, path, *, params=None, json_body=None, prefer=None):
        if method == "GET" and path == "candidates":
            return [{
                "id": "cand-3",
                "ticker": "VRDN",
                "entity_id": "ent-3",
                "state": "active",
                "scoring_profile": "binary_catalyst",
                "current_score": 40,
                "current_band": "immediate",
            }]
        if method == "GET" and path == "signals":
            return [
                {
                    "signal_id": "sig-approved",
                    "signal_type": "fda_decision",
                    "scoring_profile": "binary_catalyst",
                    "source_url": "https://example.com/approval",
                    "scan_date": "2026-04-21T00:00:00Z",
                    "raw_payload": {"status": "approved"},
                },
                {
                    "signal_id": "sig-crl",
                    "signal_type": "fda_decision",
                    "scoring_profile": "binary_catalyst",
                    "source_url": "https://example.com/crl",
                    "scan_date": "2026-04-21T00:10:00Z",
                    "raw_payload": {"status": "resolved_crl"},
                },
            ]
        if method == "GET" and path == "operator_flags":
            return []
        if method == "POST" and path == "operator_flags":
            return [{"id": "flag-1"}]
        return None

    client = _make_client(monkeypatch, dispatch)
    result = pre_edge_monitor(client)

    assert result["transition_count"] == 0
    assert result["flag_count"] == 1
    posts = [c for c in client.captured if c["method"] == "POST" and c["path"] == "operator_flags"]
    assert len(posts) == 1
    assert posts[0]["json_body"]["kind"] == "review_required"
    assert posts[0]["json_body"]["candidate_id"] == "cand-3"
