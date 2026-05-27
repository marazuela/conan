# Scanner layer high-impact edits — deploy runbook

Drafted 2026-05-27 alongside the punch-list implementation. Covers what
landed in code vs what still needs an operator push, and the order to push
in.

## What landed in code (this branch)

| # | Edit | File |
|---|------|------|
| 1 | Expanded garbage `drug_name` regex in v3 bridge — catches `EX-99`, `Exhibit`, `(auto-discovered)`, `unknown/n-a/tbd`, pure-punctuation residue. | `supabase/migrations/20260530000000_v3_bridge_signal_to_fda_assets.sql` |
| 3 | `pdufa_watchlist.json` sanitizer on read — drops entries missing pdufa_date, status in {approved, resolved_crl, killed, excluded, non_tradeable}, garbage drug_name (`(auto-discovered)`, EX-99…), or missing ticker. Logs dropped counts. | `modal_workers/scanners/fda_pdufa_pipeline.py` |
| 5 | `partial_query_failures` counter on `edgar_8k_pdufa` — distinguishes "EDGAR returned nothing" from "2 of 3 queries silently dropped after retries". Adds `partial_query_failures`, `queries_total`, `queries_failed` to the result envelope; WARN log when non-zero. | `modal_workers/fetchers/universe/edgar_8k_pdufa.py` |
| 9 | `scanner_base` zero-signal partial flip — when `fetched_records > 0` and `signals_emitted == 0` with no declared error, flip `status` to `partial` and append a `zero_signal_with_fetched_records` entry to errors. Catches today's `fda_pdufa_pipeline` + `edgar_8k_pdufa` silent failures (last_run_signals=0 yet status=ok). | `modal_workers/shared/scanner_base.py` |
| 10 | Bridge writes `extracted_facts.pdufa_date` — when a bridged signal carries `pdufa_date` in raw_payload, the bridge function also INSERTs an `extracted_facts` row, which fires the existing `fda_assets_next_catalyst_from_extracted_facts` trigger and refreshes `next_catalyst_date`. | `supabase/migrations/20260530000000_v3_bridge_signal_to_fda_assets.sql` |

Already-done (no action needed):

| # | Why no-op |
|---|-----------|
| 4 | `fda_adcomm_pdufa.fetch()` already passes resolved `entity_id` to `upsert_catalyst_universe_row()` (line 101-113). |
| 6 | Migration `20260511102914 add_openfda_corpus_ingest_scanner` is applied; scanner row exists, last_run 2026-05-27. |
| 7 | `edgar_filing_monitor.status='deprecated'`, last_run_utc=2026-05-11 — not still writing. Memory note stale. |
| 8 | Migration `20260530` already creates an `operator_flag` (source `bridge_signal_to_v3`, kind `v3_bridge_no_asset_match`) for asset-less signals — orphans surface in the open-flag dashboard. |

## What still needs to be pushed (operator action)

Three migrations are committed to this branch but **NOT** applied to prod
(verified via `mcp__supabase__list_migrations` on 2026-05-27):

1. `20260521120000_fda_assets_next_catalyst_writer.sql` — installs
   `fda_assets_recompute_next_catalyst_date(uuid)`, the two upstream
   triggers (extracted_facts + catalyst_universe), and the
   `fda_assets_backfill_next_catalyst_date()` helper.
2. `20260527010000_catalyst_universe_adcomm_enum.sql` — extends the
   catalyst_type CHECK constraint to allow `adcomm`. Required because the
   `fda_assets_next_catalyst_from_catalyst_universe` trigger gate lists
   `adcomm` as one of the FDA-typed catalyst categories.
3. `20260530000000_v3_bridge_signal_to_fda_assets.sql` — installs the new
   bridge function (with #1 garbage regex + #10 extracted_facts writeback)
   and the AFTER INSERT trigger on `signals`.

**Push order matters.** The 20260530 bridge writes `extracted_facts` rows
that depend on the triggers from 20260521120000 to refresh
`next_catalyst_date`. The 20260527010000 enum extension must land before
either of the other two writes its first `adcomm` row.

```bash
# From the repo root, on this branch:
supabase db push --project-ref xvwvwbnxdsjpnealarkh
```

`supabase db push` applies migrations in filename order, so the three
land in the correct sequence automatically.

## Post-push backfill (item #2)

Once `20260521120000_fda_assets_next_catalyst_writer.sql` is applied,
run the backfill once to populate `next_catalyst_date` on the ~62 active
`fda_assets` rows:

```sql
SELECT * FROM public.fda_assets_backfill_next_catalyst_date();
```

Returns `(rows_seen, rows_updated)`. Idempotent — safe to re-run.

Then, optionally, replay the 30d orphan signals through the new bridge so
the existing binary_catalyst backlog gets asset-linked:

```sql
SELECT * FROM public.bridge_signal_to_v3_backfill(
  p_since => now() - interval '30 days',
  p_limit => 1000
);
```

Returns `(rows_seen, rows_linked, rows_flagged)`. Idempotent (the
underlying function uses unique-key guards on documents, asset_documents,
and operator_flags).

## Code redeploy (Modal)

The Python-side edits (#3, #5, #9) ship via Modal redeploy. They are
backward-compatible — older scanner_runs rows are not rewritten — so they
take effect on the next run of each scanner without a migration.

```bash
modal deploy modal_workers/app.py
```

Watch the next `fda_pdufa_pipeline` + `edgar_8k_pdufa` runs. With #9 in
place, runs that emit zero signals with non-zero fetched_records will now
surface as `status='partial'` in `scanner_runs`, and the scanner_liveness
watchdog will start flagging them.

## Verification queries

After push + redeploy, expect:

```sql
-- #2: next_catalyst_date populated on >1/62 active assets
SELECT count(*) FILTER (WHERE next_catalyst_date IS NOT NULL) AS with_date,
       count(*) AS total_active
FROM public.fda_assets WHERE is_active = true;

-- #9: silent failures now surface as partial
SELECT name, last_run_status, last_run_signals
FROM public.scanners
WHERE name IN ('fda_pdufa_pipeline', 'edgar_8k_pdufa')
ORDER BY name;

-- #1, #10 combined: bridged signals create extracted_facts.pdufa_date rows
SELECT count(*) FROM public.extracted_facts
WHERE extraction_model = 'bridge_signal_to_v3' AND fact_type = 'pdufa_date';

-- #8: orphan signals surface as open operator_flags
SELECT count(*) FROM public.operator_flags
WHERE source = 'bridge_signal_to_v3' AND resolved_at IS NULL;
```
