"""
Tests for edgar_filing_monitor — merger-clause false-positive defense.

Covers the 2026-04-20 DLQ incident (QXO-TopBuild $17B merger announcement
on 2026-04-18 surfaced as two activist_keyword signals via "board
representation", both DLQ'd post_edge by thesis_writer). The defense is a
co-filing proximity check: an activist keyword in 8-K is suppressed if the
same CIK filed 425 / PREM14A / SC TO-T within ±3 days.

Run: python -m pytest modal_workers/tests/test_edgar_filing_monitor.py -v
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

def _hit(form="8-K", cik="0000012345", adsh="0001-25-000100",
         file_date="2026-04-18"):
    return {
        "cik": cik,
        "adsh": adsh,
        "form": form,
        "file_date": file_date,
        "company_name": "Example Corp",
        "company_raw": "Example Corp (CIK 12345)",
        "file_description": "Entry into a Material Definitive Agreement",
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


def _run_scan(cfg, *, keyword_hits, sibling_patch):
    """Run efm.scan() with external IO stubbed.

    - Rotation forced to 'activist' (index 0).
    - _efts_search returns `keyword_hits` for every keyword query.
    - `sibling_patch` is the `_mock._patch` object produced by
      `patch.object(efm, "_has_merger_sibling", ...)`. Caller controls the
      return value or side_effect; this helper only enters the context.
    - Filing-type scan (SC 13D, NT 10-K) returns empty.
    """
    rotation = {"rotation_index": -1, "scan_history": {}}  # next = 0 = activist

    mock_client = MagicMock()
    mock_client.openfigi_cache_backend.return_value = (None, None, None)

    def efts_side_effect(query, date_from, date_to, form_type="", **kwargs):
        if form_type:
            return []  # filing-type scan — not exercised by these tests
        return keyword_hits

    with patch.object(efm, "SupabaseClient", return_value=mock_client), \
         patch.object(efm, "_load_rotation", return_value=rotation), \
         patch.object(efm, "_save_rotation"), \
         patch.object(efm, "_load_dedup", return_value={}), \
         patch.object(efm, "_save_dedup"), \
         patch.object(efm, "_efts_search", side_effect=efts_side_effect), \
         patch.object(efm, "_get_company_tickers", return_value=(["EXC"], "NYSE")), \
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
