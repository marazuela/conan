"""
Focused regression tests for EDGAR issuer-level SPAC/shell filtering.

Run:
  PYTHONDONTWRITEBYTECODE=1 python3 -m pytest \
    unified_system/unified_system/tools/test_edgar_spac_filter.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import edgar_filing_monitor as efm


def _write_filter(tmp_path: Path, **overrides) -> Path:
    base = {
        "_schema_version": 1,
        "_description": "test fixture",
        "blocked_ciks": [],
        "name_patterns_ci": [],
        "description_patterns_ci": [],
        "allowlist_tickers": [],
        "allowlist_ciks": [],
    }
    base.update(overrides)
    path = tmp_path / "edgar_issuer_filter.json"
    path.write_text(json.dumps(base), encoding="utf-8")
    return path


def _install_filter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **overrides) -> Path:
    path = _write_filter(tmp_path, **overrides)
    monkeypatch.setattr(efm, "ISSUER_FILTER_FILE", str(path))
    monkeypatch.setattr(efm, "_ISSUER_FILTER_CACHE", None)
    return path


def _hit(company_name: str,
         *,
         cik: str = "0000000000",
         form: str = "8-K",
         file_description: str = "Entry into a Material Definitive Agreement",
         adsh: str = "0001-26-000001") -> dict:
    return {
        "cik": cik,
        "adsh": adsh,
        "company_name": company_name,
        "company_raw": company_name,
        "form": form,
        "file_date": "2026-04-21",
        "file_description": file_description,
        "filing_url": f"https://www.sec.gov/Archives/{adsh}",
        "sics": [],
    }


class TestIssuerFilterHelper:
    def test_blocks_known_spac_name_pattern(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        _install_filter(
            monkeypatch,
            tmp_path,
            name_patterns_ci=[r"\bacquisition\s+corp(?:oration)?\b"],
        )
        blocked, reason, ticker = efm._is_spac_or_shell_issuer(
            _hit("JATT II Acquisition Corp.", cik="0002112446"),
            cik="0002112446",
        )
        assert blocked is True
        assert reason.startswith("name_pattern:")
        assert ticker is None

    def test_blocks_shell_description_pattern(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        _install_filter(
            monkeypatch,
            tmp_path,
            description_patterns_ci=[r"\bblank\s+check\s+company\b"],
        )
        blocked, reason, _ = efm._is_spac_or_shell_issuer(
            _hit(
                "Example Holdings",
                file_description="Blank check company formed for the purpose of effecting a merger",
            ),
            cik="0001999999",
        )
        assert blocked is True
        assert reason.startswith("description_pattern:")

    def test_allowlist_ticker_overrides_name_pattern(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        _install_filter(
            monkeypatch,
            tmp_path,
            name_patterns_ci=[r"\bacquisition\s+corp(?:oration)?\b"],
            allowlist_tickers=["RPAY"],
        )
        with patch.object(efm, "_get_company_tickers", return_value=(["RPAY"], "NASDAQ")):
            blocked, reason, ticker = efm._is_spac_or_shell_issuer(
                _hit("Repay Acquisition Corp", cik="0001720592"),
                cik="0001720592",
            )
        assert blocked is False
        assert reason is None
        assert ticker == "RPAY"

    def test_blocked_cik_short_circuits_without_ticker_lookup(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        _install_filter(monkeypatch, tmp_path, blocked_ciks=["0002028516"])
        with patch.object(efm, "_get_company_tickers", side_effect=AssertionError("ticker lookup should not run")):
            blocked, reason, _ = efm._is_spac_or_shell_issuer(
                _hit("Archimedes Tech SPAC Partners II Co.", cik="0002028516"),
                cik="0002028516",
            )
        assert blocked is True
        assert reason == "blocked_cik"


class TestScanFlowOrdering:
    def test_scan_keywords_filters_before_novelty_and_ticker_lookup(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        _install_filter(monkeypatch, tmp_path, blocked_ciks=["0002028516"])
        metrics = efm._new_run_metrics(0.0)
        hit = _hit("Archimedes Tech SPAC Partners II Co.", cik="0002028516")
        monkeypatch.setattr(efm, "_efts_search", lambda **kwargs: [hit])
        monkeypatch.setattr(efm, "_load_dedup_log", lambda filepath: {})
        monkeypatch.setattr(efm, "_save_dedup_log", lambda filepath, log: None)
        monkeypatch.setattr(efm, "_is_novel", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("novelty check should not run for filtered issuer")))
        monkeypatch.setattr(efm, "_get_company_tickers", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ticker lookup should not run for filtered issuer")))

        signals = efm.scan_keywords(categories=["activist"], days_back=2, market_cap_filter=False, budget_s=0.0, metrics=metrics)
        assert signals == []
        assert metrics["issuer_filtered_total"] == 1
        assert metrics["issuer_filtered_by_reason"]["blocked_cik"] == 1

    def test_scan_filing_types_filters_before_ticker_lookup(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        _install_filter(monkeypatch, tmp_path, blocked_ciks=["0001865248"])
        metrics = efm._new_run_metrics(0.0)
        hit = _hit("Piermont Valley Acquisition Corp", cik="0001865248", form="SC 13D")
        monkeypatch.setattr(efm, "_efts_search", lambda **kwargs: [hit])
        monkeypatch.setattr(efm, "_get_company_tickers", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ticker lookup should not run for filtered issuer")))

        signals = efm.scan_filing_types(days_back=2, market_cap_filter=False, budget_s=0.0, metrics=metrics)
        assert signals == []
        assert metrics["issuer_filtered_total"] == 1
        assert metrics["issuer_filtered_by_reason"]["blocked_cik"] == 1

    def test_scan_keywords_allows_operating_company(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        _install_filter(
            monkeypatch,
            tmp_path,
            name_patterns_ci=[r"\bacquisition\s+corp(?:oration)?\b"],
            allowlist_ciks=["0001720592"],
        )
        metrics = efm._new_run_metrics(0.0)
        hit = _hit("Repay Acquisition Corp", cik="0001720592")
        monkeypatch.setattr(efm, "_efts_search", lambda **kwargs: [hit])
        monkeypatch.setattr(efm, "_load_dedup_log", lambda filepath: {})
        monkeypatch.setattr(efm, "_save_dedup_log", lambda filepath, log: None)
        monkeypatch.setattr(efm, "_is_novel", lambda *args, **kwargs: True)
        monkeypatch.setattr(efm, "_get_company_tickers", lambda cik: (["RPAY"], "NASDAQ"))

        signals = efm.scan_keywords(categories=["activist"], days_back=2, market_cap_filter=False, budget_s=0.0, metrics=metrics)
        assert len(signals) == len(efm.SIGNAL_KEYWORDS["activist"])
        assert all(signal["ticker"] == "RPAY" for signal in signals)
        assert metrics["issuer_filtered_total"] == 0
