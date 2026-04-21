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
        with patch.object(efm.requests, "get",
                          return_value=_fake_efts_response([{"adsh": "0001-25-000425"}])):
            assert efm._has_merger_sibling(
                "12345", "2026-04-18",
                user_agent="ua@example.com", cache=cache) is True

    def test_returns_false_when_no_sibling_filing(self):
        cache: dict = {}
        with patch.object(efm.requests, "get",
                          return_value=_fake_efts_response([])):
            assert efm._has_merger_sibling(
                "12345", "2026-04-18",
                user_agent="ua@example.com", cache=cache) is False

    def test_cache_dedupes_repeat_call(self):
        cache: dict = {}
        with patch.object(efm.requests, "get",
                          return_value=_fake_efts_response([{"adsh": "x"}])) as mock_get:
            efm._has_merger_sibling("12345", "2026-04-18",
                                    user_agent="u", cache=cache)
            efm._has_merger_sibling("12345", "2026-04-18",
                                    user_agent="u", cache=cache)
            assert mock_get.call_count == 1

    def test_different_cik_triggers_new_call(self):
        cache: dict = {}
        with patch.object(efm.requests, "get",
                          return_value=_fake_efts_response([])) as mock_get:
            efm._has_merger_sibling("12345", "2026-04-18",
                                    user_agent="u", cache=cache)
            efm._has_merger_sibling("67890", "2026-04-18",
                                    user_agent="u", cache=cache)
            assert mock_get.call_count == 2

    def test_network_error_fails_open(self):
        """On RequestException the helper returns False (does not suppress)."""
        cache: dict = {}
        with patch.object(efm.requests, "get",
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
        with patch.object(efm.requests, "get",
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
    with patch.object(efm.requests, "get", side_effect=[timeout, success]) as mock_get:
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
    with patch.object(efm.requests, "get", side_effect=error):
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
