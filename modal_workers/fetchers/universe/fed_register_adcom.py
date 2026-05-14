"""FDA Advisory Committee meeting fetcher for catalyst_universe.

Source: Federal Register API
  https://www.federalregister.gov/api/v1/documents.json
  ?conditions[type][]=NOTICE
  &conditions[agencies][]=food-and-drug-administration
  &conditions[term]=advisory committee meeting

Each notice whose abstract or title parses to a valid meeting date becomes
one catalyst_universe row at profile=binary_catalyst, catalyst_type=adcomm.
Notices without a parseable date are counted as `skipped` (not errored) —
some Federal Register notices announce technical procedure rather than a
specific meeting date.

Why this fetcher exists
-----------------------
modal_workers/sub_agents/regulatory_history.py exposes two MCP tools
(fda_adcomm_upcoming, fda_adcomm_historical) that query
`catalyst_universe WHERE catalyst_type='adcomm'`. Before this fetcher,
the table held zero `adcomm` rows and the MCP always returned empty.
This module is the missing producer.

The heavy lifting (Federal Register API call, meeting-date parsing,
committee tag detection, drug-name candidate extraction) is reused from
modal_workers.shared.fda_advisory_calendar — the same helper that
fda_pdufa_pipeline already uses to emit `adcom_scheduled` signals. This
fetcher is the catalyst_universe complement to that signal path.

Ticker resolution is deferred: Federal Register notices don't carry CIK
and drug-name → entity matching is a separate problem. Initial rows ship
with ticker=NULL and raw_payload.drug_candidates preserved for the
entity_linker pass.

Idempotency: dedupe via `upsert_catalyst_universe_row`'s ON CONFLICT
on `(source_feed, catalyst_type, ticker, catalyst_date)`. Re-running the
same window is a no-op.

Run locally:
    SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \\
    python3 -m modal_workers.fetchers.universe.fed_register_adcom \\
        --lookback-days 30 --apply
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))

from modal_workers.shared.fda_advisory_calendar import (  # noqa: E402
    Meeting,
    fetch_advisory_committee_meetings,
)
from modal_workers.shared.emissions_ledger import upsert_catalyst_universe_row  # noqa: E402
from modal_workers.shared.supabase_client import SupabaseClient, SupabaseError  # noqa: E402

logger = logging.getLogger("fed_register_adcom")

SOURCE_FEED = "federal_register_adcom"
CATALYST_TYPE = "adcomm"
PROFILE = "binary_catalyst"


def fetch(
    client: SupabaseClient,
    *,
    start_date: date,
    end_date: date,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Fetch FDA AdComm notices and upsert one row per parseable meeting date.

    Contract matches modal_workers.app._run_fetcher: takes
    (client, *, start_date, end_date) and returns
    {fetched, upserted, skipped, errors, window}.

    `start_date` is interpreted as the publication-date lower bound — i.e.
    "notices published since this date". `end_date` is informational (the
    Federal Register API doesn't accept an upper bound on the simple-term
    query we use; the helper queries with `publication_date >= today -
    lookback_days`). Future meeting dates inside notices published in the
    window are also captured.
    """
    today = date.today()
    lookback_days = max(0, (today - start_date).days)

    fetched = 0
    upserted = 0
    skipped = 0
    errors: List[Dict[str, Any]] = []

    try:
        meetings: List[Meeting] = fetch_advisory_committee_meetings(
            lookback_days=lookback_days,
            client=client if not dry_run else None,
        )
    except Exception as e:  # noqa: BLE001
        # Federal Register or cache error — fail closed at the top level
        # rather than upsert partial data.
        return {
            "fetched": 0,
            "upserted": 0,
            "skipped": 0,
            "errors": [{"phase": "fetch_advisory_committee_meetings",
                        "error": str(e)[:400]}],
            "window": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        }

    for m in meetings:
        fetched += 1
        if not m.meeting_date:
            # Notice exists but its prose didn't parse to a date. These are
            # often "procedural" notices (CV solicitation, scheduling-change
            # announcement). Counted as skipped, not errored.
            skipped += 1
            continue

        row = _map_meeting_to_row(m)
        if dry_run:
            upserted += 1
            continue
        try:
            upsert_catalyst_universe_row(
                client,
                profile=row["profile"],
                catalyst_type=row["catalyst_type"],
                catalyst_date=row["catalyst_date"],
                source_feed=row["source_feed"],
                ticker=row.get("ticker"),
                entity_id=row.get("entity_id"),
                material_outcome=row["material_outcome"],
                source_url=row.get("source_url"),
                raw_payload=row["raw_payload"],
            )
            upserted += 1
        except (SupabaseError, ValueError) as e:
            errors.append({
                "catalyst_date": row["catalyst_date"],
                "title": (m.title or "")[:80],
                "error": str(e)[:400],
            })
            skipped += 1

    return {
        "fetched": fetched,
        "upserted": upserted,
        "skipped": skipped,
        "errors": errors,
        "window": {"start": start_date.isoformat(), "end": end_date.isoformat()},
    }


def _map_meeting_to_row(m: Meeting) -> Dict[str, Any]:
    """Meeting → catalyst_universe row.

    Ticker is NULL by design — Federal Register notices don't carry CIK,
    and drug-name → entity resolution is a separate problem. The
    drug_candidates list is preserved in raw_payload for a future
    entity_linker pass.
    """
    return {
        "profile": PROFILE,
        "catalyst_type": CATALYST_TYPE,
        "catalyst_date": m.meeting_date,  # already ISO YYYY-MM-DD per helper
        "source_feed": SOURCE_FEED,
        "ticker": None,
        "entity_id": None,
        "material_outcome": "unclear",   # scheduled future event
        "source_url": m.source_url,
        "raw_payload": {
            "committee": m.committee,
            "drug_candidates": list(m.drug_candidates or []),
            "publication_date": m.publication_date,
            "title": m.title,
            "abstract": m.abstract,
        },
    }


# --------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=30,
                        help="Federal Register publication-date lookback. Default 30.")
    parser.add_argument("--apply", action="store_true",
                        help="Write to Supabase. Default dry-run.")
    args = parser.parse_args()

    end = date.today()
    start = end - timedelta(days=args.lookback_days)

    client = SupabaseClient() if args.apply else _DryClient()
    result = fetch(client, start_date=start, end_date=end,
                   dry_run=not args.apply)
    print(f"window:   {result['window']['start']} → {result['window']['end']}")
    print(f"fetched:  {result['fetched']}")
    print(f"upserted: {result['upserted']} ({'dry-run' if not args.apply else 'applied'})")
    print(f"skipped:  {result['skipped']}  (notices without parseable meeting_date)")
    if result["errors"]:
        print(f"errors:   {len(result['errors'])}")
        for err in result["errors"][:5]:
            print(f"  - {err}")
    return 0


class _DryClient:
    """Stub for --no-apply runs — supports the client interface but never calls REST."""
    def _rest(self, *a, **kw):
        return []

    def read_cache(self, *a, **kw):
        return None

    def write_cache(self, *a, **kw):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
