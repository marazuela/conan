"""Phase 4 tests: Form 4 reroute + new 13D/13G scanner.

Locks down the two scanner deliverables:

1. Form 4 (insider_form4_scanner.py) routes signals on FDA-tracked tickers
   to scoring_profile='binary_catalyst' with extensions.signal_category=
   'insider_activity'. Non-tracked tickers keep the v3 default
   (short_positioning).

2. New edgar_13d_13g_scanner.py emits binary_catalyst signals for
   shareholder structure changes (SC 13D/13G filings) on FDA-tracked
   tickers only. MVP parses EFTS metadata; doc-body percent-of-class
   parsing is deferred to Phase 4b.

Tests avoid network/Supabase: helpers are exercised in isolation, the
end-to-end scan() function gets a stubbed _efts_search + a degraded
ticker-set path.

Plan: ~/.claude/plans/proud-booping-seal.md (Phase 4).
Run: python3 -m pytest modal_workers/tests/test_orchestrator_v4_phase4.py -v
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

import pytest

os.environ.setdefault("SUPABASE_URL", "https://x.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")


# ---------------------------------------------------------------------------
# Form 4 — _load_fda_tracked_tickers + routing logic
# ---------------------------------------------------------------------------

class _StubSupabaseClient:
    """Minimal SupabaseClient surrogate for testing _load_fda_tracked_tickers
    in isolation. Subclasses override _rest to return canned responses."""

    def __init__(self, rest_response: Any = None, raise_exc: Exception = None):
        self._rest_response = rest_response
        self._raise_exc = raise_exc
        self.calls: List[Dict[str, Any]] = []

    def _rest(self, method: str, path: str, params=None, json_body=None, **kw):
        self.calls.append({"method": method, "path": path, "params": params})
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._rest_response


def test_form4_load_fda_tracked_tickers_returns_uppercase_set():
    from modal_workers.scanners.insider_form4_scanner import _load_fda_tracked_tickers

    stub = _StubSupabaseClient(rest_response=[
        {"ticker": "vrdn"},
        {"ticker": "AXSM"},
        {"ticker": "  ions  "},  # whitespace tolerant
        {"ticker": None},        # null ticker dropped
        {"ticker": ""},          # empty ticker dropped
    ])
    tickers = _load_fda_tracked_tickers(stub)

    assert tickers == {"VRDN", "AXSM", "IONS"}, (
        f"expected uppercased, trimmed, non-empty tickers; got {tickers}"
    )

    # Confirms the query targets active rows with non-null tickers.
    assert len(stub.calls) == 1
    params = stub.calls[0]["params"]
    assert params["is_active"] == "eq.true"
    assert params["ticker"] == "not.is.null"


def test_form4_load_fda_tracked_tickers_safe_on_query_failure():
    """A DB query failure leaves the set empty → all clusters route to
    short_positioning. Degraded but safe (matches v3 behavior)."""
    from modal_workers.scanners.insider_form4_scanner import _load_fda_tracked_tickers

    stub = _StubSupabaseClient(raise_exc=RuntimeError("supabase down"))
    tickers = _load_fda_tracked_tickers(stub)
    assert tickers == set()


def test_form4_routing_decision_logic_inline():
    """The actual routing decision is inline in scan(). Verify the source
    contains the v4 Phase 4 routing branch — covers: tracked-ticker check,
    scoring_profile override, extensions.signal_category."""
    import inspect

    from modal_workers.scanners import insider_form4_scanner

    source = inspect.getsource(insider_form4_scanner.scan)

    # Pre-fetch of the ticker set, once per run.
    assert "_load_fda_tracked_tickers(client)" in source

    # Per-cluster routing branch.
    assert "ticker.upper() in fda_tracked_tickers" in source
    assert 'scoring_profile_override = "binary_catalyst"' in source
    assert '"signal_category"' in source
    assert '"insider_activity"' in source

    # Observability metric.
    assert "clusters_routed_binary_catalyst" in source


def test_form4_routing_metrics_in_run_metrics():
    """The run_metrics dict must carry the two new Phase 4 counters."""
    import inspect

    from modal_workers.scanners import insider_form4_scanner

    source = inspect.getsource(insider_form4_scanner.scan)
    assert "fda_tracked_tickers_loaded" in source
    assert "clusters_routed_binary_catalyst" in source


# ---------------------------------------------------------------------------
# 13D/13G scanner — pure helpers
# ---------------------------------------------------------------------------

def test_13d_13g_signal_type_mapping():
    from modal_workers.scanners.edgar_13d_13g_scanner import _signal_type_for_form

    assert _signal_type_for_form("SC 13D") == "shareholder_13d_filing"
    assert _signal_type_for_form("SC 13D/A") == "shareholder_13d_amendment"
    assert _signal_type_for_form("SC 13G") == "shareholder_13g_filing"
    assert _signal_type_for_form("SC 13G/A") == "shareholder_13g_amendment"

    # Defensive: whitespace + case insensitive.
    assert _signal_type_for_form("sc13d") == "shareholder_13d_filing"
    assert _signal_type_for_form("Sc 13G") == "shareholder_13g_filing"

    # Unknown form → unknown bucket (not a crash).
    assert _signal_type_for_form("SC 14D9") == "shareholder_unknown"
    assert _signal_type_for_form("") == "shareholder_unknown"


def test_13d_13g_strength_ordering():
    """13D > 13G (intent matters more than passive). Initial filings > amendments."""
    from modal_workers.scanners.edgar_13d_13g_scanner import _strength_for_signal_type

    s_13d_new = _strength_for_signal_type("shareholder_13d_filing")
    s_13d_amd = _strength_for_signal_type("shareholder_13d_amendment")
    s_13g_new = _strength_for_signal_type("shareholder_13g_filing")
    s_13g_amd = _strength_for_signal_type("shareholder_13g_amendment")

    assert s_13d_new > s_13d_amd, "13D initial must outrank 13D amendment"
    assert s_13d_new >= s_13g_new, "13D activist must outrank 13G passive"
    assert s_13g_new > s_13g_amd, "13G initial must outrank 13G amendment"

    # All bounded 1-5 (Signal.strength_estimate convention).
    for s in (s_13d_new, s_13d_amd, s_13g_new, s_13g_amd):
        assert 1 <= s <= 5


def test_13d_13g_content_hash_deterministic_and_unique():
    from modal_workers.scanners.edgar_13d_13g_scanner import _content_hash

    h1 = _content_hash("0000000000-00-000001", "0001234567", "SC 13D")
    h2 = _content_hash("0000000000-00-000001", "0001234567", "SC 13D")
    h3 = _content_hash("0000000000-00-000001", "0001234567", "SC 13D/A")
    h4 = _content_hash("0000000000-00-000002", "0001234567", "SC 13D")

    assert h1 == h2, "same (adsh, cik, form_type) must produce same hash"
    assert h1 != h3, "different form_type must produce different hash"
    assert h1 != h4, "different accession must produce different hash"
    # SHA-256 hex length.
    assert len(h1) == 64


def test_13d_13g_load_tracked_tickers_mirror_of_form4():
    """The two scanners share the routing convention. If they diverge, one
    will see assets the other doesn't."""
    from modal_workers.scanners.edgar_13d_13g_scanner import (
        _load_fda_tracked_tickers as _13d_loader,
    )
    from modal_workers.scanners.insider_form4_scanner import (
        _load_fda_tracked_tickers as _f4_loader,
    )

    stub = _StubSupabaseClient(rest_response=[
        {"ticker": "vrdn"}, {"ticker": "AXSM"},
    ])
    assert _13d_loader(stub) == {"VRDN", "AXSM"}

    stub2 = _StubSupabaseClient(rest_response=[
        {"ticker": "vrdn"}, {"ticker": "AXSM"},
    ])
    assert _f4_loader(stub2) == {"VRDN", "AXSM"}


# ---------------------------------------------------------------------------
# 13D/13G scanner — degraded-mode scan()
# ---------------------------------------------------------------------------

def test_13d_13g_scan_empty_tracked_set_returns_ok_empty(monkeypatch):
    """When fda_assets is empty, the scanner must return ok status + zero
    signals + a clear warning. This is "nothing to emit" not "broken"."""
    monkeypatch.setenv("SEC_USER_AGENT", "test-agent contact@example.com")

    from modal_workers.scanners import edgar_13d_13g_scanner
    from modal_workers.shared.supabase_client import ScannerConfig

    # Stub the tracked-tickers fetch to return empty.
    monkeypatch.setattr(
        edgar_13d_13g_scanner, "_load_fda_tracked_tickers",
        lambda client: set(),
    )
    # Also stub SupabaseClient construction so we don't hit real DB.
    monkeypatch.setattr(
        edgar_13d_13g_scanner, "SupabaseClient", lambda: object(),
    )

    cfg = ScannerConfig(
        scanner_id="00000000-0000-0000-0000-000000000000",
        name=edgar_13d_13g_scanner.NAME,
        status="active",
        geography="US",
        cadence="hourly",
        default_scoring_profile="binary_catalyst",
        signal_type_profile_map={},
        endpoints={},
        timeout_soft_s=30,
        timeout_hard_s=60,
        config={},
    )

    result = edgar_13d_13g_scanner.scan(cfg)
    assert result.status == "ok"
    assert result.signals == []
    assert result.run_metrics["fda_tracked_tickers_loaded"] == 0
    assert result.run_metrics["signals_emitted"] == 0
    assert any("no fda_assets to match" in w for w in result.warnings)


def test_13d_13g_scan_filters_to_tracked_tickers_only(monkeypatch):
    """End-to-end-ish: feed two EFTS hits — one on a tracked ticker, one
    on a non-tracked ticker. Only the tracked one becomes a signal."""
    monkeypatch.setenv("SEC_USER_AGENT", "test-agent contact@example.com")

    from modal_workers.scanners import edgar_13d_13g_scanner
    from modal_workers.shared.supabase_client import ScannerConfig

    monkeypatch.setattr(
        edgar_13d_13g_scanner, "_load_fda_tracked_tickers",
        lambda client: {"VRDN"},
    )
    monkeypatch.setattr(
        edgar_13d_13g_scanner, "SupabaseClient", lambda: object(),
    )

    # Stub EFTS to return two hits.
    fake_hits = [
        {
            "ciks": ["0001234567"],
            "adsh": "0000000000-00-000001",
            "form": "SC 13D",
            "file_date": "2026-05-25",
            "display_names": ["Viridian Therapeutics, Inc. (VRDN)", "Acme Capital"],
        },
        {
            "ciks": ["0007654321"],
            "adsh": "0000000000-00-000002",
            "form": "SC 13G",
            "file_date": "2026-05-25",
            "display_names": ["Random Co (RNDM)", "Passive Fund"],
        },
    ]
    monkeypatch.setattr(
        edgar_13d_13g_scanner, "_efts_search",
        lambda **kw: fake_hits,
    )

    # Stub ticker resolution: 0001234567 → VRDN (tracked), 0007654321 → RNDM (not tracked).
    def fake_resolve(cik, *, user_agent):
        return {"0001234567": "VRDN", "0007654321": "RNDM"}.get(cik)
    monkeypatch.setattr(
        edgar_13d_13g_scanner, "_resolve_ticker_for_cik",
        fake_resolve,
    )

    cfg = ScannerConfig(
        scanner_id="00000000-0000-0000-0000-000000000000",
        name=edgar_13d_13g_scanner.NAME,
        status="active",
        geography="US",
        cadence="hourly",
        default_scoring_profile="binary_catalyst",
        signal_type_profile_map={},
        endpoints={},
        timeout_soft_s=30,
        timeout_hard_s=60,
        config={},
    )

    result = edgar_13d_13g_scanner.scan(cfg)
    assert result.status == "ok"
    assert len(result.signals) == 1, (
        f"expected 1 signal (VRDN only); got {len(result.signals)}"
    )

    sig = result.signals[0]
    assert sig.signal_type == "shareholder_13d_filing"
    assert sig.scoring_profile == "binary_catalyst"
    assert sig.extensions["signal_category"] == "shareholder_structure"
    assert sig.raw_payload["signal_category"] == "shareholder_structure"
    assert sig.raw_payload["subject_ticker"] == "VRDN"
    assert sig.raw_payload["form_type"] == "SC 13D"
    assert sig.entity_hints.ticker == "VRDN"

    # Metrics reflect the filter.
    assert result.run_metrics["filings_listed"] == 2
    assert result.run_metrics["filings_on_tracked_assets"] == 1
    assert result.run_metrics["signals_emitted"] == 1


def test_13d_13g_scan_missing_user_agent_raises(monkeypatch):
    """SEC requires a User-Agent header. Missing → MissingAuthError, not
    silent failure."""
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)

    from modal_workers.scanners import edgar_13d_13g_scanner
    from modal_workers.shared.scanner_base import MissingAuthError
    from modal_workers.shared.supabase_client import ScannerConfig

    cfg = ScannerConfig(
        scanner_id="00000000-0000-0000-0000-000000000000",
        name=edgar_13d_13g_scanner.NAME,
        status="active",
        geography="US",
        cadence="hourly",
        default_scoring_profile="binary_catalyst",
        signal_type_profile_map={},
        endpoints={},
        timeout_soft_s=30,
        timeout_hard_s=60,
        config={},
    )

    with pytest.raises(MissingAuthError):
        edgar_13d_13g_scanner.scan(cfg)
