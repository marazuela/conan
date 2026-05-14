from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from modal_workers.shared import fda_advisory_calendar as cal
from modal_workers.shared.fda_advisory_calendar import (
    Meeting,
    _detect_committee,
    _parse_meeting_date,
    fetch_advisory_committee_meetings,
    hydrate_watchlist_adcom_dates,
)


# ---------------------------------------------------------------------------
# _parse_meeting_date / _detect_committee
# ---------------------------------------------------------------------------

def test_parse_meeting_date_handles_canonical_phrasing():
    text = "FDA is announcing a meeting on January 12, 2026 to discuss XYZ."
    assert _parse_meeting_date(text) == "2026-01-12"


def test_parse_meeting_date_handles_two_day_range():
    # "March 4-5, 2026" — we collapse to the first day.
    text = "The committee meeting will be held on March 4-5, 2026 at FDA HQ."
    assert _parse_meeting_date(text) == "2026-03-04"


def test_parse_meeting_date_returns_none_when_absent():
    assert _parse_meeting_date("This notice has no date references.") is None


def test_detect_committee_recognizes_odac():
    assert _detect_committee("Oncologic Drugs Advisory Committee (ODAC) meeting") == "ODAC"


def test_detect_committee_returns_none_when_unknown():
    assert _detect_committee("Quarterly review notice with no acronym") is None


# ---------------------------------------------------------------------------
# fetch_advisory_committee_meetings
# ---------------------------------------------------------------------------

def _fake_federal_register_payload() -> dict:
    return {
        "results": [
            {
                "title": ("Oncologic Drugs Advisory Committee (ODAC) meeting on "
                          "ImaginaryDrug for metastatic NSCLC"),
                "abstract": ("Notice of meeting on March 4, 2026 to consider "
                             "the new drug application for ImaginaryDrug."),
                "publication_date": "2026-04-15",
                "html_url": "https://www.federalregister.gov/d/2026-12345",
            },
            {
                "title": "Quarterly procedural notice",
                "abstract": "No meeting referenced.",
                "publication_date": "2026-04-12",
                "html_url": "https://www.federalregister.gov/d/2026-12300",
            },
        ],
    }


def test_fetch_advisory_committee_meetings_parses_real_shape(monkeypatch):
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = _fake_federal_register_payload()
    fake_resp.raise_for_status = lambda: None
    monkeypatch.setattr(cal.requests, "get", lambda *_a, **_kw: fake_resp)

    fake_client = MagicMock()
    fake_client.read_cache.return_value = None
    fake_client.write_cache.return_value = None

    meetings = fetch_advisory_committee_meetings(client=fake_client)
    assert len(meetings) == 2
    odac = next(m for m in meetings if "ImaginaryDrug" in m.title)
    assert odac.committee == "ODAC"
    assert odac.meeting_date == "2026-03-04"


def test_fetch_parses_meeting_date_from_structured_dates_field(monkeypatch):
    """Real FDA AdComm notices put the meeting date in the structured `dates`
    field, NOT the abstract (which is boilerplate). 2026-05-14 production
    invocation returned 5 fetched / 0 upserted because the helper was only
    parsing title + abstract. Regression guard: ensure a notice whose
    `dates` field carries the only date string still gets meeting_date."""
    payload = {
        "results": [
            {
                "title": ("Vaccines and Related Biological Products Advisory "
                          "Committee; Notice of Meeting"),
                # Boilerplate abstract — no parseable date.
                "abstract": ("The Food and Drug Administration (FDA) announces "
                             "a forthcoming public advisory committee meeting "
                             "of the Vaccines and Related Biological Products "
                             "Advisory Committee."),
                # Structured DATES section — where the real date lives.
                "dates": ("The meeting will be held on May 28, 2026, "
                          "from 8:30 a.m. to 4:30 p.m. Eastern Time."),
                "publication_date": "2026-04-27",
                "html_url": "https://www.federalregister.gov/d/2026-99999",
            },
        ],
    }
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = payload
    fake_resp.raise_for_status = lambda: None
    monkeypatch.setattr(cal.requests, "get", lambda *_a, **_kw: fake_resp)

    fake_client = MagicMock()
    fake_client.read_cache.return_value = None
    fake_client.write_cache.return_value = None

    meetings = fetch_advisory_committee_meetings(client=fake_client)
    assert len(meetings) == 1
    assert meetings[0].meeting_date == "2026-05-28"


def test_fetch_returns_empty_on_http_failure(monkeypatch):
    import requests as _r

    def boom(*_a, **_kw):
        raise _r.exceptions.ConnectionError("boom")

    monkeypatch.setattr(cal.requests, "get", boom)
    fake_client = MagicMock()
    fake_client.read_cache.return_value = None
    fake_client.write_cache.return_value = None

    assert fetch_advisory_committee_meetings(client=fake_client) == []


# ---------------------------------------------------------------------------
# hydrate_watchlist_adcom_dates
# ---------------------------------------------------------------------------

def _meeting(meeting_date: str, drug: str = "ImaginaryDrug",
             committee: str = "ODAC") -> Meeting:
    return Meeting(
        publication_date="2026-04-15",
        meeting_date=meeting_date,
        title=f"{committee} meeting for {drug}",
        abstract=f"Notice of meeting on {meeting_date} to consider {drug}.",
        committee=committee,
        source_url="https://www.federalregister.gov/d/test",
    )


def test_adcom_hydration_updates_active_entry():
    today = datetime.now(timezone.utc).date()
    future_md = (today + timedelta(days=20)).isoformat()
    watchlist = [{"ticker": "ABCD", "drug_name": "ImaginaryDrug",
                  "adcom_date": None, "notes": "", "status": "active"}]
    meetings = [_meeting(future_md)]

    updated = hydrate_watchlist_adcom_dates(watchlist, meetings)
    assert updated == ["ABCD"]
    assert watchlist[0]["adcom_date"] == future_md


def test_adcom_hydration_skips_past_meeting():
    today = datetime.now(timezone.utc).date()
    past_md = (today - timedelta(days=5)).isoformat()
    watchlist = [{"ticker": "ABCD", "drug_name": "ImaginaryDrug",
                  "adcom_date": None, "notes": "", "status": "active"}]
    meetings = [_meeting(past_md)]

    assert hydrate_watchlist_adcom_dates(watchlist, meetings) == []
    assert watchlist[0]["adcom_date"] is None


def test_adcom_hydration_skips_auto_discovered_placeholder():
    today = datetime.now(timezone.utc).date()
    future_md = (today + timedelta(days=20)).isoformat()
    watchlist = [{"ticker": "ABCD", "drug_name": "(auto-discovered)",
                  "adcom_date": None, "notes": "", "status": "active"}]
    meetings = [_meeting(future_md)]
    assert hydrate_watchlist_adcom_dates(watchlist, meetings) == []


def test_adcom_hydration_does_not_overwrite_more_recent_existing_date():
    today = datetime.now(timezone.utc).date()
    earlier = (today + timedelta(days=10)).isoformat()
    later = (today + timedelta(days=30)).isoformat()
    watchlist = [{"ticker": "ABCD", "drug_name": "ImaginaryDrug",
                  "adcom_date": earlier, "notes": "", "status": "active"}]
    meetings = [_meeting(later)]
    # We only want to advance to a *new* meeting earlier than the existing one;
    # a later-dated meeting shouldn't overwrite an existing earlier one.
    assert hydrate_watchlist_adcom_dates(watchlist, meetings) == []
    assert watchlist[0]["adcom_date"] == earlier
