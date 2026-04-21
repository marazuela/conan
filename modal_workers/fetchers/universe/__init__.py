"""Universe fetchers — populate catalyst_universe from independent data sources.

Each fetcher exports a `fetch(client, *, start_date, end_date, dry_run=False) -> dict`
callable. Return shape:
    {
      "fetched": int,   # raw rows pulled from source
      "upserted": int,  # rows written/updated in catalyst_universe
      "skipped": int,   # rows dropped (dedup, schema drift, or resolver miss)
      "errors": list[dict],
    }

All fetchers are idempotent: re-running the same window is a no-op update.
"""
