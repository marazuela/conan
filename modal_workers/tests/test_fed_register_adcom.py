"""Tests for fed_register_adcom fetcher.

Pure-Python: HTTP session and SupabaseClient are stubbed via monkeypatch
fixtures. Asserts the title/date/sponsor extraction invariants and the
end-to-end fetch envelope shape on canned Federal Register responses.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

import pytest

from modal_workers.fetchers.universe import fed_register_adcom as M


# ---------------------------------------------------------------------------
# Title filter
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title,expected", [
    ("Cardiovascular and Renal Drugs Advisory Committee; Notice of Meeting", True),
    ("Vaccines and Related Biological Products Advisory Committee; Notice of Meeting", True),
    ("Advisory Committee; Blood Products Advisory Committee; Renewal", False),
    ("Anesthetic and Analgesic Drug Products Advisory Committee; Renewal", False),
    ("Establishment of a Public Docket; Request for Comments", False),
    ("ChemoCentryx, Inc.; Proposal To Withdraw Approval of New Drug Application", False),
    # "Establishment of a Public Docket" is FDA's standard pattern for the
    # comment docket attached to a meeting notice; not a committee establishment.
    # Should pass the title filter; sponsor / asset gates handle precision.
    ("Pharmacy Compounding Advisory Committee; Notice of Meeting; Establishment of a Public Docket", True),
])
def test_is_meeting_notice(title, expected):
    assert M._is_meeting_notice(title) is expected


# ---------------------------------------------------------------------------
# Meeting-date extraction from the free-text 'dates' field
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dates_text,expected", [
    ("The meeting will be held on July 23, 2026, from 8:00 a.m. to 4:30 p.m. Eastern Time.",
     "2026-07-23"),
    ("The meeting will be held on May 28, 2026, from 8:30 a.m. to 4:30 p.m.",
     "2026-05-28"),
    ("Meeting: October 1, 2026", "2026-10-01"),
    ("Date: November 15, 2026 ... Day 2 November 16, 2026", "2026-11-15"),
    ("", None),
    ("Comments due by June 30, 2026", None),  # no meeting/date keyword preceding
])
def test_parse_meeting_date(dates_text, expected):
    assert M._parse_meeting_date(dates_text) == expected


# ---------------------------------------------------------------------------
# Sponsor extraction + normalization
# ---------------------------------------------------------------------------

def test_sponsor_from_title_prefix():
    doc = {"title": "Acme Therapeutics, Inc.; Notice of Meeting", "abstract": ""}
    terms = M._sponsor_terms_from_doc(doc)
    # The exact prefix should be the first candidate
    assert terms
    assert terms[0].startswith("Acme")


def test_sponsor_from_abstract_when_title_is_panel_only():
    doc = {
        "title": "Cardiovascular and Renal Drugs Advisory Committee; Notice of Meeting",
        "abstract": "The committee will discuss the New Drug Application from Axsome Therapeutics for AXS-05.",
    }
    terms = M._sponsor_terms_from_doc(doc)
    # Abstract sniff should surface a candidate
    assert any("Axsome" in t for t in terms)


def test_sponsor_normalize_strips_corporate_suffixes():
    assert M._normalize_sponsor("Acme Therapeutics, Inc.") == "Acme"
    assert M._normalize_sponsor("Axsome Pharmaceuticals LLC") == "Axsome"
    assert M._normalize_sponsor("Sanofi S.A.") == "Sanofi"


# ---------------------------------------------------------------------------
# Content-hash stability
# ---------------------------------------------------------------------------

def test_content_hash_is_stable_and_unique():
    h1 = M._content_hash("2026-08122")
    h2 = M._content_hash("2026-08122")
    h3 = M._content_hash("2026-07361")
    assert h1 == h2
    assert h1 != h3
    assert h1.startswith("sha256:")
    assert len(h1) == len("sha256:") + 64


# ---------------------------------------------------------------------------
# Stubbed SupabaseClient + HTTP session for end-to-end fetch
# ---------------------------------------------------------------------------

class _StubResponse:
    def __init__(self, body: Dict[str, Any], status: int = 200):
        self._body = body
        self.status = status

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"http {self.status}")

    def json(self) -> Dict[str, Any]:
        return self._body


class _StubSession:
    def __init__(self, pages: List[Dict[str, Any]]):
        self._pages = pages
        self._calls = 0
        self.headers: Dict[str, str] = {}

    def get(self, url, params=None, timeout=None):
        body = self._pages[self._calls] if self._calls < len(self._pages) else {"results": []}
        self._calls += 1
        return _StubResponse(body)


class _StubClient:
    """Fake SupabaseClient.

    Asset resolution succeeds for sponsors whose normalized prefix is in
    `known_sponsors`. _rest_with_retry records every call for assertion.
    """

    def __init__(self, known_sponsors: Optional[List[str]] = None):
        self.known_sponsors = [s.lower() for s in (known_sponsors or [])]
        self.posted: List[Dict[str, Any]] = []

    def _rest(self, method, path, params=None, **_):
        if path == "fda_assets":
            sponsor_filter = (params or {}).get("sponsor_name") or ""
            for ks in self.known_sponsors:
                if ks in sponsor_filter.lower():
                    return [{"id": f"asset-{ks}"}]
            return []
        if path == "entities":
            return []
        return []

    def _rest_with_retry(self, method, path, json_body=None, prefer=None, **_):
        # Capture the POST shape; act as if a row was newly inserted
        # (ignore-duplicates returns the inserted rows; if conflict it'd return []).
        self.posted.extend(json_body or [])
        return list(json_body or [])


@pytest.fixture
def two_doc_response():
    """Page 1: a sponsor-resolvable notice + a panel-only notice."""
    return [{
        "results": [
            {
                "title": "Axsome Therapeutics, Inc.; Notice of Meeting",
                "abstract": "...",
                "publication_date": "2026-04-15",
                "document_number": "2026-08001",
                "html_url": "https://www.federalregister.gov/d/2026-08001",
                "dates": "The meeting will be held on July 23, 2026, from 8:00 a.m.",
            },
            {
                "title": "Pharmacy Compounding Advisory Committee; Notice of Meeting",
                "abstract": "General-purpose compounding discussion.",
                "publication_date": "2026-04-16",
                "document_number": "2026-07361",
                "html_url": "https://www.federalregister.gov/d/2026-07361",
                "dates": "The meeting will be held on August 1, 2026.",
            },
        ],
        "next_page_url": None,
    }]


def test_fetch_emits_event_for_resolvable_sponsor(monkeypatch, two_doc_response):
    monkeypatch.setattr(M, "_session", lambda: _StubSession(two_doc_response))
    client = _StubClient(known_sponsors=["axsome"])
    result = M.fetch(client, start_date=date(2026, 4, 1), end_date=date(2026, 5, 1))

    assert result["fetched"] == 2
    assert result["upserted"] == 1
    assert result["skipped_no_asset"] == 1
    assert result["errors"] == []
    # The inserted row must carry the parsed meeting date + adcom event_type
    assert len(client.posted) == 1
    row = client.posted[0]
    assert row["event_type"] == "adcom"
    assert row["event_status"] == "pending"
    assert row["event_date"] == "2026-07-23"
    assert row["asset_id"] == "asset-axsome"
    assert row["source_content_hash"].startswith("sha256:")
    assert row["extensions"]["fed_register_document_number"] == "2026-08001"


def test_fetch_dry_run_makes_no_supabase_writes(monkeypatch, two_doc_response):
    monkeypatch.setattr(M, "_session", lambda: _StubSession(two_doc_response))
    client = _StubClient(known_sponsors=["axsome"])
    result = M.fetch(client, start_date=date(2026, 4, 1), end_date=date(2026, 5, 1), dry_run=True)
    # Dry-run counts everything past title+date+sponsor_terms gates (asset
    # resolution is bypassed) — both fixture docs pass, so upserted=2.
    assert result["upserted"] == 2
    assert client.posted == []


def test_renewal_notices_filtered_before_date_parse(monkeypatch):
    pages = [{
        "results": [
            {
                "title": "Blood Products Advisory Committee; Renewal",
                "abstract": "",
                "publication_date": "2026-05-07",
                "document_number": "2026-09108",
                "html_url": "https://www.federalregister.gov/d/2026-09108",
                "dates": "The meeting will be held on June 1, 2026.",
            },
        ],
        "next_page_url": None,
    }]
    monkeypatch.setattr(M, "_session", lambda: _StubSession(pages))
    client = _StubClient(known_sponsors=["any"])
    result = M.fetch(client, start_date=date(2026, 5, 1), end_date=date(2026, 5, 31))
    assert result["fetched"] == 1
    assert result["skipped_not_meeting"] == 1
    assert result["upserted"] == 0
    assert client.posted == []
