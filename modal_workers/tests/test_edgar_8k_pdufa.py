"""Tests for edgar_8k_pdufa fetcher.

Stubs EDGAR FTS and SupabaseClient. Covers display_name parsing, hit→row
mapping, cross-query accession dedup, two-tier asset resolution (CIK first,
ticker fallback), and the end-to-end fetch envelope.

SEC_USER_AGENT is set in the fixture so _session() does not raise.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any, Dict, List, Optional

import pytest

from modal_workers.fetchers.universe import edgar_8k_pdufa as M


@pytest.fixture(autouse=True)
def _set_sec_user_agent(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Conan Tests test@example.com")
    # Reset module-level caches between tests so state doesn't leak.
    M._CIK_ASSET_CACHE.clear()
    M._TICKER_ASSET_CACHE.clear()


# ---------------------------------------------------------------------------
# display_names parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("display_name,expected_ticker", [
    ("Axsome Therapeutics, Inc.  (AXSM) (CIK 0001579428) (SIC 2834)", "AXSM"),
    ("Prelude Therapeutics Inc  (PRLD) (CIK 0001678660) (SIC 2836)", "PRLD"),
    ("Some Co (NO_TICKER) (CIK 0001234567) (SIC 2836)", "NO_TICKER"),
    ("Multi Class Co  (FOOA, FOOB) (CIK 0009999999) (SIC 6199)", "FOOA"),
    ("Bare Filer (CIK 0001111111)", None),  # no ticker cluster
    ("", None),
])
def test_extract_ticker(display_name, expected_ticker):
    assert M._extract_ticker(display_name) == expected_ticker


@pytest.mark.parametrize("display_name,expected_cik", [
    ("Axsome (AXSM) (CIK 0001579428) (SIC 2834)", "1579428"),
    ("Co (CIK 0000001234)", "1234"),
    ("No CIK here", None),
])
def test_extract_cik(display_name, expected_cik):
    assert M._extract_cik(display_name) == expected_cik


# ---------------------------------------------------------------------------
# Hit → row mapping
# ---------------------------------------------------------------------------

def test_map_hit_to_row_minimal():
    hit = {
        "_source": {
            "file_date": "2026-05-01",
            "display_names": ["Axsome Therapeutics, Inc.  (AXSM) (CIK 0001579428) (SIC 2834)"],
            "adsh": "0001579428-26-000123",
            "file_type": "8-K",
        }
    }
    row = M._map_hit_to_row(hit)
    assert row is not None
    assert row["accession"] == "0001579428-26-000123"
    assert row["cik"] == "1579428"
    assert row["ticker"] == "AXSM"
    assert row["file_date"] == "2026-05-01"
    # company_name is the slice before " (CIK" — matches the existing
    # sec_8k_mna pattern; the trailing ticker cluster is intentionally kept
    # (downstream consumers parse ticker separately).
    assert row["company_name"].startswith("Axsome Therapeutics, Inc.")
    assert "/Archives/edgar/data/1579428/" in (row["source_url"] or "")


def test_map_hit_to_row_returns_none_when_no_filing_date():
    hit = {"_source": {"display_names": ["X (CIK 0000001)"], "adsh": "z"}}
    assert M._map_hit_to_row(hit) is None


def test_content_hash_is_stable_and_unique():
    h1 = M._content_hash("0001579428-26-000123", "1579428")
    h2 = M._content_hash("0001579428-26-000123", "1579428")
    h3 = M._content_hash("0001579428-26-000456", "1579428")
    assert h1 == h2
    assert h1 != h3
    assert h1.startswith("sha256:")


# ---------------------------------------------------------------------------
# Stubbed EDGAR + SupabaseClient for end-to-end fetch
# ---------------------------------------------------------------------------

class _StubResponse:
    def __init__(self, body, status=200):
        self._body, self.status = body, status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"http {self.status}")

    def json(self):
        return self._body


class _StubSession:
    """Returns a canned response per HTTP GET call, cycling through `pages`."""
    def __init__(self, pages: List[Dict[str, Any]]):
        self._pages = pages
        self._i = 0
        self.headers: Dict[str, str] = {}

    def get(self, url, params=None, timeout=None):
        body = self._pages[self._i] if self._i < len(self._pages) else {"hits": {"hits": [], "total": {"value": 0}}}
        self._i += 1
        return _StubResponse(body)


def _empty_hits():
    return {"hits": {"hits": [], "total": {"value": 0}}}


def _hit(file_date, ticker, cik, accession="000123-26-000001"):
    cik_padded = cik.zfill(10)
    display = f"Co (CIK {cik_padded}) (SIC 2836)" if ticker is None \
        else f"Co  ({ticker}) (CIK {cik_padded}) (SIC 2836)"
    return {
        "_source": {
            "file_date": file_date,
            "display_names": [display],
            "adsh": accession,
            "file_type": "8-K",
        }
    }


class _StubClient:
    """CIK→asset map + ticker→asset map. _rest_with_retry captures inserts."""
    def __init__(
        self,
        cik_assets: Optional[Dict[str, str]] = None,
        ticker_assets: Optional[Dict[str, str]] = None,
    ):
        self.cik_assets = cik_assets or {}
        self.ticker_assets = ticker_assets or {}
        # CIK → entity_id via entity_identifiers; for the test, just identity-map.
        self.cik_to_entity = {cik: f"entity-{cik}" for cik in self.cik_assets}
        self.posted: List[Dict[str, Any]] = []

    def _rest(self, method, path, params=None, **_):
        if path == "entity_identifiers":
            id_value = (params or {}).get("id_value", "")
            # Param is "eq.<cik>" — strip prefix
            cik = id_value.replace("eq.", "")
            ent = self.cik_to_entity.get(cik)
            return [{"entity_id": ent}] if ent else []
        if path == "fda_assets":
            entity_filter = (params or {}).get("entity_id", "")
            ticker_filter = (params or {}).get("ticker", "")
            if entity_filter.startswith("eq."):
                ent = entity_filter.replace("eq.", "")
                # Reverse-lookup CIK from entity
                for cik, e in self.cik_to_entity.items():
                    if e == ent and cik in self.cik_assets:
                        return [{"id": self.cik_assets[cik]}]
                return []
            if ticker_filter.startswith("eq."):
                t = ticker_filter.replace("eq.", "")
                aid = self.ticker_assets.get(t)
                return [{"id": aid}] if aid else []
        return []

    def _rest_with_retry(self, method, path, json_body=None, prefer=None, **_):
        self.posted.extend(json_body or [])
        return list(json_body or [])


# ---------------------------------------------------------------------------
# End-to-end fetch
# ---------------------------------------------------------------------------

def test_fetch_inserts_for_cik_resolved_filer(monkeypatch):
    pages = [
        # 1st query response, 1 hit, then empty pages for subsequent queries
        {"hits": {"hits": [_hit("2026-05-01", "AXSM", "1579428", "000-26-1")],
                  "total": {"value": 1}}},
        _empty_hits(),  # second query
        _empty_hits(),  # third query
    ]
    monkeypatch.setattr(M, "_session", lambda: _StubSession(pages))
    client = _StubClient(cik_assets={"1579428": "asset-axsm"})

    result = M.fetch(client, start_date=date(2026, 4, 1), end_date=date(2026, 5, 11))
    assert result["fetched"] == 1
    assert result["upserted"] == 1
    assert result["skipped_no_asset"] == 0
    assert len(client.posted) == 1
    row = client.posted[0]
    assert row["event_type"] == "pdufa"
    assert row["event_status"] == "pending"
    assert row["event_date"] is None
    assert row["asset_id"] == "asset-axsm"
    assert row["extensions"]["edgar_accession"] == "000-26-1"
    assert row["extensions"]["edgar_cik"] == "1579428"


def test_fetch_falls_back_to_ticker_when_cik_misses(monkeypatch):
    pages = [
        {"hits": {"hits": [_hit("2026-05-01", "PRLD", "1678660", "000-26-2")],
                  "total": {"value": 1}}},
        _empty_hits(),
        _empty_hits(),
    ]
    monkeypatch.setattr(M, "_session", lambda: _StubSession(pages))
    # No CIK match, but ticker is in fda_assets
    client = _StubClient(cik_assets={}, ticker_assets={"PRLD": "asset-prld"})
    result = M.fetch(client, start_date=date(2026, 4, 1), end_date=date(2026, 5, 11))
    assert result["upserted"] == 1
    assert client.posted[0]["asset_id"] == "asset-prld"


def test_fetch_dedupes_same_accession_across_queries(monkeypatch):
    # Same accession appears in two of the three PDUFA queries
    h = _hit("2026-05-01", "AXSM", "1579428", "DUPACCESSION-1")
    pages = [
        {"hits": {"hits": [h], "total": {"value": 1}}},
        {"hits": {"hits": [h], "total": {"value": 1}}},
        _empty_hits(),
    ]
    monkeypatch.setattr(M, "_session", lambda: _StubSession(pages))
    client = _StubClient(cik_assets={"1579428": "asset-axsm"})
    result = M.fetch(client, start_date=date(2026, 4, 1), end_date=date(2026, 5, 11))
    assert result["fetched"] == 2
    assert result["duplicate_accession"] == 1
    assert result["upserted"] == 1
    assert len(client.posted) == 1


def test_fetch_skips_when_no_fda_assets_match(monkeypatch):
    pages = [
        {"hits": {"hits": [_hit("2026-05-01", "UNKN", "9999999", "000-26-3")],
                  "total": {"value": 1}}},
        _empty_hits(),
        _empty_hits(),
    ]
    monkeypatch.setattr(M, "_session", lambda: _StubSession(pages))
    client = _StubClient()  # empty maps
    result = M.fetch(client, start_date=date(2026, 4, 1), end_date=date(2026, 5, 11))
    assert result["upserted"] == 0
    assert result["skipped_no_asset"] == 1
    assert client.posted == []


def test_fetch_dry_run_writes_nothing(monkeypatch):
    pages = [
        {"hits": {"hits": [_hit("2026-05-01", "AXSM", "1579428", "000-26-4")],
                  "total": {"value": 1}}},
        _empty_hits(),
        _empty_hits(),
    ]
    monkeypatch.setattr(M, "_session", lambda: _StubSession(pages))
    client = _StubClient(cik_assets={"1579428": "asset-axsm"})
    result = M.fetch(client, start_date=date(2026, 4, 1), end_date=date(2026, 5, 11),
                     dry_run=True)
    assert result["upserted"] == 1
    assert client.posted == []
