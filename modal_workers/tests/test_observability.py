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
            # scanner_probe now widens to the full runnable set (operational +
            # shadow + shadow_with_emit). Return the canned row for the
            # in-list filter; legacy eq.operational still supported for tests
            # that haven't been updated.
            if status_filter and (
                status_filter == "eq.operational"
                or status_filter.startswith("in.(")
                and "operational" in status_filter
            ):
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


def test_record_snapshot_fetch_failure_upserts_flag_with_ticker_evidence(fake_client):
    """Scanner-side snapshot failure → operator_flags row with scanner-scoped
    source + ticker in evidence."""
    observability.record_snapshot_fetch_failure(
        fake_client,
        scanner_name="esma_short_scanner",
        ticker="ACME",
        exc=RuntimeError("yfinance 429 Too Many Requests"),
    )

    gets = [c for c in fake_client.captured
            if c["method"] == "GET" and c["path"] == "operator_flags"]
    assert len(gets) == 1
    assert gets[0]["params"]["source"] == "eq.scanner:esma_short_scanner"
    assert gets[0]["params"]["kind"] == "eq.market_snapshot_fetch_failed"

    posts = [c for c in fake_client.captured
             if c["method"] == "POST" and c["path"] == "operator_flags"]
    assert len(posts) == 1
    body = posts[0]["json_body"]
    assert body["severity"] == "info"
    assert body["source"] == "scanner:esma_short_scanner"
    assert body["kind"] == "market_snapshot_fetch_failed"
    assert body["evidence"]["ticker"] == "ACME"
    assert body["evidence"]["error_type"] == "RuntimeError"
    assert "429" in body["evidence"]["error_message"]


def test_record_snapshot_fetch_failure_swallows_flag_write_errors(monkeypatch):
    """Flag writing must NEVER break the scanner loop — even if _upsert_flag
    raises, the helper returns normally."""

    def boom(*args, **kwargs):
        raise RuntimeError("supabase down")

    monkeypatch.setattr(observability, "_upsert_flag", boom)

    observability.record_snapshot_fetch_failure(
        None,  # type: ignore[arg-type]
        scanner_name="takeover_candidate_scanner",
        ticker="XYZ",
        exc=ValueError("parse fail"),
    )  # must not raise


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


def test_scanner_probe_queries_runnable_status_set(monkeypatch):
    """scanner_probe must query operational + shadow + shadow_with_emit, not
    just operational. The fda_signal_bridge runs in shadow lifecycle stage and
    still needs probe coverage."""
    captured = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({"method": method, "path": path, "params": params})
        return []

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    client = SupabaseClient.__new__(SupabaseClient)
    observability.scanner_probe(client)

    initial_get = next(
        c for c in captured if c["method"] == "GET" and c["path"] == "scanners"
    )
    status_filter = initial_get["params"]["status"]
    assert status_filter.startswith("in.("), \
        f"expected status=in.(...) filter, got {status_filter!r}"
    for stage in ("operational", "shadow", "shadow_with_emit"):
        assert stage in status_filter, f"{stage} missing from {status_filter!r}"


def test_scanner_probe_records_skip_when_no_primary_endpoint(monkeypatch):
    """Scanners whose endpoints jsonb lacks a `primary` key (e.g. fda_signal_bridge
    with provider-named keys) must be recorded as skipped with reason
    'no_primary_endpoint', not silently dropped. F-206 regression lock."""
    captured = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({"method": method, "path": path, "params": params,
                         "json_body": json_body, "prefer": prefer})
        if method == "GET" and path == "scanners":
            return [{
                "id": "sc-bridge", "name": "fda_signal_bridge",
                "endpoints": {"polygon": "https://api.polygon.io",
                              "federal_register": "https://www.federalregister.gov/api/v1/documents"},
                "config": {},
                "last_run_status": None,
            }]
        return None

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    client = SupabaseClient.__new__(SupabaseClient)

    result = observability.scanner_probe(client)

    assert result["skipped"] == [{"scanner": "fda_signal_bridge",
                                  "reason": "no_primary_endpoint"}]
    assert result["results"] == []
    # Probe evaluation must be recorded so the dashboard can distinguish
    # "evaluated but no probe target" from "silently missed".
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


def test_provisional_convergence_audit_flags_violators(monkeypatch):
    """Rows with requires_resolution=true AND band_with_bonus stamped are
    invariant violations; sweeper must raise an error flag, never auto-fix."""
    captured_rest = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured_rest.append({"method": method, "path": path, "params": params,
                              "json_body": json_body, "prefer": prefer})
        if method == "GET" and path == "signals":
            return [
                {"signal_id": "s1", "scoring_profile": "short_positioning",
                 "band_with_bonus": "immediate", "score_with_bonus": 36.5},
                {"signal_id": "s2", "scoring_profile": "takeover_candidate",
                 "band_with_bonus": "watchlist", "score_with_bonus": 28.0},
                {"signal_id": "s3", "scoring_profile": "short_positioning",
                 "band_with_bonus": "archive", "score_with_bonus": 18.0},
            ]
        if method == "GET" and path == "operator_flags":
            return []  # no existing flag → INSERT path
        return None

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    client = SupabaseClient.__new__(SupabaseClient)
    client.url = "https://xvwvwbnxdsjpnealarkh.supabase.co"
    client.service_key = "fake_service_key"

    result = observability.provisional_convergence_audit(client)

    assert result["violators_found"] == 3
    assert set(result["sample_signal_ids"]) == {"s1", "s2", "s3"}
    assert result["per_profile"]["short_positioning"] == 2
    assert result["per_profile"]["takeover_candidate"] == 1

    # Must use the nested JSONB filter path — this is the contract with PostgREST.
    get_signals = [c for c in captured_rest
                   if c["method"] == "GET" and c["path"] == "signals"]
    assert len(get_signals) == 1
    params = get_signals[0]["params"]
    assert params["extensions->scoring_meta->>requires_resolution"] == "eq.true"
    assert params["band_with_bonus"] == "not.is.null"

    # Must insert an error-severity flag, not warn/info.
    posts = [c for c in captured_rest
             if c["method"] == "POST" and c["path"] == "operator_flags"]
    assert len(posts) == 1
    assert posts[0]["json_body"]["severity"] == "error"
    assert posts[0]["json_body"]["kind"] == "provisional_converged_invariant_violated"


def test_thesis_jobs_sla_sweeper_flags_each_breaching_status(monkeypatch):
    """Rows past each status's threshold raise a per-status flag; scoring rows
    are auto-reset to needs_scoring with incremented attempt_count."""
    captured = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({"method": method, "path": path, "params": params,
                         "json_body": json_body, "prefer": prefer})
        if method == "GET" and path == "thesis_jobs":
            status = (params or {}).get("status", "")
            if status == "eq.needs_scoring":
                return [
                    {"id": "j1", "signal_id": "s1", "status": "needs_scoring",
                     "updated_at": "2026-04-22T10:00:00Z", "attempt_count": 0,
                     "signals": {"scoring_profile": "short_positioning"}},
                ]
            if status == "eq.scoring":
                return [
                    {"id": "j2", "signal_id": "s2", "status": "scoring",
                     "updated_at": "2026-04-22T10:30:00Z", "attempt_count": 0,
                     "signals": {"scoring_profile": "takeover_candidate"}},
                    {"id": "j3", "signal_id": "s3", "status": "scoring",
                     "updated_at": "2026-04-22T10:30:00Z", "attempt_count": 2,
                     "signals": {"scoring_profile": "binary_catalyst"}},
                ]
            return []
        if method == "GET" and path == "operator_flags":
            return []  # no existing flags
        return None

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    client = SupabaseClient.__new__(SupabaseClient)
    client.url = "https://fake"
    client.service_key = "fake"

    result = observability.thesis_jobs_sla_sweeper(client)

    # One flag per breaching status.
    assert result["breaches_by_status"]["needs_scoring"] == 1
    assert result["breaches_by_status"]["scoring"] == 2
    # j2 (attempt_count=0) gets reset, j3 (attempt_count=2) goes to dlq on this attempt.
    assert result["scoring_resets"] == 1
    assert result["scoring_dlqs"] == 1

    # Verify auto-reset patch went to needs_scoring with incremented attempt_count.
    patches = [c for c in captured if c["method"] == "PATCH" and c["path"] == "thesis_jobs"]
    reset_patch = next(p for p in patches if p["json_body"].get("status") == "needs_scoring")
    assert reset_patch["json_body"]["attempt_count"] == 1
    assert reset_patch["params"]["id"] == "eq.j2"

    # Verify dlq patch for the 3rd-attempt row.
    dlq_patch = next(p for p in patches if p["json_body"].get("status") == "dlq")
    assert dlq_patch["json_body"]["attempt_count"] == 3
    assert dlq_patch["params"]["id"] == "eq.j3"

    # Scoring flag must be severity=error because at least one row was DLQ'd.
    scoring_flag_posts = [
        c for c in captured
        if c["method"] == "POST" and c["path"] == "operator_flags"
        and c["json_body"].get("kind") == "sla_breach_scoring"
    ]
    assert len(scoring_flag_posts) == 1
    assert scoring_flag_posts[0]["json_body"]["severity"] == "error"
    # needs_scoring flag is warn (not auto-acted).
    ns_flag_posts = [
        c for c in captured
        if c["method"] == "POST" and c["path"] == "operator_flags"
        and c["json_body"].get("kind") == "sla_breach_needs_scoring"
    ]
    assert len(ns_flag_posts) == 1
    assert ns_flag_posts[0]["json_body"]["severity"] == "warn"
    # Evidence carries sample with profile joined from signals.
    sample = ns_flag_posts[0]["json_body"]["evidence"]["sample"]
    assert sample[0]["scoring_profile"] == "short_positioning"


def test_thesis_jobs_sla_sweeper_resolves_status_flags_when_clean(monkeypatch):
    """Zero breaches in a given status → PATCH any open flag of that kind to resolved."""
    captured = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({"method": method, "path": path, "params": params,
                         "json_body": json_body})
        if method == "GET":
            return []  # no breaches
        return None

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    client = SupabaseClient.__new__(SupabaseClient)
    client.url = "https://fake"
    client.service_key = "fake"

    result = observability.thesis_jobs_sla_sweeper(client)
    assert all(count == 0 for count in result["breaches_by_status"].values())
    # F-216: aged sweep also runs and finds zero rows.
    assert result["needs_scoring_aged_count"] == 0

    # Five PATCH calls: one per SLA status + the F-216 aged-sweep kind.
    resolves = [c for c in captured
                if c["method"] == "PATCH" and c["path"] == "operator_flags"]
    resolved_kinds = {c["params"].get("kind") for c in resolves}
    assert resolved_kinds == {
        "eq.sla_breach_needs_scoring", "eq.sla_breach_scoring",
        "eq.sla_breach_queued", "eq.sla_breach_drafting",
        "eq.thesis_jobs_needs_scoring_aged",
    }


def test_thesis_jobs_sla_sweeper_aged_needs_scoring_flag(monkeypatch):
    """F-216: rows in needs_scoring older than the aged threshold (created_at,
    not updated_at) raise a `thesis_jobs_needs_scoring_aged` flag — but are
    NOT auto-reset (this is a 'check signal_resolver health' alert, not a
    retry-eligible state)."""
    captured = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({"method": method, "path": path, "params": params,
                         "json_body": json_body, "prefer": prefer})
        if method == "GET" and path == "thesis_jobs":
            params = params or {}
            # Aged sweep: status=eq.needs_scoring AND a created_at filter is set.
            if "created_at" in params:
                return [
                    {"id": "old-1", "signal_id": "s-old-1",
                     "status": "needs_scoring",
                     "created_at": "2026-04-01T10:00:00Z",
                     "updated_at": "2026-05-06T10:00:00Z",
                     "attempt_count": 0,
                     "signals": {"scoring_profile": "binary_catalyst"}},
                    {"id": "old-2", "signal_id": "s-old-2",
                     "status": "needs_scoring",
                     "created_at": "2026-04-15T10:00:00Z",
                     "updated_at": "2026-05-06T10:00:00Z",
                     "attempt_count": 1,
                     "signals": {"scoring_profile": "fda_event"}},
                ]
            return []  # no rows for the regular per-status SLA loop
        if method == "GET" and path == "operator_flags":
            return []
        return None

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    client = SupabaseClient.__new__(SupabaseClient)
    client.url = "https://fake"
    client.service_key = "fake"

    result = observability.thesis_jobs_sla_sweeper(client)

    # Aged sweep counted exactly the seeded rows.
    assert result["needs_scoring_aged_count"] == 2
    # No PATCH on thesis_jobs — aged sweep MUST NOT auto-reset/dlq.
    job_patches = [c for c in captured
                   if c["method"] == "PATCH" and c["path"] == "thesis_jobs"]
    assert job_patches == []
    # Exactly one POST to operator_flags with the aged-flag kind.
    aged_posts = [
        c for c in captured
        if c["method"] == "POST" and c["path"] == "operator_flags"
        and c["json_body"].get("kind") == "thesis_jobs_needs_scoring_aged"
    ]
    assert len(aged_posts) == 1
    flag_body = aged_posts[0]["json_body"]
    assert flag_body["severity"] == "warn"
    assert flag_body["evidence"]["aged_count"] == 2
    assert flag_body["evidence"]["threshold_days"] == 7
    sample = flag_body["evidence"]["sample"]
    assert {row["job_id"] for row in sample} == {"old-1", "old-2"}
    assert {row["scoring_profile"] for row in sample} == {
        "binary_catalyst", "fda_event",
    }


def test_thesis_jobs_sla_sweeper_aged_resolves_when_clean(monkeypatch):
    """F-216: zero aged rows → existing aged-flag is auto-resolved."""
    captured = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({"method": method, "path": path, "params": params,
                         "json_body": json_body})
        if method == "GET":
            return []
        return None

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    client = SupabaseClient.__new__(SupabaseClient)
    client.url = "https://fake"
    client.service_key = "fake"

    result = observability.thesis_jobs_sla_sweeper(client)
    assert result["needs_scoring_aged_count"] == 0

    aged_resolves = [
        c for c in captured
        if c["method"] == "PATCH" and c["path"] == "operator_flags"
        and c["params"].get("kind") == "eq.thesis_jobs_needs_scoring_aged"
    ]
    assert len(aged_resolves) == 1


def test_summarize_provisional_backlog_counts_by_profile(monkeypatch):
    """Returns a per-profile counter of heuristic rows waiting on resolution."""
    captured = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({"method": method, "path": path, "params": params})
        if method == "GET" and path == "signals":
            # Simulate a single page returning all four rows.
            return [
                {"scoring_profile": "short_positioning"},
                {"scoring_profile": "short_positioning"},
                {"scoring_profile": "takeover_candidate"},
                {"scoring_profile": "binary_catalyst"},
            ]
        return None

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    client = SupabaseClient.__new__(SupabaseClient)
    client.url = "https://fake"
    client.service_key = "fake"

    result = observability.summarize_provisional_backlog(client)
    assert result["total_provisional"] == 4
    assert result["provisional_by_profile"] == {
        "short_positioning": 2, "takeover_candidate": 1, "binary_catalyst": 1,
    }

    # The JSON filter is the contract the reactor-gate depends on.
    params = captured[0]["params"]
    assert params["extensions->scoring_meta->>requires_resolution"] == "eq.true"
    assert params["band_with_bonus"] == "is.null"


def test_provisional_convergence_audit_empty_clears_flag(monkeypatch):
    """Zero violators → auto-resolve any prior flag (steady-state contract)."""
    rest_calls = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        rest_calls.append({"method": method, "path": path, "params": params,
                           "json_body": json_body})
        if method == "GET":
            return []
        return None

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    client = SupabaseClient.__new__(SupabaseClient)
    client.url = "https://xvwvwbnxdsjpnealarkh.supabase.co"
    client.service_key = "fake_service_key"

    result = observability.provisional_convergence_audit(client)
    assert result["violators_found"] == 0

    resolves = [c for c in rest_calls
                if c["method"] == "PATCH" and c["path"] == "operator_flags"]
    assert len(resolves) == 1
    assert "resolved_at" in resolves[0]["json_body"]


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


def test_edgar_runtime_health_raises_flag_for_repeated_degraded_runs(monkeypatch):
    captured = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({
            "method": method,
            "path": path,
            "params": params,
            "json_body": json_body,
            "prefer": prefer,
        })
        if method == "GET" and path == "scanners":
            return [{"id": "sc-edgar", "name": "edgar_filing_monitor"}]
        if method == "GET" and path == "scanner_runs":
            return [
                {
                    "status": "partial",
                    "signals_emitted": 0,
                    "started_at": "2026-04-21T12:00:00Z",
                    "completed_at": "2026-04-21T12:00:20Z",
                    "errors": [{"metrics": {"budget_exhausted": True, "partial_reasons": ["budget_exhausted_keyword_phase"], "degraded": True}}],
                },
                {
                    "status": "partial",
                    "signals_emitted": 0,
                    "started_at": "2026-04-21T09:00:00Z",
                    "completed_at": "2026-04-21T09:00:20Z",
                    "errors": [{"metrics": {"budget_exhausted": True, "partial_reasons": ["budget_exhausted_filing_phase"], "degraded": True}}],
                },
            ]
        if method == "GET" and path == "operator_flags":
            return []
        return None

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    client = SupabaseClient.__new__(SupabaseClient)

    result = observability.edgar_runtime_health(client)

    assert result["flagged"] is True
    posts = [c for c in captured if c["method"] == "POST" and c["path"] == "operator_flags"]
    assert len(posts) == 1
    assert posts[0]["json_body"]["source"] == "edgar_runtime_health"
    assert posts[0]["json_body"]["kind"] == "degraded_run_streak"
    assert posts[0]["json_body"]["scanner_id"] == "sc-edgar"


def test_edgar_runtime_health_resolves_flag_when_runs_are_healthy(monkeypatch):
    captured = []

    def fake_rest(self, method, path, *, params=None, json_body=None, prefer=None):
        captured.append({
            "method": method,
            "path": path,
            "params": params,
            "json_body": json_body,
            "prefer": prefer,
        })
        if method == "GET" and path == "scanners":
            return [{"id": "sc-edgar", "name": "edgar_filing_monitor"}]
        if method == "GET" and path == "scanner_runs":
            return [
                {
                    "status": "ok",
                    "signals_emitted": 7,
                    "started_at": "2026-04-21T12:00:00Z",
                    "completed_at": "2026-04-21T12:00:20Z",
                    "errors": [{"metrics": {"budget_exhausted": False, "degraded": False}}],
                },
            ]
        return []

    monkeypatch.setattr(SupabaseClient, "_rest", fake_rest)
    client = SupabaseClient.__new__(SupabaseClient)

    result = observability.edgar_runtime_health(client)

    assert result["flagged"] is False
    patches = [c for c in captured if c["method"] == "PATCH" and c["path"] == "operator_flags"]
    assert len(patches) == 1
    assert patches[0]["params"]["source"] == "eq.edgar_runtime_health"
    assert patches[0]["params"]["kind"] == "eq.degraded_run_streak"
