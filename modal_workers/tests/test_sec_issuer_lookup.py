"""Tests for sec_issuer_lookup.IssuerIndex.

All HTTP / Storage operations are mocked. Fixture tickers list has a handful
of real public companies plus contrived ambiguities.
"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from modal_workers.shared import sec_issuer_lookup as sil


# Fixture: a subset of SEC's tickers list shape.
_FIXTURE_ENTRIES = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1318605, "ticker": "TSLA", "title": "Tesla, Inc."},
    "2": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
    "3": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
    "4": {"cik_str": 1652044, "ticker": "GOOG", "title": "Alphabet Inc."},  # class C
    "5": {"cik_str": 1750, "ticker": "AIR", "title": "AAR Corp"},
    "6": {"cik_str": 1492633, "ticker": "REVG", "title": "REV Group, Inc."},
    # Two distinct "Apex Inc." issuers → ambiguous
    "7": {"cik_str": 1111111, "ticker": "APX1", "title": "Apex Inc."},
    "8": {"cik_str": 2222222, "ticker": "APX2", "title": "Apex Inc."},
}


class TestNormalize:
    @pytest.mark.parametrize("inp,out", [
        ("Apple Inc.", "appleinc"),
        ("Apple, Inc.", "appleinc"),
        ("APPLE INC", "appleinc"),
        ("  Apple  Inc.  ", "appleinc"),
        ("", ""),
    ])
    def test_normalize(self, inp, out):
        assert sil._normalize(inp) == out

    def test_ampersand_collapsed(self):
        assert sil._normalize("Procter & Gamble Co.") == "proctergambleco"


class TestStripSuffix:
    @pytest.mark.parametrize("inp,out", [
        ("Apple Inc.", "apple"),
        ("Tesla, Inc.", "tesla"),
        ("Microsoft Corp", "microsoft"),
        ("Alphabet Inc.", "alphabet"),
        ("REV Group, Inc.", "revgroup"),
        ("Widget Holdings, Inc.", "widget"),
        ("Samsung Electronics Co.", "samsungelectronics"),
    ])
    def test_strip_suffix(self, inp, out):
        assert sil._strip_suffix(inp) == out

    def test_empty(self):
        assert sil._strip_suffix("") == ""


class TestIssuerIndex:

    def _index(self):
        return sil.IssuerIndex(_FIXTURE_ENTRIES)

    def test_exact_match(self):
        idx = self._index()
        m = idx.resolve("Apple Inc.")
        assert m is not None
        assert m.ticker == "AAPL"
        assert m.cik == "0000320193"
        assert m.match_kind == "exact"

    def test_exact_match_lowercased(self):
        idx = self._index()
        m = idx.resolve("apple inc.")
        assert m and m.ticker == "AAPL"

    def test_punctuation_tolerance(self):
        idx = self._index()
        assert idx.resolve("Apple, Inc.").ticker == "AAPL"
        assert idx.resolve("APPLE INC").ticker == "AAPL"

    def test_suffix_trimmed_match(self):
        idx = self._index()
        # Input missing ".", SEC stores "Tesla, Inc."
        m = idx.resolve("Tesla Inc")
        assert m is not None
        assert m.ticker == "TSLA"

    def test_suffix_trimmed_real_flood_case(self):
        """The exact case from the 2026-04-23 log flood."""
        idx = self._index()
        m = idx.resolve("REV Group Inc")
        assert m is not None
        assert m.ticker == "REVG"

    def test_multi_share_class_same_cik_returns_one(self):
        """Alphabet has GOOGL + GOOG but same CIK — treat as unique."""
        idx = self._index()
        m = idx.resolve("Alphabet Inc.")
        assert m is not None
        assert m.cik == "0001652044"
        assert m.ticker in ("GOOGL", "GOOG")

    def test_ambiguous_returns_none(self):
        idx = self._index()
        m = idx.resolve("Apex Inc.")
        assert m is None

    def test_suffix_trimmed_exact_when_key_matches(self):
        idx = self._index()
        # "Microsoft" normalizes to "microsoft"; _strip_suffix("Microsoft Corp")
        # also yields "microsoft" — so this is an exact suffix-trimmed hit,
        # not a prefix match.
        m = idx.resolve("Microsoft")
        assert m is not None
        assert m.ticker == "MSFT"
        assert m.match_kind == "suffix_trimmed"

    def test_startswith_unique_match(self):
        idx = self._index()
        # "Microsof" has no exact key but is a unique prefix of "microsoft"
        m = idx.resolve("Microsof")
        assert m is not None
        assert m.ticker == "MSFT"
        assert m.match_kind == "startswith"

    def test_startswith_too_short_rejected(self):
        idx = self._index()
        # "A" is too short (< 4 chars after normalization)
        assert idx.resolve("A") is None

    def test_unknown_name_returns_none(self):
        idx = self._index()
        assert idx.resolve("Not A Real Company LLC") is None

    def test_empty_input(self):
        idx = self._index()
        assert idx.resolve("") is None


class TestLoad:

    def _fake_client_no_cache(self):
        client = MagicMock()
        client.read_cache.return_value = None
        return client

    def _fake_client_with_cache(self, payload):
        client = MagicMock()
        client.read_cache.return_value = json.dumps(payload).encode("utf-8")
        return client

    def test_fetches_when_cache_empty(self):
        client = self._fake_client_no_cache()
        fake_resp = MagicMock(status_code=200)
        fake_resp.json.return_value = _FIXTURE_ENTRIES
        with patch.object(sil.requests, "get", return_value=fake_resp) as rg:
            idx = sil.IssuerIndex.load(client, user_agent="Test UA")
        assert idx is not None
        assert idx.resolve("Apple Inc.").ticker == "AAPL"
        rg.assert_called_once()
        client.write_cache.assert_called_once()

    def test_uses_cache_when_fresh(self):
        client = self._fake_client_with_cache({
            "cached_at": time.time(),
            "entries": _FIXTURE_ENTRIES,
        })
        with patch.object(sil.requests, "get") as rg:
            idx = sil.IssuerIndex.load(client, user_agent="Test UA")
        assert idx is not None
        assert idx.resolve("Tesla, Inc.").ticker == "TSLA"
        rg.assert_not_called()

    def test_refetches_when_cache_stale(self):
        client = self._fake_client_with_cache({
            "cached_at": time.time() - (31 * 24 * 3600),
            "entries": _FIXTURE_ENTRIES,
        })
        fake_resp = MagicMock(status_code=200)
        fake_resp.json.return_value = _FIXTURE_ENTRIES
        with patch.object(sil.requests, "get", return_value=fake_resp) as rg:
            idx = sil.IssuerIndex.load(client, user_agent="Test UA")
        assert idx is not None
        rg.assert_called_once()

    def test_returns_none_on_total_failure(self):
        client = self._fake_client_no_cache()
        with patch.object(sil.requests, "get",
                          side_effect=requests.exceptions.ConnectionError("no net")):
            assert sil.IssuerIndex.load(client, user_agent="Test UA") is None

    def test_skip_cache_forces_fetch(self):
        client = self._fake_client_with_cache({
            "cached_at": time.time(),
            "entries": _FIXTURE_ENTRIES,
        })
        fake_resp = MagicMock(status_code=200)
        fake_resp.json.return_value = _FIXTURE_ENTRIES
        with patch.object(sil.requests, "get", return_value=fake_resp) as rg:
            sil.IssuerIndex.load(client, user_agent="UA", skip_cache=True)
        rg.assert_called_once()
