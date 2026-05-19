"""
Focused tests for the flagship Modal EDGAR scanner.

Covers:
  - merger-sibling suppression (QXO/TopBuild regression)
  - EFTS retry / failure semantics
  - budget exhaustion signaling
  - issuer/SPAC filter behavior
  - market-cap triage
  - filing-type scan behavior
  - after_insert persistence for dedup / rotation
  - persistent CIK-ticker + merger-sibling caches (2026-04-22 perf pass)
  - post-scan yfinance deferral (2026-04-22 perf pass)
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from modal_workers.scanners import edgar_filing_monitor as efm


# ----------------------------------------------------------------------
# _has_merger_sibling
# ----------------------------------------------------------------------

def _fake_efts_response(hits):
    resp = MagicMock()
    resp.json.return_value = {"hits": {"hits": hits}}
    resp.raise_for_status.return_value = None
    return resp


class TestHasMergerSibling:
    def test_returns_true_when_sibling_filing_exists(self):
        cache: dict = {}
        with patch.object(efm, "_http_get",
                          return_value=_fake_efts_response([{"adsh": "0001-25-000425"}])):
            assert efm._has_merger_sibling(
                "12345", "2026-04-18",
                user_agent="ua@example.com", cache=cache) is True

    def test_returns_false_when_no_sibling_filing(self):
        cache: dict = {}
        with patch.object(efm, "_http_get",
                          return_value=_fake_efts_response([])):
            assert efm._has_merger_sibling(
                "12345", "2026-04-18",
                user_agent="ua@example.com", cache=cache) is False

    def test_cache_dedupes_repeat_call(self):
        cache: dict = {}
        with patch.object(efm, "_http_get",
                          return_value=_fake_efts_response([{"adsh": "x"}])) as mock_get:
            efm._has_merger_sibling("12345", "2026-04-18",
                                    user_agent="u", cache=cache)
            efm._has_merger_sibling("12345", "2026-04-18",
                                    user_agent="u", cache=cache)
            assert mock_get.call_count == 1

    def test_different_cik_triggers_new_call(self):
        cache: dict = {}
        with patch.object(efm, "_http_get",
                          return_value=_fake_efts_response([])) as mock_get:
            efm._has_merger_sibling("12345", "2026-04-18",
                                    user_agent="u", cache=cache)
            efm._has_merger_sibling("67890", "2026-04-18",
                                    user_agent="u", cache=cache)
            assert mock_get.call_count == 2

    def test_network_error_fails_open(self):
        """On RequestException the helper returns False (does not suppress)."""
        cache: dict = {}
        with patch.object(efm, "_http_get",
                          side_effect=requests.exceptions.ConnectionError("no net")):
            assert efm._has_merger_sibling(
                "12345", "2026-04-18",
                user_agent="u", cache=cache) is False

    def test_bad_date_returns_false(self):
        cache: dict = {}
        assert efm._has_merger_sibling(
            "12345", "not-a-date", user_agent="u", cache=cache) is False

    def test_empty_cik_returns_false(self):
        cache: dict = {}
        assert efm._has_merger_sibling(
            "", "2026-04-18", user_agent="u", cache=cache) is False

    def test_query_params_shape(self):
        """CIK is 10-padded, forms comma-joined, window is ±MERGER_SIBLING_WINDOW_DAYS.

        Broadened 2026-04-21 after the QXO/TopBuild DLQ incident: MERGER_SIBLING_FORMS
        grew from the original 425/PREM14A/SC TO-T triplet to the full M&A co-filing
        ecosystem (8 forms), and the window extended from ±3d to ±7d to tolerate
        filing-date drift. The test now derives expectations from the module constants
        rather than hardcoding — any future broadening stays green automatically.
        """
        cache: dict = {}
        with patch.object(efm, "_http_get",
                          return_value=_fake_efts_response([])) as mock_get:
            efm._has_merger_sibling("12345", "2026-04-18",
                                    user_agent="u", cache=cache)
            params = mock_get.call_args.kwargs["params"]
            assert params["ciks"] == "0000012345"
            assert params["forms"] == ",".join(efm.MERGER_SIBLING_FORMS)
            # Current forms at 2026-04-21: 425, PREM14A, DEFM14A, DEFA14A,
            # SC TO-T, SC TO-I, SC 14D9, S-4.
            assert "425" in params["forms"]
            assert "DEFM14A" in params["forms"]   # was missed pre-2026-04-21
            assert "S-4" in params["forms"]       # was missed pre-2026-04-21
            # Window is derived from the module constant so a future bump stays green.
            from datetime import datetime, timedelta
            anchor = datetime.strptime("2026-04-18", "%Y-%m-%d")
            w = efm.MERGER_SIBLING_WINDOW_DAYS
            assert params["startdt"] == (anchor - timedelta(days=w)).strftime("%Y-%m-%d")
            assert params["enddt"] == (anchor + timedelta(days=w)).strftime("%Y-%m-%d")


# ----------------------------------------------------------------------
# scan() — gate integration
# ----------------------------------------------------------------------

def _hit(
    form="8-K",
    cik="0000012345",
    adsh="0001-25-000100",
    file_date="2026-04-18",
    company_name="Example Corp",
    company_raw="Example Corp (CIK 12345)",
    file_description="Entry into a Material Definitive Agreement",
):
    return {
        "cik": cik,
        "adsh": adsh,
        "form": form,
        "file_date": file_date,
        "company_name": company_name,
        "company_raw": company_raw,
        "file_description": file_description,
        "filing_url": f"https://www.sec.gov/Archives/{adsh}",
        "sics": [],
    }


def _make_cfg(**overrides):
    from modal_workers.shared.supabase_client import ScannerConfig
    base = dict(
        scanner_id="sid", name="edgar_filing_monitor", status="active",
        geography="US", cadence="3h",
        default_scoring_profile="activist_governance",
        signal_type_profile_map={}, endpoints={},
        timeout_soft_s=35, timeout_hard_s=120,
        config={"days_back": 2},
    )
    base.update(overrides)
    return ScannerConfig(**base)


def _run_scan(
    cfg,
    *,
    keyword_hits,
    sibling_patch,
    filing_hits=None,
    company_tickers=None,
    market_cap_side_effect=None,
):
    """Run efm.scan() with external IO stubbed.

    - Coverage forced to a single activist category unless caller overrides cfg.
    - _efts_search returns `keyword_hits` for every keyword query.
    - `sibling_patch` is the `_mock._patch` object produced by
      `patch.object(efm, "_has_merger_sibling", ...)`. Caller controls the
      return value or side_effect; this helper only enters the context.
    - Filing-type scan returns `filing_hits` keyed by form_type.
    """
    filing_hits = filing_hits or {}
    company_tickers = company_tickers or (["EXC"], "NYSE")

    mock_client = MagicMock()
    mock_client.openfigi_cache_backend.return_value = (None, None, None)

    def efts_side_effect(query, date_from, date_to, form_type="", **kwargs):
        if form_type:
            return filing_hits.get(form_type, [])
        return keyword_hits

    with patch.object(efm, "SupabaseClient", return_value=mock_client), \
         patch.object(efm, "_rotation_state_for_mode", return_value=(["activist"], {"rotation_index": 0, "scan_history": {}})), \
         patch.object(efm, "_save_rotation"), \
         patch.object(efm, "_load_dedup", return_value={}), \
         patch.object(efm, "_save_dedup"), \
         patch.object(efm, "_efts_search", side_effect=efts_side_effect), \
         patch.object(efm, "_get_company_tickers", return_value=company_tickers), \
         patch.object(efm, "_load_market_cap_usd_mm", side_effect=market_cap_side_effect or (lambda client, ticker, memo: None)), \
         patch("modal_workers.shared.openfigi_resolver.set_cache_backend"), \
         patch("modal_workers.shared.openfigi_resolver.resolve_ticker",
               return_value=MagicMock(resolved=False, issuer_figi=None)), \
         patch.dict("os.environ", {"SEC_USER_AGENT": "ua@example.com"}), \
         sibling_patch:
        return efm.scan(cfg)


class TestScanMergerSuppression:
    def test_8k_with_sibling_produces_zero_signals(self):
        """QXO-TopBuild scenario: 8-K on same CIK that has a merger 425 sibling.
        Canned hit simulates 'board representation' appearing in the merger 8-K."""
        result = _run_scan(
            _make_cfg(),
            keyword_hits=[_hit(form="8-K")],
            sibling_patch=patch.object(efm, "_has_merger_sibling", return_value=True))
        assert len(result.signals) == 0
        assert any("suppressed" in w for w in result.warnings)

    def test_8k_without_sibling_produces_signal(self):
        """No merger sibling → legitimate activist 8-K survives."""
        result = _run_scan(
            _make_cfg(),
            keyword_hits=[_hit(form="8-K")],
            sibling_patch=patch.object(efm, "_has_merger_sibling", return_value=False))
        # 8 activist keywords × 1 hit each = 8 signals (adsh|keyword unique per pair).
        assert len(result.signals) == len(efm.SIGNAL_KEYWORDS["activist"])
        assert all(s.signal_type == "activist_keyword" for s in result.signals)

    def test_prer14a_bypasses_sibling_check(self):
        """Activist-specific forms (PRER14A, DFAN14A, SC 13D, SC 14D9) never
        invoke the sibling check — preserves RGR/Beretta-style legit hits."""
        result = _run_scan(
            _make_cfg(),
            keyword_hits=[_hit(form="PRER14A")],
            sibling_patch=patch.object(
                efm, "_has_merger_sibling",
                side_effect=AssertionError("sibling check must not run for non-8-K forms")))
        assert len(result.signals) == len(efm.SIGNAL_KEYWORDS["activist"])

    def test_suppression_disabled_via_config(self):
        """Feature flag: cfg.config.activist_merger_sibling_suppression=False
        restores pre-fix behavior."""
        cfg = _make_cfg()
        cfg.config["activist_merger_sibling_suppression"] = False
        result = _run_scan(
            cfg,
            keyword_hits=[_hit(form="8-K")],
            sibling_patch=patch.object(
                efm, "_has_merger_sibling",
                side_effect=AssertionError(
                    "sibling check must not run when suppression is disabled")))
        assert len(result.signals) == len(efm.SIGNAL_KEYWORDS["activist"])


def test_efts_search_retries_transient_failure_then_succeeds():
    metrics = efm._new_run_metrics(
        budget_s=50,
        coverage_mode="full",
        categories_requested=["activist"],
        filing_types_requested=[],
    )
    timeout = requests.exceptions.Timeout("slow")
    success = _fake_efts_response([{"_source": {
        "ciks": ["0000012345"],
        "adsh": "0001-25-000100",
        "display_names": ["Example Corp (CIK 0000012345)"],
        "form": "8-K",
        "file_date": "2026-04-18",
        "file_description": "Entry into a Material Definitive Agreement",
        "sics": [],
    }}])
    with patch.object(efm, "_http_get", side_effect=[timeout, success]) as mock_get:
        hits = efm._efts_search(
            '"board representation"',
            "2026-04-17",
            "2026-04-21",
            user_agent="ua@example.com",
            metrics=metrics,
        )
    assert len(hits) == 1
    assert mock_get.call_count == 2
    assert metrics["retries_attempted"] == 1
    assert metrics["efts_failures"] == 0


def test_efts_search_records_failure_metrics_on_non_retriable_error():
    metrics = efm._new_run_metrics(
        budget_s=50,
        coverage_mode="full",
        categories_requested=["activist"],
        filing_types_requested=[],
    )
    response = MagicMock()
    response.status_code = 400
    error = requests.exceptions.HTTPError("bad request", response=response)
    with patch.object(efm, "_http_get", side_effect=error):
        hits = efm._efts_search(
            '"board representation"',
            "2026-04-17",
            "2026-04-21",
            user_agent="ua@example.com",
            metrics=metrics,
        )
    assert hits == []
    assert metrics["efts_failures"] == 1
    assert "efts_failure" in metrics["partial_reasons"]


def test_keyword_phase_budget_exhaustion_marks_partial():
    cfg = _make_cfg(config={"days_back": 2, "coverage_mode": "full"})
    with patch.object(efm, "SupabaseClient", return_value=MagicMock(openfigi_cache_backend=lambda: (None, None, None))), \
         patch.object(efm, "_rotation_state_for_mode", return_value=(["activist"], None)), \
         patch.object(efm, "_load_dedup", return_value={}), \
         patch.object(efm, "_save_dedup"), \
         patch.object(efm, "_efts_search", return_value=[]), \
         patch("modal_workers.shared.openfigi_resolver.set_cache_backend"), \
         patch.dict("os.environ", {"SEC_USER_AGENT": "ua@example.com"}), \
         patch.object(efm, "_has_budget_for_query", return_value=False):
        result = efm.scan(cfg)

    assert result.status == "partial"
    assert result.run_metrics["budget_exhausted"] is True
    assert "budget_exhausted_keyword_phase" in result.run_metrics["partial_reasons"]


def test_filing_phase_budget_exhaustion_marks_partial():
    cfg = _make_cfg(config={"days_back": 2, "coverage_mode": "full"})
    budget_calls = {"n": 0}

    def fake_budget(*args, **kwargs):
        budget_calls["n"] += 1
        return budget_calls["n"] <= 1

    mock_client = MagicMock()
    mock_client.openfigi_cache_backend.return_value = (None, None, None)
    with patch.object(efm, "SupabaseClient", return_value=mock_client), \
         patch.object(efm, "_rotation_state_for_mode", return_value=([], None)), \
         patch.object(efm, "_load_dedup", return_value={}), \
         patch.object(efm, "_save_dedup"), \
         patch.object(efm, "_efts_search", return_value=[]), \
         patch("modal_workers.shared.openfigi_resolver.set_cache_backend"), \
         patch.dict("os.environ", {"SEC_USER_AGENT": "ua@example.com"}), \
         patch.object(efm, "_has_budget_for_query", side_effect=fake_budget):
        result = efm.scan(cfg)

    assert result.status == "partial"
    assert "budget_exhausted_filing_phase" in result.run_metrics["partial_reasons"]


def test_blocked_issuer_filter_drops_spac_like_name():
    result = _run_scan(
        _make_cfg(config={"days_back": 2, "coverage_mode": "full"}),
        keyword_hits=[_hit(
            form="8-K",
            cik="0002000775",
            company_name="Black Hawk Acquisition Corp",
            company_raw="Black Hawk Acquisition Corp (CIK 0002000775)",
            file_description="Blank check business combination update",
        )],
        sibling_patch=patch.object(efm, "_has_merger_sibling", return_value=False),
    )
    assert len(result.signals) == 0
    assert result.run_metrics["issuer_filtered_total"] >= 1


def test_allowlist_ticker_escapes_name_pattern_filter():
    result = _run_scan(
        _make_cfg(config={"days_back": 2, "coverage_mode": "full"}),
        keyword_hits=[_hit(
            form="8-K",
            cik="0001999999",
            company_name="Special Purpose Acquisition Widget",
            company_raw="Special Purpose Acquisition Widget (CIK 0001999999)",
            file_description="Board representation update",
        )],
        sibling_patch=patch.object(efm, "_has_merger_sibling", return_value=False),
        company_tickers=(["RPAY"], "NYSE"),
    )
    assert len(result.signals) == len(efm.SIGNAL_KEYWORDS["activist"])
    assert result.run_metrics["issuer_filtered_total"] == 0


def test_market_cap_filter_drops_small_cap_issuer():
    result = _run_scan(
        _make_cfg(config={"days_back": 2, "coverage_mode": "full", "market_cap_floor_usd_mm": 215}),
        keyword_hits=[_hit(form="8-K", cik="0000012345")],
        sibling_patch=patch.object(efm, "_has_merger_sibling", return_value=False),
        company_tickers=(["TINY"], "NYSE"),
        market_cap_side_effect=lambda client, ticker, memo: 100.0,
    )
    assert len(result.signals) == 0
    assert result.run_metrics["market_cap_filtered_total"] >= 1


def test_filing_type_scan_emits_activist_ownership_signal():
    filing_hit = _hit(form="SC 13D", cik="0000012345", adsh="0001-25-000200", file_description="Schedule 13D filing")
    result = _run_scan(
        _make_cfg(config={"days_back": 2, "coverage_mode": "full"}),
        keyword_hits=[],
        filing_hits={"SC 13D": [filing_hit]},
        sibling_patch=patch.object(efm, "_has_merger_sibling", return_value=False),
    )
    activist_ownership = [signal for signal in result.signals if signal.signal_type == "activist_ownership"]
    assert len(activist_ownership) == 1
    assert activist_ownership[0].strength_estimate == 4
    assert activist_ownership[0].thesis_direction == "long"


def test_dedup_and_rotation_persist_only_after_insert():
    cfg = _make_cfg(config={"days_back": 2, "coverage_mode": "rotation"})
    rotation_state = {"rotation_index": 0, "scan_history": {}}
    mock_client = MagicMock()
    mock_client.openfigi_cache_backend.return_value = (None, None, None)

    with patch.object(efm, "SupabaseClient", return_value=mock_client), \
         patch.object(efm, "_rotation_state_for_mode", return_value=(["activist"], rotation_state)), \
         patch.object(efm, "_load_dedup", return_value={}), \
         patch.object(efm, "_save_dedup") as save_dedup, \
         patch.object(efm, "_save_rotation") as save_rotation, \
         patch.object(efm, "_efts_search", return_value=[_hit(form="PRER14A")]), \
         patch.object(efm, "_get_company_tickers", return_value=(["EXC"], "NYSE")), \
         patch.object(efm, "_load_market_cap_usd_mm", return_value=None), \
         patch("modal_workers.shared.openfigi_resolver.set_cache_backend"), \
         patch("modal_workers.shared.openfigi_resolver.resolve_ticker",
               return_value=MagicMock(resolved=False, issuer_figi=None)), \
         patch.dict("os.environ", {"SEC_USER_AGENT": "ua@example.com"}), \
         patch.object(efm, "_has_merger_sibling", return_value=False):
        result = efm.scan(cfg)
        save_dedup.assert_not_called()
        save_rotation.assert_not_called()
        assert result.after_insert is not None
        result.after_insert()
        save_dedup.assert_called_once()
        save_rotation.assert_called_once_with(mock_client, rotation_state)

    assert result.run_metrics["coverage_mode"] == "rotation"


# ----------------------------------------------------------------------
# Persistent caches (2026-04-22 perf pass)
# ----------------------------------------------------------------------

class TestCoerceCacheBlob:
    """`_coerce_cache_blob` must absorb every non-bytes input type so the
    cache loaders degrade to a cold cache rather than raising."""

    def test_none_returns_none(self):
        assert efm._coerce_cache_blob(None) is None

    def test_magicmock_returns_none(self):
        # This is the real-world case: tests pass a MagicMock as the
        # SupabaseClient, and its read_cache() returns a MagicMock by default.
        assert efm._coerce_cache_blob(MagicMock()) is None

    def test_invalid_json_returns_none(self):
        assert efm._coerce_cache_blob(b"{not: json") is None

    def test_non_dict_json_returns_none(self):
        assert efm._coerce_cache_blob(b"[]") is None

    def test_valid_dict_passes_through(self):
        out = efm._coerce_cache_blob(b'{"entries": {"x": 1}}')
        assert out == {"entries": {"x": 1}}


class TestLoadPersistentTickerCache:
    def test_missing_cache_returns_empty(self):
        client = MagicMock()
        client.read_cache.return_value = None
        assert efm._load_persistent_ticker_cache(client) == {}

    def test_magicmock_read_returns_empty(self):
        """Defensive against tests that don't configure read_cache — the
        default MagicMock return must not crash the loader."""
        client = MagicMock()  # read_cache returns a MagicMock by default
        assert efm._load_persistent_ticker_cache(client) == {}

    def test_valid_blob_returns_entries(self):
        client = MagicMock()
        client.read_cache.return_value = (
            b'{"cached_at": 1.0, "entries": '
            b'{"0000012345": {"tickers": ["EXC"], "exchange": "NYSE", "cached_at": 1.0}}}'
        )
        cache = efm._load_persistent_ticker_cache(client)
        assert "0000012345" in cache
        assert cache["0000012345"]["tickers"] == ["EXC"]

    def test_read_cache_exception_returns_empty(self):
        client = MagicMock()
        client.read_cache.side_effect = RuntimeError("storage unreachable")
        assert efm._load_persistent_ticker_cache(client) == {}


class TestLoadCompanyContextTTL:
    """Verify the 7-day per-entry TTL — fresh hits skip the fetch,
    stale entries trigger a refetch and flip the dirty flag."""

    def test_fresh_entry_skips_fetch(self):
        import time
        cache = {
            "0000012345": {
                "tickers": ["EXC"],
                "exchange": "NYSE",
                "cached_at": time.time() - 60,  # 60s old, well within 7d
            }
        }
        dirty = [False]
        with patch.object(efm, "_get_company_tickers",
                          side_effect=AssertionError("must not fetch on fresh hit")):
            tickers, exchange = efm._load_company_context(
                "12345", user_agent="u", cache=cache, dirty=dirty,
            )
        assert tickers == ["EXC"]
        assert exchange == "NYSE"
        assert dirty == [False]

    def test_stale_entry_triggers_refetch(self):
        import time
        cache = {
            "0000012345": {
                "tickers": ["OLD"],
                "exchange": "NYSE",
                "cached_at": time.time() - efm.COMPANY_CACHE_TTL_S - 10,
            }
        }
        dirty = [False]
        with patch.object(efm, "_get_company_tickers",
                          return_value=(["NEW"], "NASDAQ")) as mock_fetch:
            tickers, exchange = efm._load_company_context(
                "12345", user_agent="u", cache=cache, dirty=dirty,
            )
        mock_fetch.assert_called_once()
        assert tickers == ["NEW"]
        assert exchange == "NASDAQ"
        assert dirty == [True]
        # Cache entry was overwritten with fresh data
        assert cache["0000012345"]["tickers"] == ["NEW"]

    def test_cache_miss_triggers_fetch_and_dirty(self):
        cache: dict = {}
        dirty = [False]
        with patch.object(efm, "_get_company_tickers",
                          return_value=(["NEW"], "NYSE")) as mock_fetch:
            tickers, exchange = efm._load_company_context(
                "12345", user_agent="u", cache=cache, dirty=dirty,
            )
        mock_fetch.assert_called_once()
        assert dirty == [True]
        assert "0000012345" in cache  # keyed padded
        assert cache["0000012345"]["tickers"] == ["NEW"]

    def test_empty_cik_short_circuits(self):
        """Empty CIK returns ([], None) without touching the cache or fetching."""
        cache: dict = {}
        dirty = [False]
        with patch.object(efm, "_get_company_tickers",
                          side_effect=AssertionError("must not fetch on empty cik")):
            assert efm._load_company_context(
                "", user_agent="u", cache=cache, dirty=dirty,
            ) == ([], None)
        assert cache == {}
        assert dirty == [False]


class TestMergerSiblingPersistentCache:
    """Two-tier cache invariant: persistent cache stores ONLY True answers
    (monotonic); session cache dedupes False answers within one run only."""

    def test_true_result_persisted_and_dirties(self):
        """A True answer writes to the persistent cache and flips dirty —
        True is monotonic (filings don't un-file) so cross-run caching is safe."""
        cache: dict = {}
        session: dict = {}
        dirty = [False]
        with patch.object(efm, "_http_get",
                          return_value=_fake_efts_response([{"adsh": "0001-25-000425"}])):
            result = efm._has_merger_sibling(
                "12345", "2026-04-18",
                user_agent="u", cache=cache,
                session_cache=session, dirty=dirty,
            )
        assert result is True
        assert cache["12345|2026-04-18"] is True
        assert session["12345|2026-04-18"] is True
        assert dirty == [True]

    def test_false_result_not_persisted(self):
        """A False answer must NOT land in the persistent cache — a companion
        PREM14A could be filed later, and a cached False would wrongly
        suppress the QXO/TopBuild regression class on the next run."""
        cache: dict = {}
        session: dict = {}
        dirty = [False]
        with patch.object(efm, "_http_get",
                          return_value=_fake_efts_response([])):
            result = efm._has_merger_sibling(
                "12345", "2026-04-18",
                user_agent="u", cache=cache,
                session_cache=session, dirty=dirty,
            )
        assert result is False
        assert "12345|2026-04-18" not in cache
        assert session["12345|2026-04-18"] is False  # session dedup still works
        assert dirty == [False]

    def test_false_result_dedupes_within_run(self):
        """Same (cik, file_date) queried twice with shared session cache
        must hit the network only once — per-run dedup still works even
        though we don't persist False."""
        cache: dict = {}
        session: dict = {}
        with patch.object(efm, "_http_get",
                          return_value=_fake_efts_response([])) as mock_get:
            efm._has_merger_sibling(
                "12345", "2026-04-18",
                user_agent="u", cache=cache, session_cache=session,
            )
            efm._has_merger_sibling(
                "12345", "2026-04-18",
                user_agent="u", cache=cache, session_cache=session,
            )
        assert mock_get.call_count == 1

    def test_true_result_dedupes_within_run_from_persistent(self):
        """Persistent-cache True hit short-circuits without touching the
        network, no session cache needed."""
        cache = {"12345|2026-04-18": True}
        with patch.object(efm, "_http_get",
                          side_effect=AssertionError("must not query on persistent True hit")):
            assert efm._has_merger_sibling(
                "12345", "2026-04-18",
                user_agent="u", cache=cache, session_cache={},
            ) is True

    def test_qxo_topbuild_class_not_suppressed_on_late_proxy(self):
        """Regression: yesterday we cached False for (cik, 2026-04-18)
        because the PREM14A hadn't landed yet. Today the proxy is filed.
        The NEW design must NOT have persisted yesterday's False — so
        today's query flips the answer to True and suppresses correctly.

        This is the whole reason we switched to True-only persistence.
        """
        # Simulate yesterday's run: ended with False, nothing persisted.
        cache: dict = {}  # persistent cache: empty, as expected
        # Today's run: the companion PREM14A has been filed overnight.
        session: dict = {}
        dirty = [False]
        with patch.object(efm, "_http_get",
                          return_value=_fake_efts_response([{"adsh": "0001-25-000999"}])):
            result = efm._has_merger_sibling(
                "12345", "2026-04-18",
                user_agent="u", cache=cache,
                session_cache=session, dirty=dirty,
            )
        assert result is True  # suppression engages today
        assert cache["12345|2026-04-18"] is True  # now persisted

    def test_missing_session_cache_still_works(self):
        """session_cache is optional — older call sites (and tests) that
        pass only `cache=` must still behave correctly. No per-run dedup
        in that case, but the query still runs and True still persists."""
        cache: dict = {}
        with patch.object(efm, "_http_get",
                          return_value=_fake_efts_response([{"adsh": "x"}])):
            assert efm._has_merger_sibling(
                "12345", "2026-04-18",
                user_agent="u", cache=cache,
            ) is True
        assert cache["12345|2026-04-18"] is True

    def test_network_error_fails_open_and_not_persisted(self):
        """Network failures return False and DO NOT write to the persistent
        cache (could be a transient error; don't poison cross-run state)."""
        cache: dict = {}
        session: dict = {}
        with patch.object(efm, "_http_get",
                          side_effect=requests.exceptions.ConnectionError("no net")):
            result = efm._has_merger_sibling(
                "12345", "2026-04-18",
                user_agent="u", cache=cache, session_cache=session,
            )
        assert result is False
        assert cache == {}  # no cross-run pollution
        assert session["12345|2026-04-18"] is False  # but session dedup holds


class TestPruneMergerSiblingCache:
    def test_keeps_recent_drops_stale(self):
        from datetime import datetime, timedelta, timezone
        today = datetime.now(timezone.utc)
        recent = (today - timedelta(days=5)).strftime("%Y-%m-%d")
        stale = (today - timedelta(days=efm.MERGER_SIBLING_PRUNE_DAYS + 5)).strftime("%Y-%m-%d")
        pruned = efm._prune_merger_sibling_cache({
            f"12345|{recent}": True,
            f"67890|{stale}": False,
        })
        assert f"12345|{recent}" in pruned
        assert f"67890|{stale}" not in pruned

    def test_keeps_malformed_keys_defensively(self):
        """Entries without the `|` separator can't be dated; keep them
        to avoid silently losing data."""
        pruned = efm._prune_merger_sibling_cache({"no-separator": True})
        assert pruned == {"no-separator": True}


class TestPostScanMarketCapPass:
    """Market-cap resolution must happen AFTER both scan loops, so budget
    exhaustion in those loops never interferes with the filter."""

    def test_market_cap_filter_runs_after_loops_complete(self):
        """Even when budget is exhausted mid-scan, candidates already
        collected still go through market-cap filtering in the post-pass."""
        cfg = _make_cfg(config={
            "days_back": 2,
            "coverage_mode": "full",
            "market_cap_floor_usd_mm": 215,
        })
        # Market cap resolver returns a below-floor value → all survivors drop.
        market_cap_calls: list = []

        def mc_side_effect(client, ticker, memo):
            market_cap_calls.append(ticker)
            return 50.0  # well below 215 floor

        result = _run_scan(
            cfg,
            keyword_hits=[_hit(form="8-K", cik="0000012345")],
            sibling_patch=patch.object(efm, "_has_merger_sibling", return_value=False),
            company_tickers=(["TINY"], "NYSE"),
            market_cap_side_effect=mc_side_effect,
        )
        # Every candidate got its market cap resolved (post-pass ran).
        assert market_cap_calls, "expected post-scan market-cap resolver to run"
        # And the filter correctly zeroed out the signals.
        assert len(result.signals) == 0
        assert result.run_metrics["market_cap_filtered_total"] >= 1

    def test_unresolved_market_cap_keeps_signal(self):
        """When yfinance returns None, we increment market_cap_unknown_total
        but keep the signal (the filter is a floor, not a gate)."""
        result = _run_scan(
            _make_cfg(config={"days_back": 2, "market_cap_floor_usd_mm": 215}),
            keyword_hits=[_hit(form="8-K")],
            sibling_patch=patch.object(efm, "_has_merger_sibling", return_value=False),
            market_cap_side_effect=lambda client, ticker, memo: None,
        )
        assert len(result.signals) == len(efm.SIGNAL_KEYWORDS["activist"])
        assert result.run_metrics["market_cap_unknown_total"] >= 1
        assert result.run_metrics["market_cap_filtered_total"] == 0

    def test_market_cap_attached_to_signal_raw_payload(self):
        """Post-pass must write the resolved cap back onto the signal's
        raw_payload so downstream reactor sees it."""
        result = _run_scan(
            _make_cfg(config={"days_back": 2, "market_cap_floor_usd_mm": 100}),
            keyword_hits=[_hit(form="8-K")],
            sibling_patch=patch.object(efm, "_has_merger_sibling", return_value=False),
            market_cap_side_effect=lambda client, ticker, memo: 1_200.0,
        )
        assert result.signals, "expected survivors with a 100 floor and 1.2B cap"
        for sig in result.signals:
            assert sig.raw_payload["market_cap_usd_mm"] == 1_200.0


class TestAfterInsertPersistsCaches:
    """The new persistent caches must save on after_insert only when dirty
    — i.e., when we actually fetched fresh ticker / sibling data."""

    def test_ticker_cache_saved_when_dirty(self):
        """A run that resolves a new CIK → ticker flips the dirty flag and
        triggers `_save_persistent_ticker_cache` on after_insert."""
        cfg = _make_cfg(config={"days_back": 2, "coverage_mode": "rotation"})
        mock_client = MagicMock()
        mock_client.openfigi_cache_backend.return_value = (None, None, None)
        with patch.object(efm, "SupabaseClient", return_value=mock_client), \
             patch.object(efm, "_rotation_state_for_mode",
                          return_value=(["activist"], {"rotation_index": 0, "scan_history": {}})), \
             patch.object(efm, "_load_dedup", return_value={}), \
             patch.object(efm, "_save_dedup"), \
             patch.object(efm, "_save_rotation"), \
             patch.object(efm, "_efts_search", return_value=[_hit(form="PRER14A")]), \
             patch.object(efm, "_get_company_tickers", return_value=(["EXC"], "NYSE")), \
             patch.object(efm, "_load_market_cap_usd_mm", return_value=None), \
             patch.object(efm, "_save_persistent_ticker_cache") as save_ticker, \
             patch.object(efm, "_save_merger_sibling_cache") as save_merger, \
             patch("modal_workers.shared.openfigi_resolver.set_cache_backend"), \
             patch("modal_workers.shared.openfigi_resolver.resolve_ticker",
                   return_value=MagicMock(resolved=False, issuer_figi=None)), \
             patch.dict("os.environ", {"SEC_USER_AGENT": "ua@example.com"}), \
             patch.object(efm, "_has_merger_sibling", return_value=False):
            result = efm.scan(cfg)
            # Saves must be deferred until after_insert runs.
            save_ticker.assert_not_called()
            save_merger.assert_not_called()
            result.after_insert()
            # Ticker cache dirtied (we fetched EXC from cold); save happens.
            save_ticker.assert_called_once()
            # Merger sibling was patched, so our dirty flag never flipped
            # (the patched function bypasses our code entirely).
            save_merger.assert_not_called()

    def test_ticker_cache_not_saved_when_clean(self):
        """A run that processes zero hits never flips dirty → no save."""
        cfg = _make_cfg(config={"days_back": 2})
        mock_client = MagicMock()
        mock_client.openfigi_cache_backend.return_value = (None, None, None)
        with patch.object(efm, "SupabaseClient", return_value=mock_client), \
             patch.object(efm, "_rotation_state_for_mode", return_value=(["activist"], None)), \
             patch.object(efm, "_load_dedup", return_value={}), \
             patch.object(efm, "_save_dedup"), \
             patch.object(efm, "_efts_search", return_value=[]), \
             patch.object(efm, "_save_persistent_ticker_cache") as save_ticker, \
             patch.object(efm, "_save_merger_sibling_cache") as save_merger, \
             patch("modal_workers.shared.openfigi_resolver.set_cache_backend"), \
             patch.dict("os.environ", {"SEC_USER_AGENT": "ua@example.com"}):
            result = efm.scan(cfg)
            result.after_insert()
            save_ticker.assert_not_called()
            save_merger.assert_not_called()


class TestParallelMarketCapResolution:
    """Post-scan market-cap pass runs yfinance in a ThreadPoolExecutor with
    a soft wall-clock budget. Verifies parallelism, budget enforcement,
    memo fast-path, and worker exception isolation."""

    @staticmethod
    def _make_candidate(ticker):
        """Build a (sig, ticker, dedup_hash) triple matching the type
        signature of `_resolve_market_caps_parallel`. The Signal is only
        used for indexing; we can stub it."""
        return (MagicMock(), ticker, f"hash-{ticker}")

    def test_parallel_resolution_faster_than_serial(self):
        """10 candidates × 0.2s lookup. Serial would take ~2s; parallel
        (10 workers) should finish in ~0.2–0.4s. A 1.5s wall leaves
        comfortable margin without flaking on slow CI."""
        import time

        def slow_lookup(client, ticker, memo):
            time.sleep(0.2)
            return 1_500.0

        candidates = [self._make_candidate(f"T{i}") for i in range(10)]
        memo: dict = {}

        with patch.object(efm, "_load_market_cap_usd_mm", side_effect=slow_lookup):
            started = time.time()
            by_idx, exhausted = efm._resolve_market_caps_parallel(
                candidates, client=MagicMock(), memo=memo,
                budget_s=5.0, max_workers=10,
            )
            elapsed = time.time() - started

        assert not exhausted
        assert len(by_idx) == 10
        assert all(cap == 1_500.0 for cap in by_idx.values())
        assert elapsed < 1.5, f"expected parallel speedup, got {elapsed:.2f}s"

    def test_budget_exhaustion_marks_partial_reason(self):
        """When yfinance hangs past the budget, remaining indexes resolve
        to None and `budget_exhausted=True` is returned."""
        import time

        def stuck_lookup(client, ticker, memo):
            time.sleep(5)  # longer than budget
            return 1_000.0

        candidates = [self._make_candidate(f"T{i}") for i in range(5)]

        with patch.object(efm, "_load_market_cap_usd_mm", side_effect=stuck_lookup):
            by_idx, exhausted = efm._resolve_market_caps_parallel(
                candidates, client=MagicMock(), memo={},
                budget_s=0.3, max_workers=5,  # 0.3s < 5s per-call
            )

        assert exhausted is True
        # Every index is still present (as None), so scan()'s filter loop
        # can count them all as `market_cap_unknown_total`.
        assert set(by_idx.keys()) == set(range(5))
        assert all(v is None for v in by_idx.values())

    def test_memo_hit_fast_path_skips_threadpool(self):
        """All tickers already in memo → no pool spun up. Patch the
        executor class to raise if instantiated."""
        candidates = [self._make_candidate("KNOWN")]
        memo = {"KNOWN": 2_500.0}

        with patch.object(efm, "ThreadPoolExecutor",
                          side_effect=AssertionError("must not spin up pool on full memo hit")):
            by_idx, exhausted = efm._resolve_market_caps_parallel(
                candidates, client=MagicMock(), memo=memo,
            )

        assert by_idx == {0: 2_500.0}
        assert exhausted is False

    def test_worker_exception_yields_none_for_that_candidate(self):
        """One thread raising must not fail the whole run — the failing
        candidate maps to None, others resolve normally."""

        def flaky_lookup(client, ticker, memo):
            if ticker == "BAD":
                raise RuntimeError("yfinance rate-limited")
            return 900.0

        candidates = [
            self._make_candidate("GOOD1"),
            self._make_candidate("BAD"),
            self._make_candidate("GOOD2"),
        ]

        with patch.object(efm, "_load_market_cap_usd_mm", side_effect=flaky_lookup):
            by_idx, exhausted = efm._resolve_market_caps_parallel(
                candidates, client=MagicMock(), memo={},
                budget_s=5.0, max_workers=3,
            )

        assert exhausted is False
        assert by_idx[0] == 900.0
        assert by_idx[1] is None  # exception swallowed
        assert by_idx[2] == 900.0

    def test_empty_candidate_list_is_noop(self):
        """No candidates → no work, no budget consumed."""
        by_idx, exhausted = efm._resolve_market_caps_parallel(
            [], client=MagicMock(), memo={},
        )
        assert by_idx == {}
        assert exhausted is False

    def test_empty_ticker_maps_to_none_without_lookup(self):
        """Candidates with ticker=None short-circuit to None."""
        candidates = [
            (MagicMock(), None, "h1"),
            (MagicMock(), "", "h2"),
        ]
        with patch.object(efm, "_load_market_cap_usd_mm",
                          side_effect=AssertionError("no ticker, no lookup")):
            by_idx, _ = efm._resolve_market_caps_parallel(
                candidates, client=MagicMock(), memo={},
            )
        assert by_idx == {0: None, 1: None}


class TestScanSurfacesBudgetExhaustionAsPartialReason:
    """End-to-end: when the parallel market-cap pass hits its budget,
    scan() must surface `market_cap_budget_exhausted` in partial_reasons."""

    def test_scan_marks_partial_when_market_cap_budget_exhausted(self):
        cfg = _make_cfg(config={"days_back": 2, "market_cap_floor_usd_mm": 215})
        # Patch the parallel helper to simulate exhaustion directly, so we
        # don't depend on real threading timing in the test.
        def fake_resolver(candidates, client, memo, **kwargs):
            return {i: None for i in range(len(candidates))}, True

        mock_client = MagicMock()
        mock_client.openfigi_cache_backend.return_value = (None, None, None)
        with patch.object(efm, "SupabaseClient", return_value=mock_client), \
             patch.object(efm, "_rotation_state_for_mode", return_value=(["activist"], None)), \
             patch.object(efm, "_load_dedup", return_value={}), \
             patch.object(efm, "_save_dedup"), \
             patch.object(efm, "_efts_search", return_value=[_hit(form="8-K")]), \
             patch.object(efm, "_get_company_tickers", return_value=(["EXC"], "NYSE")), \
             patch.object(efm, "_resolve_market_caps_parallel", side_effect=fake_resolver), \
             patch.object(efm, "_has_merger_sibling", return_value=False), \
             patch("modal_workers.shared.openfigi_resolver.set_cache_backend"), \
             patch("modal_workers.shared.openfigi_resolver.resolve_ticker",
                   return_value=MagicMock(resolved=False, issuer_figi=None)), \
             patch.dict("os.environ", {"SEC_USER_AGENT": "ua@example.com"}):
            result = efm.scan(cfg)

        assert "market_cap_budget_exhausted" in result.run_metrics["partial_reasons"]
        assert result.status == "partial"


class TestHttpGetUsesSharedSession:
    """The `_http_get` seam routes through a single pooled Session so every
    SEC call reuses TCP/TLS. Verify the session is module-scoped (i.e., the
    same instance is returned across calls)."""

    def test_session_is_reused(self):
        # Reset the cached session to guarantee a clean check.
        efm._SEC_SESSION = None
        s1 = efm._sec_session()
        s2 = efm._sec_session()
        assert s1 is s2, "pooled session must be a singleton for the module"
        # Has an https adapter mounted.
        assert "https://" in s1.adapters
