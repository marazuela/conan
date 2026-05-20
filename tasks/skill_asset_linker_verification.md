# Skill Asset Linker Verification

Run after applying `20260601000000_skill_asset_linker_cutover.sql` and
deploying `modal_workers/orchestrator_app.py`. The migration covers both the
original skill cutover (Modal LLM linker disabled) AND the deterministic
edge-prefilter layer added on top (`doc_asset_candidates` +
`fda_asset_aliases` + tsvector sweeper).

See [skill_asset_linker_edge_prefilter_plan.md](skill_asset_linker_edge_prefilter_plan.md)
for the full design.

## Cron And Dispatch
- `cron.job` has no rows named `v3-asset-linker-pass1`,
  `v3-asset-linker-pass2`, or `v3-fact-extractor` — the cutover unscheduled
  all three LLM-ingestion crons.
- `cron.job` has active rows named `v3-doc-asset-prefilter` (every 2 min)
  and `v3-asset-alias-weekly-refresh` (Mon 03:00 UTC). These are the only
  two crons the watchdog protects post-cutover.
- `SELECT public.v3_ingestion_scheduler_watchdog();` returns
  `asset_linker_mode = 'cursor_skill_edge_queue'` and `protected_jobs`
  containing exactly the two zero-LLM-cost protected crons (no
  fact-extractor, no asset-linker).
- `compute_v3` rejects `asset_linker_run`, `asset_linker_pass2_run`, AND
  `fact_extractor_run` as unknown actions.
- `compute_v3` accepts `seed_fda_asset_aliases_refresh`.
- `modal.Function.from_name("conan-v3-orchestrator", "fact_extractor_run")
  (...)` returns the disabled stub
  `{"return_code": 0, "disabled": True, "reason": "..."}` rather than
  spending Anthropic budget.

## Deterministic Edge Prefilter

### Tables and columns
- `public.documents.raw_text_tsv` exists as a GENERATED STORED `tsvector`
  column. `\d+ public.documents` shows it with `simple` config.
- `public.documents_raw_text_tsv_gin_idx` GIN index exists.
- `public.fda_asset_aliases` table exists. CHECK constraints reject
  `alias_kind='ticker'` (tickers live on `fda_assets.ticker`), reject
  blocklisted normalized aliases (`peptide`, `concept`, `default`, etc.),
  and require NCT IDs match `^nct[0-9]{8}$`.
- `public.doc_asset_candidates` table exists with a UNIQUE constraint on
  `(document_id, asset_id, alias_set_hash)` and a partial index on
  `WHERE analyzed_at IS NULL`.
- `public.doc_asset_prefilter_runs` table exists.

### Hash function
- `SELECT public.asset_linker_alias_set_hash();` returns a 32-char md5
  string. The old function `asset_linker_skill_asset_set_hash` is gone.
- Inserting a row into `fda_asset_aliases` changes the hash. Deleting it
  changes the hash back.

### Sweeper
- `SELECT public.fn_generate_doc_asset_candidates(50);` returns a jsonb
  like `{"docs_scanned": N, "edges_emitted": M, "alias_set_hash": "..."}`
  with N up to 50.
- `doc_asset_prefilter_runs` row count increases by N after the call.
- Re-running on the same docs (same alias_set_hash) is a no-op:
  `docs_scanned = 0` because the anti-join skips already-scanned docs.

### Backfill
- Initial seed: kick `fn_generate_doc_asset_candidates(50000)` once over
  the 3142-doc backlog. Wall time should be <60s; CPU on Supabase side
  near-zero. The sweeper is set-based SQL with GIN-indexed tsvector
  matching — no PL/pgSQL row loop.

## Alias Seed Pass

### Initial seed
- `python -m modal_workers.scripts.seed_fda_asset_aliases --dry-run --asset-id <ONE>`
  prints a small batch of proposed candidates per source (curated_map,
  openfda_label, clinicaltrials_v2, extensions_mining), zero writes.
- `python -m modal_workers.scripts.seed_fda_asset_aliases` (write mode)
  produces ~250–800 rows from the 81 active assets. Verify via
  `SELECT count(*) FROM public.fda_asset_aliases`.
- One `asset_linker_runs` row appears with `pass='seed'`,
  `model='seed-script'`, `input_tokens=0`, `output_tokens=0`,
  `cost_usd=0`.

### Operator review
- `SELECT * FROM public.v_recent_auto_aliases LIMIT 50;` returns the
  newly inserted auto-aliases for spot-checking.
- Sanity bound: `SELECT count(*) FROM public.fda_asset_aliases
  WHERE created_at > now() - interval '24 hours'` should be reasonable
  (<200 unless seed was just run).
- Operator-flag should be raised if daily-aliases-added exceeds the
  threshold in `internal_config`.

## Skill Output

### Queue is edge-shaped
- `\d public.v_asset_linker_skill_queue` shows columns
  `candidate_id, document_id, asset_id, matched_aliases, match_strength,
   alias_set_hash, ...` — i.e. ONE row per `(doc, asset)` edge, NOT one
  row per doc.
- `SELECT count(*) FROM public.v_asset_linker_skill_queue` returns a
  bounded high-signal batch (in the thousands at most for the 3142-doc
  backlog post-seed).
- Rows are ordered by `match_strength DESC` so the skill processes
  multi-alias-kind matches first.

### Skill writes
- A small dry run of the local `asset-linker` skill produces decisions
  without writes.
- A small write run inserts:
  - `asset_documents` rows for `status='linked'` decisions only.
  - `document_asset_linker_attempts` rows for every processed edge,
    carrying `document_id`, **`asset_id`**, `alias_set_hash`, `status`,
    and `link_inserted` boolean.
  - One `asset_linker_runs` row with `pass='skill'`,
    `model='cursor-agent-skill'`, token + cost = 0.
  - `doc_asset_candidates.analyzed_at` and `analysis_run_id` stamped on
    the consumed edges.
- Re-querying `v_asset_linker_skill_queue` does not return edges with
  terminal attempts for the current `alias_set_hash`.

### Missed-alias path
- If the skill names an asset the prefilter didn't surface, a
  `public.operator_flags` row appears with
  `source='asset_linker_skill_missed_alias'`. The skill does NOT
  directly insert into `fda_asset_aliases`.

## Cost

- `public.v_cost_24h_by_worker` shows no production Anthropic token burn
  from API-key asset-linker runs after the cutover timestamp.
- Any `asset_linker_runs.pass IN ('skill', 'seed')` rows have token and
  cost fields set to `0`.
- `asset_linker_runs.pass = 'seed'` rows reflect the public-API seed pass
  cost (zero LLM, only HTTP API calls bounded at ~160/week for the
  weekly refresh).

## Concurrency / Race Conditions

- Pre-seed cron firing (before `seed_fda_asset_aliases` has run) emits
  ticker-only candidates with `match_strength=1`. This is harmless —
  after seed runs, `alias_set_hash` changes and the sweeper re-emits
  full-signal candidates; the ticker-only candidates linger under the
  stale hash and are filtered out of `v_asset_linker_skill_queue`.
- The skill is the only consumer of the queue. If skill hasn't been
  updated to the edge-shaped contract before the migration applies,
  the cron continues to emit edges that simply accumulate in
  `doc_asset_candidates` until the skill picks them up.
