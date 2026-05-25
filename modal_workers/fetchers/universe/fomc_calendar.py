"""FOMC calendar fetcher.

Phase 3a — feeds public.fomc_calendar from federalreserve.gov. The Q1
confounder audit reads this table to flag FDA events landing on or ±1 day
from a scheduled FOMC announcement (the overnight reaction contaminates
same-day post-event returns).

Source: federalreserve.gov/monetarypolicy/fomccalendars.htm
  - Stable HTML structure since 2018+. Date strings inside <div class="panel">
    siblings, one panel per year.
  - 8 scheduled meetings per year. Minutes ~3 weeks later.
  - Emergency meetings (rare) get a separate panel.

Run locally:
    SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \\
    python3 -m modal_workers.fetchers.universe.fomc_calendar --year 2026 --apply

Backfill 2018-present:
    for y in 2018 2019 2020 2021 2022 2023 2024 2025 2026; do
      python3 -m modal_workers.fetchers.universe.fomc_calendar --year $y --apply
    done
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))

from modal_workers.shared.supabase_client import SupabaseClient, SupabaseError  # noqa: E402

logger = logging.getLogger(__name__)

FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
SOURCE = "federalreserve_gov"
REQUEST_TIMEOUT_S = 20

# FOMC two-day meetings render as "January 30-31" in the HTML; one-day
# meetings as "January 30". We extract the END day (announcement day) since
# that's the market-moving date.
_MEETING_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(\d{1,2})(?:-(\d{1,2}))?",
    re.IGNORECASE,
)

_MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def fetch(
    client: Optional[SupabaseClient],
    *,
    year: Optional[int] = None,
    html: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Fetch FOMC dates for the given year (or all years present in the HTML)
    and upsert into fomc_calendar.

    `html` lets callers inject a fixture for tests without hitting the network.
    """
    if html is None:
        try:
            r = requests.get(FOMC_URL, timeout=REQUEST_TIMEOUT_S,
                             headers={"User-Agent": "conan-fomc-fetcher/1.0"})
            r.raise_for_status()
            html = r.text
        except requests.RequestException as e:
            return {
                "fetched": 0, "upserted": 0, "skipped": 0,
                "errors": [{"error": f"http: {e!s}"[:400]}],
            }

    parsed = parse_fomc_html(html, year_filter=year)

    upserted = 0
    skipped = 0
    errors: List[Dict[str, Any]] = []
    for row in parsed:
        if dry_run or client is None:
            upserted += 1
            continue
        try:
            _upsert_fomc_row(client, row)
            upserted += 1
        except SupabaseError as e:
            errors.append({
                "fomc_date": row["fomc_date"], "error": str(e)[:400],
            })
            skipped += 1

    return {
        "fetched": len(parsed),
        "upserted": upserted,
        "skipped": skipped,
        "errors": errors,
        "year_filter": year,
    }


# ---------------------------------------------------------------------------
# HTML parser — kept pure so tests can hand in a fixture string.
# ---------------------------------------------------------------------------


def parse_fomc_html(html: str, *, year_filter: Optional[int] = None) -> List[Dict[str, Any]]:
    """Scan the calendar HTML and return rows ready for upsert.

    Strategy: find every <div class="panel panel-default"> block (one per
    year), pull the year from the header, then extract month + day(s) from
    the inner <div class="row fomc-meeting"> rows. Minutes appear as siblings
    labeled "Minutes" — captured separately as meeting_type='minutes'.

    The parser is intentionally conservative: anything that doesn't match the
    expected regex is silently skipped. The Fed has rearranged the HTML
    structure 3+ times since 2017; treating mismatches as soft failures
    keeps the daily refresh from breaking on cosmetic changes.
    """
    rows: List[Dict[str, Any]] = []

    # Split on year-panel headers. Each panel typically begins with
    # "<h4 class="panel-heading">YYYY FOMC Meetings</h4>" or similar — we
    # match liberally to survive style tweaks.
    panel_re = re.compile(
        r"(\d{4})\s+FOMC\s+Meetings", re.IGNORECASE,
    )
    panels = panel_re.split(html)
    # split() returns [pre, year1, body1, year2, body2, ...].
    for i in range(1, len(panels), 2):
        try:
            year = int(panels[i])
        except ValueError:
            continue
        body = panels[i + 1] if i + 1 < len(panels) else ""
        if year_filter is not None and year != year_filter:
            continue
        rows.extend(_extract_meetings_from_panel(year, body))

    return rows


def _extract_meetings_from_panel(year: int, body: str) -> List[Dict[str, Any]]:
    """Pull meeting + minutes rows out of one year's panel body."""
    out: List[Dict[str, Any]] = []
    # Each meeting block tends to live inside a row tagged with the month.
    # We don't rely on the DOM — just walk the text for month+day matches and
    # assume the first match per "row" is the meeting itself, subsequent
    # matches inside a "Minutes" context are minutes-release dates.
    #
    # The panel separator is heuristic; we split on "Minutes" and look at
    # whether the match preceded or followed.
    block = body
    minutes_marker = re.search(r"Minutes", block, re.IGNORECASE)

    # Pass 1: meeting dates (everything before "Minutes" header or anywhere
    # if no minutes marker exists).
    meeting_scope = block[:minutes_marker.start()] if minutes_marker else block
    for m in _MEETING_RE.finditer(meeting_scope):
        d = _date_from_match(year, m, prefer_end_day=True)
        if d is None:
            continue
        out.append({
            "fomc_date": d.isoformat(),
            "meeting_type": "scheduled",
            "source": SOURCE,
            "source_url": FOMC_URL,
        })

    # Pass 2: minutes-release dates (after the "Minutes" header).
    if minutes_marker is not None:
        minutes_scope = block[minutes_marker.start():]
        for m in _MEETING_RE.finditer(minutes_scope):
            d = _date_from_match(year, m, prefer_end_day=False)
            if d is None:
                continue
            out.append({
                "fomc_date": d.isoformat(),
                "meeting_type": "minutes",
                "source": SOURCE,
                "source_url": FOMC_URL,
            })

    # Dedupe — the regex may grab the same meeting twice if it appears in
    # both "press conference" and "statement release" contexts.
    seen: set[tuple[str, str]] = set()
    dedup: List[Dict[str, Any]] = []
    for row in out:
        key = (row["fomc_date"], row["meeting_type"])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(row)
    return dedup


def _date_from_match(year: int, m: re.Match, *, prefer_end_day: bool) -> Optional[date]:
    month = _MONTH_MAP.get(m.group(1).lower())
    if month is None:
        return None
    start_day = int(m.group(2))
    end_day = int(m.group(3)) if m.group(3) else start_day
    day = end_day if prefer_end_day else start_day
    try:
        return date(year, month, day)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Supabase upsert.
# ---------------------------------------------------------------------------


def _upsert_fomc_row(client: SupabaseClient, row: Dict[str, Any]) -> None:
    client.from_("fomc_calendar").upsert(
        {
            "fomc_date": row["fomc_date"],
            "meeting_type": row.get("meeting_type", "scheduled"),
            "source": row["source"],
            "source_url": row.get("source_url"),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="fomc_date,meeting_type",
    ).execute()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--year", type=int, default=None)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    client = SupabaseClient() if args.apply else None
    result = fetch(client, year=args.year, dry_run=not args.apply)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
