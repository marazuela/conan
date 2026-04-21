"""
Tests for modal_workers.observability — regression locks for gap #4 (2026-04-21).

Before the fix, every `client._rest(...)` call in observability.py used wrong kwargs
(`json=` instead of `json_body=`, `headers={}` instead of `prefer=`, `/rest/v1/X`
instead of `X`, and `.json()` chained on already-parsed returns). The PATCH to
scanners.last_probe_* silently sent an empty body; every observability sub-call
raised AttributeError on the first GET, which dispatch_observability swallowed.

These tests assert the correct kwarg contract on every writer path so the bug
cannot silently recur.

Run: python -m pytest modal_workers/tests/test_observability.py -v
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from modal_workers import observability
from modal_workers.shared.supabase_client import SupabaseClient


@pytest.fixture
def fake_client(monkeypatch):
    """Patch SupabaseClient._rest to capture every call; return a naked instance."""
    captured: List[Dict[str, Any]] = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({
            "method": method, "path": path, "params": params,
            "json_body": json_body, "prefer": prefer,
        })
        # Minimal canned responses so callers don't crash.
        if method == "GET" and path == "scanners":
            status_filter = (params or {}).get("status")
            if status_filter == "eq.operational":
                return [{
                    "id": "sc-1", "name": "edgar_filing_monitor",
                    "endpoints": {"primary": "https://example.com/primary"},
                    "config": {},
                }]
            return []
        return None

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    client = SupabaseClient.__new__(SupabaseClient)
    client.captured = captured  # type: ignore[attr-defined]
    return client


def test_scanner_probe_writes_last_probe_fields(fake_client, monkeypatch):
    """scanner_probe must PATCH scanners.last_probe_* with non-empty json_body."""
    monkeypatch.setattr(observability, "_probe_url", lambda url: (200, 42, 128))

    observability.scanner_probe(fake_client)

    patches = [c for c in fake_client.captured
               if c["method"] == "PATCH" and c["path"] == "scanners"]
    assert len(patches) == 1, f"expected 1 PATCH to scanners, got {patches}"
    body = patches[0]["json_body"]
    assert body is not None, "PATCH body must not be None (gap #4 regression)"
    assert body["last_probe_status"] == "ok"
    assert body["last_probe_latency_ms"] == 42
    assert "last_probe_at" in body


def test_scanner_probe_get_uses_bare_resource_path(fake_client, monkeypatch):
    """GET must use path='scanners' — not '/rest/v1/scanners' (double-prefix bug)."""
    monkeypatch.setattr(observability, "_probe_url", lambda url: (200, 42, 128))

    observability.scanner_probe(fake_client)

    gets = [c for c in fake_client.captured if c["method"] == "GET"]
    assert any(c["path"] == "scanners" for c in gets)
    assert not any(c["path"].startswith("/") for c in gets), \
        f"no path should start with /: {[c['path'] for c in gets]}"


def test_upsert_flag_inserts_when_no_open_match(fake_client, monkeypatch):
    """_upsert_flag must GET existing open flag, then POST a new row when none
    exists. Both calls must use json_body= + bare path (gap #4 contract)."""
    monkeypatch.setattr(observability, "_probe_url", lambda url: (0, 99, 0))

    # Force scanner_probe into drift → triggers _upsert_flag.
    observability.scanner_probe(fake_client)

    gets = [c for c in fake_client.captured
            if c["method"] == "GET" and c["path"] == "operator_flags"]
    assert len(gets) >= 1, "must GET existing flag before insert"
    assert gets[0]["params"]["source"] == "eq.scanner_probe"
    assert gets[0]["params"]["kind"] == "eq.endpoint_drift"
    assert gets[0]["params"]["resolved_at"] == "is.null"

    posts = [c for c in fake_client.captured
             if c["method"] == "POST" and c["path"] == "operator_flags"]
    assert len(posts) >= 1, "drift probe must insert a flag"
    post = posts[0]
    assert post["json_body"] is not None, \
        "POST body must not be None (json=row silently dropped = gap #4)"
    assert post["json_body"]["severity"] == "critical"
    assert post["json_body"]["kind"] == "endpoint_drift"
    assert post["prefer"] == "return=representation"


def test_resolve_flag_uses_json_body_and_prefer(fake_client, monkeypatch):
    """_resolve_flag must pass json_body={...} + prefer=..., PATCH operator_flags."""
    monkeypatch.setattr(observability, "_probe_url", lambda url: (200, 42, 128))

    # Healthy probe → _resolve_flag gets called for endpoint_drift + fallback.
    observability.scanner_probe(fake_client)

    resolves = [c for c in fake_client.captured
                if c["method"] == "PATCH" and c["path"] == "operator_flags"]
    assert len(resolves) >= 1
    r = resolves[0]
    assert r["json_body"] is not None
    assert "resolved_at" in r["json_body"]
    assert "resolved_note" in r["json_body"]
    assert r["prefer"] == "return=representation"


def test_summarize_open_flags_uses_bare_path(fake_client):
    """summarize_open_flags GET must use path='operator_flags'."""
    result = observability.summarize_open_flags(fake_client)

    gets = [c for c in fake_client.captured if c["method"] == "GET"]
    assert any(c["path"] == "operator_flags" for c in gets)
    assert result == {"open_flags": {}, "total_open": 0}


def test_substitute_url_template_replaces_date_placeholders():
    """tdnet-style URLs with `{YYYYMMDD}` must be substituted to the probe target."""
    from datetime import datetime, timezone

    day = datetime(2026, 4, 21, 0, 0, tzinfo=timezone.utc)
    assert observability._substitute_url_template(
        "https://www.release.tdnet.info/inbs/I_list_001_{YYYYMMDD}.html", today=day,
    ) == "https://www.release.tdnet.info/inbs/I_list_001_20260421.html"
    assert observability._substitute_url_template(
        "https://ex.com/{YYYY}/{MM}/{DD}/feed.json", today=day,
    ) == "https://ex.com/2026/04/21/feed.json"
    assert observability._substitute_url_template(
        "https://ex.com/{YYYY-MM-DD}/report.csv", today=day,
    ) == "https://ex.com/2026-04-21/report.csv"
    # No placeholders → unchanged.
    assert observability._substitute_url_template(
        "https://ex.com/static.html", today=day,
    ) == "https://ex.com/static.html"


def test_scanner_probe_skips_requires_auth_scanners(monkeypatch):
    """Scanners with config.requires_auth=true (courtlistener, kind) must be
    skipped — probing their API without the token would 401/403 and produce
    noise, not signal."""
    captured = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({"method": method, "path": path, "params": params,
                         "json_body": json_body, "prefer": prefer})
        if method == "GET" and path == "scanners":
            return [{
                "id": "sc-cl", "name": "courtlistener_scanner",
                "endpoints": {"primary": "https://www.courtlistener.com/api/rest/v4/dockets/"},
                "config": {"requires_auth": True},
                "last_run_status": None,
            }]
        return None

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    client = SupabaseClient.__new__(SupabaseClient)

    result = observability.scanner_probe(client)

    assert result["skipped"] == [{"scanner": "courtlistener_scanner",
                                  "reason": "requires_auth (token not provisioned)"}]
    assert result["results"] == []
    # Must PATCH scanners to record the check — status+latency NULL signals
    # "skipped, not drifted" to the dashboard.
    patches = [c for c in captured if c["method"] == "PATCH" and c["path"] == "scanners"]
    assert len(patches) == 1
    assert patches[0]["json_body"]["last_probe_status"] is None
    assert patches[0]["json_body"]["last_probe_latency_ms"] is None
    assert "last_probe_at" in patches[0]["json_body"]


def test_scanner_probe_skips_probe_skip_reason_scanners(monkeypatch):
    """Scanners with config.probe_skip_reason (bse_nse geo-block) must be skipped."""
    captured = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({"method": method, "path": path})
        if method == "GET" and path == "scanners":
            return [{
                "id": "sc-bse", "name": "bse_nse_scanner",
                "endpoints": {"primary": "https://www.nseindia.com/api/corporate-announcements"},
                "config": {"probe_skip_reason": "geo_blocked: NSE blocks Modal EU-West IPs"},
                "last_run_status": None,
            }]
        return None

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    client = SupabaseClient.__new__(SupabaseClient)

    result = observability.scanner_probe(client)

    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["scanner"] == "bse_nse_scanner"
    assert "geo_blocked" in result["skipped"][0]["reason"]


def test_orphan_convergence_sweeper_replays_reactor_per_row(monkeypatch):
    """Each orphan (score set, band_with_bonus NULL) must trigger one POST to
    the reactor edge function with an INSERT-shaped envelope. Failed calls
    raise an operator_flag; clean sweeps auto-resolve prior flags."""

    captured_rest = []
    captured_posts = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured_rest.append({"method": method, "path": path, "params": params,
                              "json_body": json_body, "prefer": prefer})
        if method == "GET" and path == "signals":
            return [
                {"signal_id": "a", "score": 35, "band": "immediate",
                 "band_with_bonus": None, "scoring_profile": "short_positioning"},
                {"signal_id": "b", "score": 20, "band": "watchlist",
                 "band_with_bonus": None, "scoring_profile": "short_positioning"},
            ]
        if method == "GET" and path == "operator_flags":
            return []  # no existing flag
        return None

    class FakeResp:
        def __init__(self, status_code):
            self.status_code = status_code
            self.text = "ok" if 200 <= status_code < 300 else "err"

    def fake_post(url, json=None, headers=None, timeout=None):
        captured_posts.append({"url": url, "json": json, "headers": headers})
        return FakeResp(200)

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    monkeypatch.setattr(observability.requests, "post", fake_post)

    client = SupabaseClient.__new__(SupabaseClient)
    client.url = "https://xvwvwbnxdsjpnealarkh.supabase.co"
    client.service_key = "fake_service_key"
    result = observability.orphan_convergence_sweeper(client)

    assert result["orphans_found"] == 2
    assert result["healed"] == 2
    assert result["failed"] == 0
    assert result["immediate_healed"] == ["a"]  # only signal "a" had band='immediate'
    # Two reactor POSTs, both hitting the /functions/v1/reactor URL
    assert len(captured_posts) == 2
    assert captured_posts[0]["url"].endswith("/functions/v1/reactor")
    # Payload is an INSERT-shaped webhook envelope
    assert captured_posts[0]["json"]["type"] == "INSERT"
    assert captured_posts[0]["json"]["table"] == "signals"
    assert captured_posts[0]["json"]["record"]["signal_id"] == "a"
    # Authorization header uses service key; no webhook secret required if
    # none is configured on the reactor.
    assert captured_posts[0]["headers"]["Authorization"] == "Bearer fake_service_key"


def test_orphan_convergence_sweeper_empty_clears_flag(monkeypatch):
    """When no orphans exist, sweeper auto-resolves any prior flag."""
    rest_calls = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        rest_calls.append({"method": method, "path": path, "params": params,
                           "json_body": json_body})
        if method == "GET":
            return []  # no orphans, no prior flag
        return None

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    client = SupabaseClient.__new__(SupabaseClient)
    client.url = "https://xvwvwbnxdsjpnealarkh.supabase.co"
    client.service_key = "fake_service_key"

    result = observability.orphan_convergence_sweeper(client)
    assert result["orphans_found"] == 0

    # Must attempt to PATCH operator_flags with resolved_at to clear any prior
    # open flag — defensive so a one-time stuck flag doesn't linger.
    resolves = [c for c in rest_calls
                if c["method"] == "PATCH" and c["path"] == "operator_flags"]
    assert len(resolves) == 1
    assert "resolved_at" in resolves[0]["json_body"]


def test_scanner_probe_substitutes_template_in_primary(monkeypatch):
    """tdnet's templated URL must be substituted before _probe_url is called."""
    from datetime import datetime, timezone

    captured_probe_urls = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        if method == "GET" and path == "scanners":
            return [{
                "id": "sc-td", "name": "tdnet_scanner",
                "endpoints": {"primary": "https://ex.com/{YYYYMMDD}/feed.html"},
                "config": {},
                "last_run_status": None,
            }]
        return None

    def fake_probe(url):
        captured_probe_urls.append(url)
        return (200, 50, 128)

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    monkeypatch.setattr(observability, "_probe_url", fake_probe)
    client = SupabaseClient.__new__(SupabaseClient)

    observability.scanner_probe(client)

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    assert captured_probe_urls == [f"https://ex.com/{today}/feed.html"]
