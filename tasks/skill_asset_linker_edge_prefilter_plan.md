# Skill Asset Linker — Deterministic Edge Prefilter Plan

Modifies the in-flight `20260601000000_skill_asset_linker_cutover.sql` migration
to replace LLM-based prefilter with a deterministic SQL keyword/alias matcher,
emit persisted `(doc, asset)` candidate edges, and have the local Cursor
asset-linker skill consume edges (not docs).

Supersedes the "high-signal queue" restriction in the staged cutover — keyword
matching is cheap enough to run against all docs, edges are the natural rate
limiter.

## Goals

- Stop the asset_linker Anthropic burn permanently (skill cutover already
  does this; we keep that).
- Eliminate LLM-spend on prefilter — only run the skill on pre-matched
  candidate edges.
- Expand recall to all doc types, not just clinicaltrials/edgar/openfda/
  federal_register.
- Persist candidate edges so we can answer "which docs surfaced asset X" and
  "which assets did we skip on doc Y" indefinitely.

## Non-goals

- Replacing the analysis step with deterministic logic. Skill still does the
  reasoning per `(doc, asset)` edge.
- Auto-deploy on merge. Same manual deploy posture as PR #101 / staged cutover.
- Pass-2 verification rework. Pass-2 stays on the skill path; out of scope here.

## Design

### Alias source — two layers

**Layer 1 — derived from `fda_assets`** (existing `v_asset_linker_skill_assets`
view, reused). Pulls ticker, drug_name, generic_name, sponsor_name, indication.
Already excludes the `peptide / concept / ex-99 / (auto-discovered) / default`
junk drug names.

**Layer 2 — supplement table `fda_asset_aliases`** (new).

```sql
CREATE TABLE public.fda_asset_aliases (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  asset_id        uuid NOT NULL REFERENCES public.fda_assets(id) ON DELETE CASCADE,
  alias           text NOT NULL,
  alias_normalized text NOT NULL,            -- lower(trim(alias)); CHECK length>=3
  alias_kind      text NOT NULL CHECK (alias_kind IN (
    'brand', 'generic', 'code', 'nct_id', 'abbreviation',
    'sponsor_alias', 'sponsor_stem', 'drug_name'
  )),
  -- NB: 'ticker' is intentionally NOT a valid alias_kind. Tickers live on
  -- fda_assets.ticker and require case-sensitive matching distinct from the
  -- tsvector-based name-matching path used for every other kind.
  source          text NOT NULL CHECK (source IN (
    'curated_map', 'openfda_label', 'clinicaltrials_v2',
    'extensions_mining', 'operator', 'synthetic'
  )),
  source_ref      text,                       -- e.g. openFDA setid, NCT ID
  active          boolean NOT NULL DEFAULT true,
  inactive_reason text,                       -- when operator deactivates
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (asset_id, alias_normalized, alias_kind)
);
CREATE INDEX fda_asset_aliases_lookup_idx
  ON public.fda_asset_aliases (alias_normalized) WHERE active = true;
CREATE INDEX fda_asset_aliases_asset_idx
  ON public.fda_asset_aliases (asset_id);
```

Seeded by `modal_workers/scripts/seed_fda_asset_aliases.py` — see **Seed pass**
below. Pre-migration-deploy run is mandatory.

**Materialized alias lookup view** `v_asset_alias_lookup` unions Layer 1 + 2
into `(asset_id, alias_lower, alias_kind, alias_weight)` so the matcher reads
from one place.

### Hash semantics

Single global hash `alias_set_hash` derived from the full active alias inventory
(plus ticker set from `fda_assets`):

```sql
CREATE OR REPLACE FUNCTION public.asset_linker_alias_set_hash()
RETURNS text LANGUAGE sql STABLE SET search_path = public AS $$
  SELECT md5(
    coalesce(
      (SELECT string_agg(
        a.asset_id::text || '|' || a.alias_normalized || '|' || a.alias_kind,
        ',' ORDER BY a.asset_id, a.alias_normalized, a.alias_kind
      ) FROM public.fda_asset_aliases a WHERE a.active = true),
      ''
    ) || '#' ||
    coalesce(
      (SELECT string_agg(fa.id::text || '|' || fa.ticker, ','
                         ORDER BY fa.id)
       FROM public.v_asset_linker_skill_assets fa),
      ''
    )
  );
$$;
```

Replaces `asset_linker_skill_asset_set_hash()` (delete the old function in the
same migration).

Any alias add/remove/deactivate OR asset add/remove invalidates the hash. On
hash change, sweeper rescans the full doc corpus. Cost: ~3000 docs × tsvector
@@ alias_tsquery with GIN index ≈ <60s end-to-end. Expected change frequency:
weekly refresh cron + ad-hoc operator → 2–3 events/week. Acceptable churn.

Granular per-asset hash (only rematch (doc, asset) pairs whose asset's aliases
changed) is a deferred refinement — worth doing when assets cross ~500 or docs
cross ~30k. Out of scope for v1.

### Edge table

```sql
CREATE TABLE public.doc_asset_candidates (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id     uuid NOT NULL REFERENCES public.documents(id) ON DELETE CASCADE,
  asset_id        uuid NOT NULL REFERENCES public.fda_assets(id) ON DELETE CASCADE,
  matched_aliases jsonb NOT NULL,           -- [{alias, kind, hit_count}]
  match_strength  smallint NOT NULL,        -- count of distinct kinds matched
  alias_set_hash  text NOT NULL,            -- snapshot for cache invalidation
  matched_at      timestamptz NOT NULL DEFAULT now(),
  analyzed_at     timestamptz,              -- set when skill consumes the edge
  analysis_run_id uuid,                     -- FK to asset_linker_runs(id)
  UNIQUE (document_id, asset_id, alias_set_hash)
);
CREATE INDEX doc_asset_candidates_unprocessed_idx
  ON public.doc_asset_candidates (matched_at)
  WHERE analyzed_at IS NULL;
CREATE INDEX doc_asset_candidates_document_idx
  ON public.doc_asset_candidates (document_id);
CREATE INDEX doc_asset_candidates_asset_idx
  ON public.doc_asset_candidates (asset_id);
```

Notes:
- `alias_set_hash` snapshotted at match time means alias additions don't
  invalidate prior edges — we just re-match the doc against the new hash.
- `match_strength` lets the skill prioritize multi-alias-kind hits (strong
  signal) over single-alias hits (weak signal). Skill can also choose to
  auto-reject `match_strength=1, kind='sponsor_alias'` edges without spending
  an LLM call.

### Prefilter mechanism

**Choice: background sweeper, not insert trigger.** Insert trigger would add
latency to every document insert and couple ingest to alias-table state.
Sweeper isolates the two.

**Three match paths in `fn_generate_doc_asset_candidates`, picked per
`alias_kind`.** Single-algorithm matching trades precision in the wrong places
(case folding breaks tickers, stemming/fuzz breaks NCT IDs). Per-kind matching
keeps each clause appropriate to the kind's identifier shape.

- **Exact + word-boundary** (`alias_kind IN ('nct_id', 'code')`): case-insensitive
  `raw_text ~* ('\m' || alias_normalized || '\M')`. NCT IDs and code names
  must not fuzz — wrong NCT = wrong asset.
- **Case-sensitive word-boundary regex** for **tickers**: fed from
  `fda_assets.ticker` directly, **not** from `fda_asset_aliases`. `raw_text ~
  ('\m' || ticker || '\M')` (`~`, not `~*`). Tickers don't go in the alias
  table at all — case-sensitivity requirement is unique to tickers and lower-cased
  "IONS" lexeme would collide with "ions" prose all over EDGAR documents.
- **tsvector full-text** (everything else: `drug_name`, `generic`, `brand`,
  `sponsor_alias`, `sponsor_stem`, `abbreviation`): use `simple` config (no
  English stemming, no stop-word stripping — multi-word sponsor names like
  "Eli Lilly and Company" need every token preserved).
  - Materialized doc tsvector column on `documents` (`raw_text_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('simple', coalesce(raw_text, '') || ' '
    || coalesce(title, ''))) STORED`) + GIN index.
  - Per-alias `tsquery` precompiled column on `fda_asset_aliases`
    (`alias_tsquery tsquery GENERATED ALWAYS AS (
       phraseto_tsquery('simple', alias_normalized)
    ) STORED`).
  - Match clause: `d.raw_text_tsv @@ a.alias_tsquery`.

Drop `ticker` from the `fda_asset_aliases.alias_kind` CHECK constraint — never
ingested there.

`fn_generate_doc_asset_candidates(p_limit int)` PL/pgSQL flow:

1. Select up to `p_limit` documents NOT IN `doc_asset_prefilter_runs` for the
   current `alias_set_hash`.
2. For each doc, run all three match paths, UNION the hits.
3. Aggregate hits per `(document_id, asset_id)`: collect `matched_aliases`
   jsonb, count distinct `alias_kind` values → `match_strength`.
4. Insert into `doc_asset_candidates` (UPSERT on
   `(document_id, asset_id, alias_set_hash)`).
5. Always insert into `doc_asset_prefilter_runs`, including zero-candidate
   docs, to prevent rescanning under the same hash.

```sql
CREATE TABLE public.doc_asset_prefilter_runs (
  document_id    uuid NOT NULL REFERENCES public.documents(id) ON DELETE CASCADE,
  alias_set_hash text NOT NULL,
  candidate_count integer NOT NULL DEFAULT 0,
  scanned_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (document_id, alias_set_hash)
);
CREATE INDEX doc_asset_prefilter_runs_hash_idx
  ON public.doc_asset_prefilter_runs (alias_set_hash, scanned_at DESC);
```

The sweeper's anti-join in step 1 reads against this table (not
`doc_asset_candidates`), so docs with zero candidates aren't rescanned under
the same hash.

Scheduled via `pg_cron`:
```
SELECT cron.schedule(
  'v3-doc-asset-prefilter',
  '*/2 * * * *',
  $cron$ SELECT public.fn_generate_doc_asset_candidates(2000); $cron$
);
```

Watchdog `v3_ingestion_scheduler_watchdog()` extends its `v_expected` from
`['v3-fact-extractor']` to `['v3-fact-extractor', 'v3-doc-asset-prefilter']`.

### Skill queue — edges, not docs

Replace `v_asset_linker_skill_queue` with an edge-shaped view:

```sql
CREATE VIEW public.v_asset_linker_skill_queue AS
SELECT
  e.id           AS candidate_id,
  e.document_id,
  e.asset_id,
  e.matched_aliases,
  e.match_strength,
  e.alias_set_hash,
  d.source,
  d.doc_type,
  d.title,
  d.url,
  d.published_at,
  d.raw_text_tokens,
  d.storage_path,
  d.extensions,
  a.ticker,
  a.drug_name,
  a.sponsor_name
FROM public.doc_asset_candidates e
JOIN public.documents d ON d.id = e.document_id
JOIN public.v_asset_linker_skill_assets a ON a.id = e.asset_id
WHERE e.analyzed_at IS NULL
  AND e.alias_set_hash = public.asset_linker_alias_set_hash()
ORDER BY
  e.match_strength DESC,
  d.published_at DESC NULLS LAST,
  e.matched_at;
```

The skill processes edges in priority order, calls per-edge analysis (one
LLM call per `(doc, asset)` pair max), writes:
- `asset_documents` insert if linked,
- `document_asset_linker_attempts` (existing table — keep) with outcome,
- updates `doc_asset_candidates.analyzed_at` and `analysis_run_id`.

### Migration to staged cutover

Edit `supabase/migrations/20260601000000_skill_asset_linker_cutover.sql`
in place (still unmerged on this branch). Append in this order:

1. `ALTER TABLE public.documents ADD COLUMN raw_text_tsv tsvector GENERATED
   ALWAYS AS (to_tsvector('simple', coalesce(raw_text, '') || ' ' ||
   coalesce(title, ''))) STORED;`
2. `CREATE INDEX documents_raw_text_tsv_gin ON public.documents USING GIN (raw_text_tsv);`
3. `fda_asset_aliases` table (with `alias_tsquery tsquery GENERATED ALWAYS AS
   (phraseto_tsquery('simple', alias_normalized)) STORED`) + indexes
4. `doc_asset_candidates` table + indexes
5. `doc_asset_prefilter_runs` table + indexes
6. `asset_linker_alias_set_hash()` function (replaces
   `asset_linker_skill_asset_set_hash`)
7. `DROP FUNCTION public.asset_linker_skill_asset_set_hash();`
8. `fn_generate_doc_asset_candidates(int)` function — three match paths,
   UPSERT into candidates + insert into prefilter_runs
9. `cron.schedule('v3-doc-asset-prefilter', '*/2 * * * *', ...)`
10. `cron.schedule('v3-asset-alias-weekly-refresh', '0 3 * * 1', ...)` — Mondays 03:00 UTC, calls a stub that the seed script binds to via Modal
11. Update `v3_ingestion_scheduler_watchdog()` `v_expected` to include both
    `v3-doc-asset-prefilter` and `v3-asset-alias-weekly-refresh`
12. Replace `v_asset_linker_skill_queue` body with edge-shaped query
13. `v_recent_auto_aliases` view
14. Extend `asset_linker_runs_pass_check` to allow `'seed'` in addition to
    `'pass1' | 'pass2' | 'cowork_backfill' | 'skill'`

Keep unchanged: `document_asset_linker_attempts`, `v_asset_linker_skill_assets`.

No new migration file — one atomic cutover.

Backfill caveat: the generated `raw_text_tsv` column on a non-empty
`documents` table requires a one-time table rewrite (`ALTER TABLE ADD COLUMN
... GENERATED ... STORED`). On ~3000 rows this is seconds; on larger
deployments, plan a maintenance window. Acceptable here.

## Seed pass — `fda_asset_aliases` initial population

Empty start is unacceptable — keyword matching collapses to ticker / drug_name /
sponsor_name only, which is what asset_linker was effectively doing in 2026-05.
Real recall requires brand names, generic↔brand crosswalks, NCT IDs, code names
(LY3502970-style), and sponsor variants (parent/subsidiary).

Seed source map — what we have, where it lives, how we mine it:

| Source | Yields | Mechanism | Cost |
|---|---|---|---|
| `modal_workers/shared/sponsor_resolver.py` `CURATED_MAP` | sponsor_alias, sponsor_stem | Reverse-index by ticker → emit every CURATED_MAP key with same ticker as alias; then strip `Inc\|Inc\.\|LLC\|Pharmaceuticals\|Therapeutics\|Biosciences\|Pharma` suffixes to get sponsor_stem variants (e.g. `Eli Lilly and Company` → `Eli Lilly`, `Lilly`). | Offline, instant |
| openFDA `/drug/label?search=openfda.generic_name:"X"` | brand, generic | For each asset's generic_name, fetch labels → emit every distinct `openfda.brand_name` as `alias_kind='brand'`, every distinct `openfda.generic_name` as `alias_kind='generic'`. Source_ref = setid. | ~81 API calls (one per active asset), free tier OK |
| ClinicalTrials.gov v2 `/studies?query.term=<drug_name>&query.lead=<sponsor>` | nct_id, code | For each asset, search by drug_name + sponsor; pull NCT IDs of returned trials AND `protocolSection.armsInterventionsModule.interventions[].otherNames[]` (this is where code names like LY3502970 live). | ~81 API calls, free tier OK |
| `documents.extensions` mining via SQL | nct_id, code | `SELECT extensions->>'nct_id', extensions->>'intervention_other_names' FROM documents JOIN asset_documents` — surface NCT/code values from already-linked corpus. | SQL only |
| Operator | any | Manual `INSERT ... ON CONFLICT DO NOTHING` for known gaps. | Free-form |

Synthetic abbreviations (truncated drug stems, drug+condition combos) are
**out of scope for v1**: false-positive rate is too high without curation.
`source='synthetic'` slot is reserved for a later pass.

### Script: `modal_workers/scripts/seed_fda_asset_aliases.py`

```
python -m modal_workers.scripts.seed_fda_asset_aliases \
  [--asset-id UUID]      # restrict to one asset (smoke test)
  [--sources curated_map,openfda_label,clinicaltrials_v2,extensions_mining]
                         # default: all
  [--dry-run]            # log proposed inserts, don't write
```

Idempotent (`ON CONFLICT (asset_id, alias_normalized, alias_kind) DO NOTHING`).
Emits one `asset_linker_runs` row per seeding session with token cost = 0 and
`pass = 'seed'` (extend the pass CHECK constraint accordingly).

### Maintenance — keep aliases fresh

Two follow-on mechanisms, both cheap:

1. **Trigger on `fda_assets` INSERT**: enqueue a Modal job (or pg_cron-polled
   queue) that runs `seed_fda_asset_aliases --asset-id <new>` so a freshly
   added asset gets its brand/NCT/code aliases within an hour, not on the
   next manual cron.
2. **Weekly refresh** `pg_cron` job: re-runs openFDA + ClinicalTrials sources
   for all active assets. New brand approvals + new trials get picked up.
   Run cost ≤ ~160 free-tier API calls/week.

Add both to the cutover migration alongside the prefilter cron.

### Operator review surface

Aliases pulled from openFDA/ClinicalTrials are auto-active but can be wrong
(e.g. shared brand stems across unrelated drugs). Add view:

```sql
CREATE VIEW public.v_recent_auto_aliases AS
SELECT a.*, fa.ticker, fa.drug_name
FROM public.fda_asset_aliases a
JOIN public.fda_assets fa ON fa.id = a.asset_id
WHERE a.source IN ('openfda_label','clinicaltrials_v2','extensions_mining')
  AND a.created_at > now() - interval '14 days'
  AND a.active = true
ORDER BY a.created_at DESC;
```

Operator flag opens when daily-aliases-added > N (e.g. 200) — sanity bound
against runaway false-positive seeding. Threshold lives in `internal_config`.

## Implementation order

1. Spec review (this doc). Pedro accept / amend.
2. Edit `20260601000000_skill_asset_linker_cutover.sql` per above (DDL only —
   tables, views, function, cron, watchdog update).
3. Write `modal_workers/scripts/seed_fda_asset_aliases.py` + unit tests
   (`tests/test_seed_fda_asset_aliases.py`).
4. Extend `modal_workers/tests/test_skill_asset_linker_cutover_migration.py`
   for new objects (tables, view shape, function existence, cron job
   protected, queue is edge-shaped).
5. Update local Cursor `asset-linker` skill (separate `conan-cowork-skills`
   repo) to consume edges from `v_asset_linker_skill_queue` new shape.
6. `supabase db push` against staging branch first; smoke `seed_fda_asset_aliases
   --dry-run --asset-id <one>`; confirm output.
7. Full seed: `seed_fda_asset_aliases` (all assets, all sources, write mode).
   Expect ~250–800 alias rows from the 81 active assets.
8. Operator review pass: spot-check `v_recent_auto_aliases`, deactivate obvious
   false positives (`UPDATE fda_asset_aliases SET active=false,
   inactive_reason=... WHERE id IN (...)`).
9. Kick `fn_generate_doc_asset_candidates(50000)` once to populate edges over
   the 3142-doc backlog.
10. Manual Modal redeploy from xenodochial worktree per memory
    `orchestrator_deploy_topology`.

## Verification (add to skill_asset_linker_verification.md)

- `cron.job` has `v3-doc-asset-prefilter` active.
- `SELECT fn_generate_doc_asset_candidates(50)` on a fresh batch returns
  non-zero rows and writes to `doc_asset_candidates`.
- `v_asset_linker_skill_queue` returns edges, not bare docs.
- Skill dry run consumes top N edges without writes; expected output: one
  decision per edge.
- Skill write run inserts `asset_documents` + `document_asset_linker_attempts`
  rows AND stamps `doc_asset_candidates.analyzed_at`.
- Backfill: kick `fn_generate_doc_asset_candidates(50000)` once over the
  3142-doc backlog so the queue catches up. Expect <60s wall time, near-zero
  CPU on Supabase side.

## Risks & open questions

- **Alias recall (post-seed).** Even with seed pass, gaps remain — e.g. drug
  referenced by mechanism only, brand approvals not yet in openFDA at ingest
  time, foreign trial registrations outside ClinicalTrials.gov. Skill outputs
  a "missed-alias suggestion" sidecar (whenever the skill itself names an
  asset the prefilter didn't surface) that operator triages into
  `fda_asset_aliases`. Track as separate operator routine.
- **Match precision.** Word-boundary regex on `raw_text` against generic
  aliases (`peptide`, `mRNA`) would flood. Need a CHECK on
  `fda_asset_aliases.alias_normalized` rejecting the `v_asset_linker_skill_assets`
  blocklist (`peptide`, `concept`, `default`, `ex-99`, `(auto-discovered)`,
  `nucleotide`) plus a minimum-length constraint (`length >= 3`). Plus per-kind
  shape constraints: `nct_id` must match `^NCT\d{8}$`, `ticker`-style codes
  must be uppercase-alphanum.
- **Auto-alias false positives.** openFDA returns brand names that can be
  shared across competing products (e.g. extended-release variants under
  same label setid). Mitigation: `v_recent_auto_aliases` review surface +
  operator-flag threshold. Long-term: only auto-activate aliases when at
  least 2 distinct sources agree (e.g. openFDA brand + ClinicalTrials
  intervention name) — defer to v2.
- **Skill repo change is sequential, not lockstep.** Per Pedro's call
  2026-05-20, JGoror is not currently running skills on Windows, so the
  Cursor `asset-linker` skill update can ship at our pace from Mac side.
  No multi-machine coordination needed for this cutover.
- **Concurrency.** If sweeper runs while assets are being added,
  `alias_set_hash` changes mid-run. Current shape handles this — each
  candidate row carries the hash it was matched under, and
  `v_asset_linker_skill_queue` filters to the current hash. Stale-hash
  rows linger harmlessly until the next sweep.
- **Pass-2 verification.** Untouched here. Edges feed pass-1 only; pass-2
  continues against `asset_documents` rows. Worth a follow-up on whether
  pass-2 also belongs on an edge model.
