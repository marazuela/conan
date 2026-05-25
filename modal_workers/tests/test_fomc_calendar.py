"""Phase 3a — tests for FOMC calendar parser.

Pure-function tests for `parse_fomc_html`. Uses a representative fixture
mirroring the federalreserve.gov panel structure so the parser is exercised
end-to-end without hitting the network.

Run: python -m pytest modal_workers/tests/test_fomc_calendar.py -v
"""
from __future__ import annotations

import pytest

from modal_workers.fetchers.universe.fomc_calendar import (
    parse_fomc_html,
    fetch,
)


# A condensed fixture modeled on the real federalreserve.gov calendar layout.
# Real panel: <h4>2026 FOMC Meetings</h4> followed by meeting rows then a
# "Minutes" header followed by minutes-release rows. The parser doesn't
# require valid HTML — it walks plain text for "YYYY FOMC Meetings" + month
# regex matches.
_FIXTURE_2026 = """
2026 FOMC Meetings

January 27-28
March 17-18
April 28-29
June 16-17
July 28-29
September 15-16
November 3-4
December 15-16

Minutes
February 18 (PDF)
April 8 (PDF)
May 20 (PDF)
"""

_FIXTURE_TWO_YEARS = (
    """
2025 FOMC Meetings

January 28-29
March 18-19

Minutes
February 19
"""
    + _FIXTURE_2026
)


# ---------------------------------------------------------------------------
# Scheduled meetings — end-day extraction
# ---------------------------------------------------------------------------


def test_two_day_meetings_extract_end_day_as_announcement_date():
    rows = parse_fomc_html(_FIXTURE_2026, year_filter=2026)
    dates_by_type = {(r["fomc_date"], r["meeting_type"]) for r in rows}
    assert ("2026-01-28", "scheduled") in dates_by_type
    assert ("2026-03-18", "scheduled") in dates_by_type
    assert ("2026-12-16", "scheduled") in dates_by_type


def test_all_eight_scheduled_meetings_present():
    rows = parse_fomc_html(_FIXTURE_2026, year_filter=2026)
    scheduled = [r for r in rows if r["meeting_type"] == "scheduled"]
    assert len(scheduled) == 8, f"expected 8 scheduled meetings, got {len(scheduled)}: {scheduled}"


# ---------------------------------------------------------------------------
# Minutes-release dates
# ---------------------------------------------------------------------------


def test_minutes_extracted_with_start_day():
    rows = parse_fomc_html(_FIXTURE_2026, year_filter=2026)
    minutes = [r for r in rows if r["meeting_type"] == "minutes"]
    minutes_dates = {r["fomc_date"] for r in minutes}
    # Single-day minutes releases extract that day directly.
    assert "2026-02-18" in minutes_dates
    assert "2026-04-08" in minutes_dates
    assert "2026-05-20" in minutes_dates


# ---------------------------------------------------------------------------
# Year filtering + multi-year support
# ---------------------------------------------------------------------------


def test_year_filter_picks_only_target_year():
    rows = parse_fomc_html(_FIXTURE_TWO_YEARS, year_filter=2025)
    years = {r["fomc_date"][:4] for r in rows}
    assert years == {"2025"}, f"year_filter=2025 leaked: {years}"


def test_no_year_filter_returns_both_years():
    rows = parse_fomc_html(_FIXTURE_TWO_YEARS, year_filter=None)
    years = {r["fomc_date"][:4] for r in rows}
    assert {"2025", "2026"} <= years


# ---------------------------------------------------------------------------
# Dedup + source/url plumbing
# ---------------------------------------------------------------------------


def test_duplicate_date_in_html_dedupes_to_one_row():
    # If the fixture text mentions "March 17-18" twice (press conf + statement
    # release blurbs both reference the date), we still get one row.
    polluted = _FIXTURE_2026.replace(
        "March 17-18", "March 17-18\nPress conference on March 17-18"
    )
    rows = parse_fomc_html(polluted, year_filter=2026)
    march = [r for r in rows
             if r["fomc_date"] == "2026-03-18" and r["meeting_type"] == "scheduled"]
    assert len(march) == 1, "duplicate March 17-18 should dedupe"


def test_source_and_url_populated():
    rows = parse_fomc_html(_FIXTURE_2026, year_filter=2026)
    assert rows
    for r in rows:
        assert r["source"] == "federalreserve_gov"
        assert r["source_url"].startswith("https://www.federalreserve.gov/")


# ---------------------------------------------------------------------------
# Robustness: tolerate cosmetic HTML changes
# ---------------------------------------------------------------------------


def test_empty_html_returns_empty():
    assert parse_fomc_html("", year_filter=2026) == []


def test_html_without_year_header_returns_empty():
    # Without "YYYY FOMC Meetings" we have no anchor, so nothing matches.
    assert parse_fomc_html("Just some January 28-29 text", year_filter=2026) == []


def test_invalid_date_silently_dropped():
    # February 30 doesn't exist — _date_from_match returns None and we skip.
    body = "2026 FOMC Meetings\n\nFebruary 30\n"
    rows = parse_fomc_html(body, year_filter=2026)
    assert rows == []


# ---------------------------------------------------------------------------
# fetch() integration with the in-memory parser
# ---------------------------------------------------------------------------


def test_fetch_with_html_fixture_in_dry_run_counts_correctly():
    result = fetch(None, html=_FIXTURE_2026, year=2026, dry_run=True)
    # 8 scheduled + 3 minutes = 11 rows fetched and "upserted" in dry-run.
    assert result["fetched"] == 11
    assert result["upserted"] == 11
    assert result["skipped"] == 0
    assert result["errors"] == []
