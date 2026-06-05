# Phase 1 — live weekly score (score-as-rank) + feature substrate

**Project:** Light v4 (BC-FDA monitor) — monitor-first FDA catalyst tool.
**Status:** detail plan, ready to build. **Completion-gated on Phase 0** (needs `bc_application_features`
rows with a non-NULL `pdufa_date`). **Author date:** 2026-06-03. **Reconciled:** 2026-06-04 (merged
`bc_v4_phase1_live_score.md` + `bc_v4_phase1_live_scorer.md` into this single canonical file; see
**§9 Reconciliation notes**).
**Parent:** `~/.claude/plans/plan-the-high-level-peppy-shell.md` (Phase 1 brief).
**Siblings (read alongside):** `tasks/bc_v4_phase0_universe_spike.md` (the GATE — populates the universe
this phase scores), `tasks/bc_v4_a0_cohort_confidence.md` (the offline OOS validation; **reuse its
feature-builder definitions** so live + A0 share one code path),
`tasks/bc_v4_phase2_synthesis_contract.md` (downstream consumer of `bc_rubric_scores.risk_band`).
**Supabase project:** `xvwvwbnxdsjpnealarkh`.

> **One-sentence goal.** A **weekly Modal cron** that, for the Phase-0 universe (`bc_application_features`
> rows with a `pdufa_date`), (1) builds the M14 feature substrate **point-in-time**, (2) scores each name
> with the **already-vendored** M14 scorer (`modal_workers/shared/fda_crl`), and (3) writes a **risk-band +
> percentile rank** to `bc_rubric_scores` (with the M14 columns on `bc_application_features` populated). The
> score is a **demoted ranking input** — `p_crl` is **persisted internally** (the `bc_candidates` matview
> gate keys on it) but is **never surfaced** in the digest/dashboard. Fail-loud: every run writes a
> `bc_pipeline_runs` row. Score rarely changes → weekly cadence.

This is a **thin, deterministic, zero-LLM** worker. No Cowork, no Anthropic call anywhere on this path.

> **Product scope reminder (Pedro, 2026-06-03 — BAND-ONLY v1).** v1 ships **band-only**: no Polygon options
> upgrade, no market-implied-move framing (deferred to v1.1), synthesis `recommended_action` capped at
> `monitor`. Phase 1 emits `risk_band` + `oof_percentile_rank` (and stores `p_crl` internally for the
> matview gate). The display layers (Phase 2/3/4) render **only** the band + rank — never `p_crl`, never an
> options-derived implied move. (Polygon options code *exists* at
> `modal_workers/providers/polygon/options_data.py` but the live key 403s on the options entitlement — out
> of scope for v1; see §9.)

---

## 0. Ground truth established during planning (build on these; DO NOT re-derive)

All verified live against `xvwvwbnxdsjpnealarkh` and by reading the code on 2026-06-03 (some re-verified
on 2026-06-04 during reconciliation).

### 0.1 The scorer is ALREADY VENDORED in the repo — the brief's "vendor it" step is DONE

> **AUTHORITATIVE CORRECTION (supersedes any "vendor `score_m14_adjusted.py` from `~/Downloads`" step in
> earlier drafts).** The M14 scorer is **already vendored** in the repo as a byte-faithful port. Phase 1
> **imports** it — it does **NOT** copy the `~/Downloads` script or model JSON into the repo (a second copy
> would create a two-model-JSON drift hazard).

- **`modal_workers/shared/fda_crl/nda_scorer.py`** — public entrypoint **`score_nda(row: dict) -> dict`**
  (alias of `score_row`, `nda_scorer.py:206`). Pure stdlib (`json`/`math`/`functools`), `@lru_cache`d model
  load (`nda_scorer.py:41-42`).
- Model: **`modal_workers/shared/fda_crl/models/nda_m14_adjusted.json`** — **byte-identical** to
  `~/Downloads/BC_scoring_rubrics_export/NDA_M14_adjusted/model/m14_adjusted_model.json` (verified via
  canonicalized-JSON `diff`: *MODELS IDENTICAL*). `model_version = "M14_ADJUSTED_L1_ORIG_v1_1_2026-05-30"`
  (`nda_scorer.py:25` `NDA_MODEL_VERSION`). 13 features, `lr_intercept=-1.313`, Platt `a=0.9305,b=-0.3928`
  (fit on 2024 val); biggest coefficient `is_bla=+1.266`; the three no-source features
  (`ctgov_failed_primary`, `ctgov_any_randomized`, `sponsor_has_orphan_history`) carry ~4.7% of |coef| and
  are intentionally absent.
- Fidelity fixtures: `modal_workers/shared/fda_crl/testdata/{example_input,example_output}.csv` —
  **byte-identical** to the Downloads `examples/` pair (verified; 3 rows incl. one refused `supplemental`).
- Exported from `modal_workers/shared/fda_crl/__init__.py`: `score_nda`, `NDA_MODEL_VERSION`, plus
  `to_percentile`, and (via the package) `score_row`, `classify_scope`, the sNDA scorer.

**Consequence:** Phase 1 does `from modal_workers.shared.fda_crl import score_nda, NDA_MODEL_VERSION` and
calls `score_nda(feature_dict)`. **No scorer file is created in this phase.** The only "vendoring" action
in Phase 1 is the *percentile reference CSV* (§0.6) — a data artifact, not the scorer.

> The only defensible "vendoring" mental model: treat `modal_workers/shared/fda_crl/` as the source of
> truth and the `~/Downloads` copy as a throwaway export.

### 0.2 The scorer's input-key contract (verified by reading `nda_scorer.score_row`)

`score_nda(row)` reads these keys (first alias present wins; everything missing defaults to 0 via
`as_int/as_float/as_binary/first_float`). The two **refusal gates** are the only hard exits
(`nda_scorer.score_row` refusal block): `cycle_type != 'first_cycle_orig'` ⇒ REFUSED;
`is_biosimilar_bla == 1` ⇒ REFUSED. **Every other absent feature defaults to 0** — a sparse dict produces a
*meaningful-but-low-coverage* score, never an error, so the builder MUST attach a coverage signal (§4) —
the scorer will not warn you.

| scorer concept | row keys it reads (in order) | default when absent | coef / notes |
|---|---|---|---|
| **cycle gate** | `cycle_type` | `'first_cycle_orig'` | `!= 'first_cycle_orig'` ⇒ **REFUSED** |
| **biosimilar gate** | `is_biosimilar_bla`, `biosimilar`, `is_biosimilar` | `0` | `==1` ⇒ **REFUSED** |
| `is_bla` | `is_bla`, else `ApplType`/`appl_type`=="BLA" | `0` | **+1.27** (dominant) |
| `priority` | `priority`, else `ReviewPriority`/`review_priority` token | `0` | −0.55 |
| `type5_or_3` | `type5_or_3`, else `SubmissionClassCode`/`submission_class` ∈ {TYPE 3, TYPE 5, TYPE 3 4, TYPE 5 6} | `0` | +0.31 |
| `n_prior_filings_log` | `n_prior_filings_log`, else `log1p(n_prior_filings\|sponsor_history\|n_prior_filing_events)` | `log1p(0)=0` | −0.18 |
| `sponsor_has_warning` | `sponsor_has_warning`, `sponsor_warning`, `has_warning_letter` | `0` | +0.63 |
| `n_drug_inspections_log` | `n_drug_inspections_log`, else `log1p(n_drug_inspections_5y_fix\|n_drug_inspections_5y\|n_drug_inspections)` | `log1p(0)=0` | −0.41 |
| `has_bt` | `has_bt`, `breakthrough`, `breakthrough_therapy` | `0` | −0.24 |
| `has_ft` | `has_ft`, `fast_track` | `0` | −0.03 |
| `has_aa` | `has_aa`, `accelerated_approval` | `0` | −1.21 |
| `n_8ks_30_180_clean` | `n_8ks_30_180_clean`, `n_8ks_30_180`, `edgar_8k_count_30_180` | `0` | −0.64 (2nd-largest) |
| `sponsor_has_orphan_history` | `sponsor_has_orphan_history` | `0` | −0.04 (no source) |
| `ctgov_failed_primary` | `ctgov_failed_primary`, `failed_primary` | `0` | −0.03 (no source) |
| `ctgov_any_randomized` | `ctgov_any_randomized`, `…_pre_event`, `any_randomized` | `0` | +0.21 (no source) |

**Output keys** (strings; numbers are `"%.8f"`-formatted; merged on top of the input row —
`out = dict(row); out.update(...)`): `p_crl`, `raw_p_uncalibrated`, `ci_low`, `ci_high`, `risk_band`
(`low<0.08≤moderate<0.15≤elevated<0.25≤high`), `confidence_flag`, `refusal_reason`, `model_version`. On
refusal all numeric outputs are `""`. The persister (§3.3) **casts the numerics and renames
`model_version` → `scorer_version`** + supplies `scorer_name`.

`confidence_flag` is a `;`-joined token string in append order: `low_confidence_sponsor` (if
`n_prior_filings<2`), then `moderate_confidence_no_edgar_signal` (if `n_8ks_30_180_clean<=0`), then
`probability_extrapolation` (if `p_cal>0.35`), else `standard`; refused rows emit `confidence_flag="refused"`.
**The leading token is always in the `bc_rubric_scores` CHECK's allowed set (§0.5)** → write the scorer's
string verbatim, no re-map.

**1:1 mapping to `assemble_nda_features` confirmed:** the offline `feature_assembly.assemble_nda_features`
output keys map 1:1 to the scorer's input aliases (`is_bla`, `ApplType`, `priority`, `SubmissionClassCode`,
`n_prior_filings`, `n_drug_inspections_5y_fix`, `sponsor_has_warning`, `has_bt/ft/aa`, `n_8ks_30_180_clean`,
`cycle_type`). **Verified by reading both** (`feature_assembly.assemble_nda_features` ↔ `nda_scorer.score_row`).

### 0.3 The feature substrate is ABSENT live — but the submissions table DDL + writer BOTH already exist

`feature_assembly.assemble_nda_features(client, asset, event, *, ref_date, …)` sources four tables, and
`feature_assembly._rows()` **swallows every error and returns `[]`** — so calling it unchanged against the
live DB silently defaults `priority`, `SubmissionClassCode`, `n_prior_filings`, `n_drug_inspections_5y_fix`,
`sponsor_has_warning`, AND the 8-K count → a **uniform, meaningless score** (collapses toward the intercept;
no ranking signal). **This is the crux of Phase 1.**

| table (`feature_assembly` const) | live? | live state |
|---|---|---|
| `fda_application_submissions` (`SUBMISSIONS`) | **NO** (`to_regclass` = NULL) | missing — **but DDL + writer exist on disk (see below)** |
| `fda_drug_inspections` (`INSPECTIONS`) | **NO** | missing; the fetcher's HTTP path (`fda_inspections.py:~134`) **raises `NotImplementedError`** — never run |
| `fda_warning_letters` (`WARNING_LETTERS`) | **YES** | **EXISTS but EMPTY (0 rows)** — fetcher not scheduled |
| `documents` (`DOCUMENTS`) 8-K path | **YES** | populated, but **NO `entity_id` column** (the 8-K join key `feature_assembly._n_8ks_30_180` reads at `:278` via `asset["entity_id"]`); only ~424 8-K rows total, with CIK stashed in `extensions->'ciks'`, not a column. ⇒ **route the 8-K count via EFTS count-by-CIK** (§1.3b), not the `documents` table. |

> **AUTHORITATIVE CORRECTION (supersedes any "ingest the four tables first" / "Option B" framing).** The
> `fda_application_submissions` substrate is **NOT** a from-scratch build: the table's **DDL is already on
> disk unapplied** AND its **writer already exists**. Phase 1 = *apply the existing migration + a targeted
> Drugs@FDA pull*, not a new fetcher. `fda_warning_letters` stays empty (dropped for v1, §1.1).

1. **`fda_application_submissions` DDL is already written on disk** —
   `supabase/migrations/20260615000000_fda_application_submissions.sql` (verified). It is simply **not
   applied** (table absent live). Shape (verified): PK
   `(application_number, submission_type, submission_number)`; columns `submission_status,
   submission_class_code (TYPE 1..10 | EFFICACY | LABELING | …), submission_class_code_description,
   review_priority (PRIORITY | STANDARD | NULL), submission_status_date (date), sponsor_name, ticker,
   source NOT NULL DEFAULT 'openfda_drugsfda', refreshed_at`. It is the **single source for `type5_or_3`
   (class code), `priority`, and `n_prior_filings` (ORIG submission history)** — exactly the sponsor-history
   features `feature_assembly` needs.
2. **The writer already exists** — `modal_workers/ingestion/openfda_ingest.py`:
   `extract_submission_rows(app, sponsor_name=…, ticker=…)` (`:288`, pure, unit-testable) maps a drugsfda
   record → `{submission_type, submission_class_code, review_priority, submission_status_date, sponsor_name}`
   + `_upsert_application_submissions(rows, client)` (`:331`, upsert
   `on_conflict=application_number,submission_type,submission_number`, `resolution=merge-duplicates`). It runs
   today as a best-effort side-effect of `ingest_drugsfda_approvals` (`:119`), writing into this table when
   it exists.

⇒ Phase-1 substrate work is **"apply the existing migration + run a targeted Drugs@FDA pull for the
universe's application numbers + their sponsors, populating `fda_application_submissions`"** — *not* a
from-scratch build, *not* "ingest all four tables."

### 0.4 Live `bc_application_features` contract (verified columns + NOT-NULL + UNIQUE + CHECKs)

PK `id (uuid)`; **UNIQUE `(sponsor_cik, application_number, snapshot_date)`**; FK `application_number →
bc_applications`. NOT-NULL columns Phase 1 must supply (DB-filled defaults in parens):

`sponsor_cik`, `sponsor_name`, `application_number`, `appl_type` — **NOT NULL, no default → must set**.
`cycle_type` (default `'first_cycle_orig'`), `is_biosimilar_bla` (default `false`), `as_of_date` (default
`CURRENT_DATE`), `snapshot_date` (default `CURRENT_DATE`), `built_at` (default `now()`).

**CHECK constraints (verified live 2026-06-03 — load-bearing):**
- `appl_type ∈ {NDA, BLA, sNDA, sBLA}`
- `review_priority ∈ {STANDARD, PRIORITY}` (nullable)
- **`feature_quality ∈ {standard, low, built_at_install}`** (default `'standard'`).

> **AUTHORITATIVE CORRECTION on `feature_quality` (supersedes any `'phase1_scored'` /
> `'phase1_scored_low_coverage'` / `'phase0_universe'` / `'phase0_surrogate_appl'` token in earlier drafts —
> those VIOLATE the live CHECK).** Phase 1 writes only **`'standard'`** or **`'low'`**, re-derived from
> per-row coverage (§6: `'low'` when `_coverage < COVERAGE_FLOOR`, else `'standard'`). **Phase-0 ↔ live
> CHECK drift is RESOLVED (2026-06-03, Option A, no schema change):** Phase 0 §3.2 also writes only
> `'standard'`/`'low'` (`'low'` for surrogate-appno rows where `application_number LIKE 'EDGAR8K:%'`) and
> carries surrogate-vs-real provenance on the `EDGAR8K:` appno prefix, NOT on a `feature_quality` token.
> Phase 1 overwrites Phase 0's token on the shared snapshot row.

M14 feature columns Phase 1 **UPDATES** (Phase 0 leaves them NULL; defaults in parens): `review_priority`,
`submission_class_code`, `n_prior_filings (int, def 0)`, `n_drug_inspections_5y_fix (int, def 0)`,
`n_8ks_30_180_clean (int, def 0)`, `sponsor_has_warning (bool, def false)`, `has_bt/has_ft/has_aa (bool, def
false)`, `sponsor_has_orphan_history (bool, def false)`, `ctgov_failed_primary`/`ctgov_any_randomized` (bool,
nullable, no default), plus `feature_quality` and `required_feature_missing_count (int, def 0)`. The sNDA-only
`act_*` / `months_since_orig_*` / `sponsor_prior_crl*` columns stay NULL — out of scope (M14 is NDA/BLA-only).

> **Identity-vs-features ownership.** Phase 0 owns the identity + `pdufa_date` + designations + NOT-NULL
> placeholders on a `snapshot_date=today` row. Phase 1, **on the same `snapshot_date`**, fills the M14
> numeric/bool feature columns. Because the matview reads `DISTINCT ON (application_number) ORDER BY
> snapshot_date DESC, built_at DESC`, the two writers must **converge on ONE row per
> `(sponsor_cik, application_number, today)`**: Phase 1 carries forward Phase-0's `pdufa_date` /
> designations into its own merge-duplicates upsert (newest `built_at` wins) and **never blanks
> `pdufa_date`** (the matview G3 window gate needs it). See §3.2.

### 0.5 Live `bc_rubric_scores` contract (verified) — and the score-as-rank reframe

PK `id (uuid)`; **UNIQUE `(application_number, scored_at, scorer_name)`**; FK
`application_number → bc_applications`, `features_id → bc_application_features.id`. NOT-NULL:
`application_number`, `scored_at (default now())`, `scorer_name`, `scorer_version`. Nullable payload:
`p_crl (numeric)`, `raw_p_uncalibrated`, `ci_low`, `ci_high`, **`oof_percentile_rank (numeric)`**,
`confidence_flag (text)`, `risk_band (text)`, `refusal_reason (text)`, **`features_id (uuid)`**.

**CHECKs (verified):**
- `scorer_name ∈ {M14_adjusted, sNDA_pooled}` → Phase 1 writes `'M14_adjusted'`.
- `risk_band ∈ {low, moderate, elevated, high}` (= the scorer's 0.08/0.15/0.25 cuts — no mapping needed).
- `confidence_flag`: `NULL OR ~ '^(standard|low_confidence|no_edgar_signal|refused|
  synthetic_or_unverified_submission_id|probability_extrapolation|low_confidence_sponsor|
  moderate_confidence_no_edgar_signal)(;.*)?$'`. The scorer's leading token is always in this set →
  **write the scorer's `confidence_flag` string verbatim** (copy this regex into the conformance test, §7.6).

**Scorer dict → table row mapping (load-bearing):**
| scorer key | DB column | transform |
|---|---|---|
| `p_crl` (str) | `p_crl` | `float()` or NULL if `""` |
| `raw_p_uncalibrated` (str) | `raw_p_uncalibrated` | `float()`/NULL |
| `ci_low`/`ci_high` (str) | `ci_low`/`ci_high` | `float()`/NULL |
| `risk_band` (str) | `risk_band` | verbatim or NULL if `""` |
| `confidence_flag` (str) | `confidence_flag` | verbatim |
| `refusal_reason` (str) | `refusal_reason` | verbatim (`""`→NULL) |
| `model_version` (str) | **`scorer_version`** | **rename** |
| — | `scorer_name` | constant `'M14_adjusted'` |
| — | `oof_percentile_rank` | **computed by Phase 1** (§0.6 / §3.3), NOT from the scorer |
| — | `features_id` | the `bc_application_features.id` (§3.2) that produced this score (FK) |

**Two load-bearing facts about how `bc_candidates` reads this (verified via `002_bc_candidates_view.sql`):**
- `bc_candidates.latest_score` joins **`s.scorer_name = 'M14_adjusted'`** for `appl_type IN ('NDA','BLA')`
  (`002_bc_candidates_view.sql:29`). ⇒ Phase 1 **MUST write `scorer_name='M14_adjusted'`** (the model's
  `model_version` goes in `scorer_version`). A mismatch silently drops every score from the matview.
- The matview's G1 gate for NDA/BLA is **`r.p_crl IS NOT NULL AND r.p_crl <= tau_nda`** (active) and
  `… <= tau_nda_watchlist` (watchlist) (`002_bc_candidates_view.sql:52,56`; `tau_nda=0.30`, watchlist
  `0.50`). **So `p_crl` MUST be persisted** to `bc_rubric_scores` — the matview's tier logic depends on it.
  This is **not** a contradiction of "score-as-rank": `p_crl` is **stored internally** (a *storage* fact)
  but **never displayed as a calibrated probability** (a *presentation* rule). The digest/dashboard surface
  only `risk_band` + `oof_percentile_rank`. Phase 1 writes all of
  `{p_crl, risk_band, oof_percentile_rank, ci_low/high, confidence_flag}`; suppressing `p_crl` from view is
  the Phase 2/3/4 display contract's job.

> **Idempotency (important):** the UNIQUE includes `scored_at` (`timestamptz` default `now()`). A naive
> re-run inserts a *new* row each time. **Decision (§3.3):** set `scored_at` explicitly to a **single
> per-run timestamp** (`run_started_at`, identical for all names in the run) and upsert
> `on_conflict=application_number,scored_at,scorer_name` with `resolution=merge-duplicates`. A re-run in the
> same week then updates in place; distinct weeks add history — one row per app per week.

### 0.6 `oof_percentile_rank` for NDA/BLA needs an M14 reference (the bundled `percentile.py` is sNDA-only)

`modal_workers/shared/fda_crl/percentile.py:to_percentile(raw_score, reference=None)` exists, but its
**bundled reference is the sNDA OOF set** (`models/snda_oof_reference.csv`, column `p_oof`) — *not*
applicable to NDA/BLA `p_crl`, and there is **no NDA percentile reference bundled.** Signature (verified):
`to_percentile` returns a **0..100** percentile = `fraction of reference ≤ raw_score × 100` (returns `0.0`
for an empty reference); a `reference` sequence overrides the bundled set.

**Decision (§3.3): persist `oof_percentile_rank` as the percentile of the name's calibrated `p_crl` within
the locked-2025 M14 calibrated-prediction distribution** —
`~/Downloads/BC_scoring_rubrics_export/NDA_M14_adjusted/data/locked_2025_predictions_m14_adjusted.csv`
(verified: column **`p_m14_cal`**; the model authors' own held-out calibrated scores — the natural empirical
anchor; same shape as the sNDA percentile). **Vendor that one CSV** into the repo as
`modal_workers/shared/fda_crl/models/nda_m14_locked2025_reference.csv` (the `p_m14_cal` column → `p_ref`, or
read by header) and call `to_percentile(p_crl, reference=<p_m14_cal list>)`. Direction note: higher `p_crl`
⇒ higher percentile ⇒ *worse* (riskier) rank; the display layer labels it "CRL-risk percentile, higher =
riskier" — do not invert it.

> **Reconciled conflict (see §9):** an alternative draft computed `oof_percentile_rank` as a *within-snapshot*
> rank (percentile of `p_crl` among the ~20 names scored this run). **Resolution:** the **persisted column =
> the locked-2025 reference percentile** (stable, model-anchored, matches the column-name intent and the
> merge brief's "oof_percentile NDA reference (locked_2025 p_m14_cal)"). The **within-snapshot ordering** is
> still computed for the digest, but lives in the **run log** (`bc_pipeline_runs.log.within_snapshot_rank`)
> and is the digest's sort key — it is NOT what `oof_percentile_rank` stores. This keeps the stored column
> reproducible run-over-run (a within-snapshot percentile would shift as the universe changes).

### 0.7 Fail-loud sink: `bc_pipeline_runs` (verified) — the only liveness sink (migration 005 unapplied)

`bc_pipeline_runs` columns (verified): `id`, `pipeline_name (NOT NULL)`, `started_at (default now())`,
`finished_at`, `status`, `snapshot_date`, `n_processed`, `n_failed`, `cost_usd`, `log (jsonb)`, `reason`.
**Zero rows; no code writes it today** — Phase 1 establishes the reusable open/close helper (§3 / §5,
adopted by Phase 0/2/3). Every run opens a row at start and closes it in a `finally`, even on exception.

> **AUTHORITATIVE CORRECTION on `status` values (supersedes earlier "no CHECK; use ok|partial|error"
> wording).** `bc_pipeline_runs.status` CHECK = **`{running, succeeded, partial, failed}`**. Phase 1's
> convention: open `running` → close terminal **`succeeded | partial | failed`** (`succeeded` = clean;
> `partial` = some rows scored but ≥1 threw mid-build, or the matview refresh lagged; `failed` = the run
> threw before any write). An empty universe closes `succeeded` with `n_processed=0` (honest-empty, not a
> failure).

**Migration 005 (`operator_flags` bc_ sources) is NOT applied** — the live `operator_flags_source_check`
lists the 29 v4 sources and **none of the `bc_*` ones**. The unapplied disk file
(`~/Downloads/BC_scoring_rubrics_export/migrations/005_operator_flags_bc_sources.sql`) would add
`bc_l1_feature_builder`, `bc_l2_refusal_spike`, … ⇒ **a `bc_*` `operator_flags` INSERT is REJECTED until
005 is applied.** Phase 1 therefore uses **`bc_pipeline_runs` as its ONLY liveness sink**. Applying 005 is
a cross-cutting prerequisite, **not** this phase's deliverable; if it lands, Phase 1 MAY additionally raise
a `bc_l2_refusal_spike` flag on an abnormal refusal rate (§4), guarded by a pre-flight CHECK introspection.

### 0.8 The universe this phase scores comes from Phase 0 (the GATE)

Phase 1 does **not** enumerate the universe; it reads what Phase 0 wrote. The pending PDUFA universe lives in
`bc_application_features` (Phase-0-populated identity + `pdufa_date`), corroborated by `fda_regulatory_events`
(`event_type='pdufa'`, ~32 future-dated, tickered). **If Phase 0 has not run / NO-GO, Phase 1 has nothing to
score** — the weekly cron writes a `bc_pipeline_runs` row with `status='succeeded', n_processed=0,
reason='empty_universe'` (honest-empty). Phase 1 is on the critical path **after** Phase 0.

### 0.9 Cron + idempotency + HTTP-reuse substrate (verified)

- **Weekly registry pattern:** `modal_workers/app.py::dispatch_weekly` (`@app.function(schedule=
  modal.Cron("0 12 * * 0"))`, Sun 12 UTC, `app.py:906`) → `_load_cadence_names("weekly", …)` (`app.py:747`)
  → `SupabaseClient.load_operational_names_by_cadence('weekly')` (`supabase_client.py:247`) →
  `_dispatch(names)` (`app.py:801`) spawns `<name>_once.spawn()`. **Registry (`public.scanners`) is source
  of truth** (`scanner_registry_vs_db.md`); `_dispatch` status-gates on `{operational, shadow,
  shadow_with_emit}`. Adding a weekly job = one `scanners` INSERT + a `<name>_once` Modal function — **no
  new cron slot** (stays under Modal's 5-cron cap, §2.2).
- **Idempotency idiom:** `supabase_client._rest_with_retry(method, path, *, params, json_body, prefer,
  attempts=3, backoff_s=0.25)` (`:145`, retries 429/5xx); upsert = `params={"on_conflict": "<cols>"}` +
  `prefer="resolution=merge-duplicates,return=minimal|representation"` (exemplars:
  `bc_class_precedent_refresher.upsert_base_rates :202-223`, `openfda_ingest._upsert_application_submissions
  :331-345`). `SupabaseClient()` reads `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` from env; service-role,
  RLS bypassed.
- **Corroboration sources (verified):** Drugs@FDA — `openfda_client.openfda_get("drug/drugsfda.json", {…})`
  (`:101`, authenticated, paginated, 404→None) + `openfda_auth_params` for `OPENFDA_API_KEY`. EDGAR 8-K
  count — `shared/edgar_efts.efts_search(query, date_from, date_to, *, forms, size, user_agent)` (`:32`).
  Sponsor→CIK — `entity_identifiers (id_type='cik')` / `shared/sponsor_resolver.resolve_sponsor` /
  `fda_assets` (157 rows, biotech-skewed). CLI + dry-run exemplar:
  `modal_workers/scripts/bc_class_precedent_refresher.py` (`_cli`, upsert, dry-run `:202-278`).

---

## 1. THE v1 FEATURE SET + SUBSTRATE (answering "minimize sources")

### 1.1 v1 feature decision (per the endorsed ablation: keep high-signal, drop whole integrations)

For each universe application, build a scorer-input dict with **only** these sourced features; everything
else relies on the scorer's documented defaults (safe — the scorer is *designed* to default absent features,
§0.2):

| feature | v1 source | rationale / coef |
|---|---|---|
| `is_bla` / `ApplType` | **appno prefix** (`feature_assembly.appl_is_bla :82`) — free, exact | dominant +1.27; always available |
| `priority` / `review_priority` | `fda_application_submissions` ORIG row `review_priority` (substrate pull) | −0.55 |
| `type5_or_3` / `SubmissionClassCode` | `fda_application_submissions` ORIG `submission_class_code` (substrate pull) | +0.31 |
| `n_prior_filings` | distinct prior ORIG appnos for the sponsor before `ref` (substrate) | −0.18 |
| `n_8ks_30_180_clean` | **EDGAR EFTS 8-K count-by-CIK** in `[ref−180, ref−30]` (§1.3b) — the one EDGAR signal we keep | −0.64 (2nd-largest) |
| `has_bt` / `has_ft` / `has_aa` | **inherited from Phase-0 designations** on `bc_application_features` (NULL→default 0) | soft; Phase 0 best-effort-extracts from 8-K text |
| `cycle_type` | **constant `'first_cycle_orig'`** (pending universe is first-cycle-original by Phase-0 construction) | gates the scorer |
| `is_biosimilar_bla` | **carry Phase-0 value, else `false`/0** (Phase 0 excludes biosimilars) | gates the scorer |

**DROPPED for v1 (explicit, with justification):**
- **Inspections (`n_drug_inspections_5y_fix`)** — `fda_drug_inspections` is absent live AND the fetcher's
  HTTP path raises `NotImplementedError` (never run); an openFDA `drug/inspections` pull is a whole new
  integration for a single −0.41 feature. **Drop → scorer defaults `log1p(0)=0`.** (Carries `−0.414` |coef|
  — material; the most worthwhile to wire next. A0 quantifies the cost of leaving it at default.)
- **Warning letters (`sponsor_has_warning`)** — `fda_warning_letters` EXISTS but is **EMPTY**; a populated
  source is a separate fetcher. **Drop → default 0.** The builder still *queries* the table (mirrors
  `feature_assembly._sponsor_has_warning :256`) so it lights up for free once some other workstream
  populates it.
- **CT.gov booleans** (`ctgov_failed_primary`, `ctgov_any_randomized`) + `sponsor_has_orphan_history` —
  **always dropped** (no production source, together ~4.7% |coef|), matching `feature_assembly`'s documented
  honest gap and the high-level plan's "drop CT.gov for v1."

**Why dropping these is safe:** `nda_scorer.score_row` defaults every absent numeric/boolean to 0 and only
*refuses* on `cycle_type`/`is_biosimilar_bla` (§0.2). Dropped features push `risk_band` toward the base rate
for low-coverage names rather than erroring — exactly the graceful degradation `feature_assembly` was built
for. The plan records, per name, a **coverage fraction** over the kept high-signal features (§1.4 / §6) so
the rank's confidence is visible.

### 1.2 Substrate build — REUSE the A0 offline point-in-time builder (one code path, two callers)

> **DECISION: production point-in-time feature builder, NOT `assemble_nda_features` directly, shared with
> A0.** `assemble_nda_features` reads `asset["entity_id"]` for the 8-K count (`feature_assembly.py:278`) — a
> column that **does not exist on the live `documents` table**, and an `entity_id` the cohort sponsors will
> not have. The point-in-time (PIT) builder uses a **sponsor→CIK→EFTS** resolution that works against the
> live schema (§1.3b). It mirrors `feature_assembly`'s definitions exactly and is parity-tested against it
> (§7.2) so the A0 OOS metrics transfer to the live weekly scorer.
>
> **Why a builder, not "Option B" (ingest the four tables first):** Option B is *blocked* (inspections
> fetch unimplemented), *mutates* shared tables for a read-only scoring need, and still yields a
> biotech-skewed sparse substrate. The builder keeps the score path self-contained and OOS-transferable.
> Option B remains the *correct long-term* substrate (filed as a hand-off, §8) — when `fda_inspections` /
> `fda_warning_letters` are live, the builder's inspection/warning branches light up with **zero scorer
> change**.

The A0 plan (`tasks/bc_v4_a0_cohort_confidence.md` §3.2) specs a thin offline PIT feature builder
(`analysis/bc_a0/feature_builder.py`) mirroring `feature_assembly`'s definitions, parity-tested against
`assemble_nda_features`. **Phase 1 and A0 share ONE module** (the whole point of A0 is to validate the *same*
feature substrate the live path uses):

- **Canonical path: `modal_workers/shared/fda_crl/feature_builder_pit.py`** (point-in-time). It must live
  under `modal_workers/shared/` because the **weekly cron runs in a Modal container that has the repo, not
  `analysis/`-only tooling, and A0 can import from there too**. Exposes:
  ```
  build_features_pit(client, *, application_number, sponsor_cik, sponsor_name, appl_type,
                     ticker, cik, ref_date, designations, is_biosimilar_bla=False) -> dict
  ```
  returning the scorer-input dict + a `_coverage` float + a `_provenance` map (which source filled each
  feature, for the run log), byte-aligned with `assemble_nda_features` windows.
  - **If A0 ships first:** Phase 1 imports the already-written builder (move it from `analysis/bc_a0/` to the
    shared path; leave a thin re-export at `analysis/bc_a0/feature_builder.py` so A0's imports keep working).
  - **If Phase 1 ships first:** write it at the shared path; A0's `feature_builder.py` becomes a thin
    re-export. Either way **one implementation, one parity test (§7.2)**.
- The builder sources each v1 feature point-in-time (≤ `ref_date`):
  - identity / `is_bla`: appno prefix (`appl_is_bla`).
  - `priority` / `SubmissionClassCode` / `n_prior_filings`: from `fda_application_submissions` (now
    populated — §1.3a), via the same `_orig_submission :199` / `_n_prior_filings :213` logic
    `feature_assembly` uses.
  - `n_8ks_30_180_clean`: EFTS-by-CIK (§1.3b).
  - `sponsor_has_warning`: `fda_warning_letters` by `firm_name_norm`/`sponsor_ticker`, `issue_date <= ref`
    (mirrors `_sponsor_has_warning :256`; empty table → default 0).
  - designations (`has_bt/ft/aa`): passed in from the Phase-0 feature row (not re-derived).

**Byte-alignment requirements (so OOS metrics transfer):** **import** the constants from `feature_assembly`
rather than re-declaring them — `_INSPECTION_WINDOW_DAYS = 5*365 :44`, `_EDGAR_8K_LO_DAYS = 180 :45`,
`_EDGAR_8K_HI_DAYS = 30 :46`, `_NDA_COVERAGE_KEYS :50`. Identical priority/class mapping
(`STANDARD`/`PRIORITY`; `TYPE 3`/`TYPE 5` → `type5_or_3`). Identical **"absent ⇒ omit (not 0)" discipline**
— let the scorer default, so the coverage signal is honest.

### 1.3 Sourcing `fda_application_submissions` (substrate pull) + `n_8ks_30_180_clean` (EFTS-by-CIK)

**(a) `fda_application_submissions` — apply the existing migration, then a targeted Drugs@FDA pull.**
1. **Apply `supabase/migrations/20260615000000_fda_application_submissions.sql`** (the table DDL already on
   disk, §0.3) via `supabase db push` (disk-first discipline — `feedback_mcp_apply_migration_discipline.md`).
   This is a pre-existing, reviewed `CREATE TABLE IF NOT EXISTS` for a new table with no dependents —
   additive and safe. Re-introspect after apply.
2. **Populate it for the universe.** For each distinct `application_number` in the Phase-0 universe (and each
   distinct sponsor), pull `submissions[]` from Drugs@FDA and upsert via the **existing writer**:
   `extract_submission_rows(app, sponsor_name, ticker)` → `_upsert_application_submissions(rows, client)`.
   - **Preferred (targeted):** `openfda_get("drug/drugsfda.json", {"search": f'application_number:"{appno}"'})`
     per universe appno (≤ ~20 names ⇒ ≤ ~20 calls; well under the 240/min and 1,000/day caps; set
     `OPENFDA_API_KEY` to lift the daily cap). For `n_prior_filings`, **also** do a per-sponsor
     `sponsor_name:"…"` pull (de-duped, cached within the run — `openfda_rate_limit_gap.md`) to capture prior
     ORIG appnos.
   - **Fallback (broad):** `ingest_drugsfda_approvals(since=…, until=…)` already populates the table as a
     side-effect — but it is approval-window scoped and may miss a *pending* appno with no approved
     submission yet. Prefer the targeted pull for pending names; use the broad path only as a backstop.
3. **Idempotent:** the writer upserts on `(application_number, submission_type, submission_number)` — a
   re-run refreshes status/class without fanning rows.

**(b) `n_8ks_30_180_clean` via EFTS count-by-CIK (NOT the `documents` table).** The live `documents` table
has only ~424 8-K rows and **no `entity_id` column** (CIK lives in `extensions->'ciks'`); the asset↔doc link
is a classifier (`linker_classified_*`) the cohort sponsors won't have. **v1 sourcing:**
- Resolve sponsor → CIK using the same anchors Phase 0 uses (`entity_identifiers` `id_type='cik'`,
  `sponsor_resolver.resolve_sponsor`); Phase 0 also stashes the filer CIK on the feature row during universe
  enumeration (Phase 0 §0.5/§0.7) — prefer that when present.
- Count 8-Ks in `[ref−180, ref−30]` by CIK via `shared/edgar_efts.efts_search(forms="8-K",
  date_from=ref−180, date_to=ref−30)` filtered to the filer CIK (mirrors Phase 0's EDGAR reuse; keeps the
  8-K definition identical to Phase 0's and to `feature_assembly._n_8ks_30_180 :274`).
- **If no CIK resolves, leave `n_8ks_30_180_clean` ABSENT** (the builder omits it → scorer defaults 0 and
  raises `moderate_confidence_no_edgar_signal`) and increment the coverage counter. **Do NOT fabricate a 0
  as a real value** — absent vs zero is the confidence signal the scorer keys on.

### 1.4 Coverage + missing-application_number degradation

- The PIT builder returns `_coverage` = fraction of the kept v1 high-signal keys present (the
  `_NDA_COVERAGE_KEYS` subset intersected with v1's kept set: `is_bla, priority, SubmissionClassCode,
  n_prior_filings, has_bt, has_ft, has_aa, n_8ks_30_180_clean`). Persist it to
  `bc_application_features.feature_quality` (`'low'` below `COVERAGE_FLOOR`, else `'standard'` — §6) and to
  `required_feature_missing_count` (count of kept keys that fell back to default).
- **Missing `application_number` (common on pending 8-K-sourced events) degrades the rank, not blocks it.**
  When the universe row has a **surrogate appno** (`EDGAR8K:<cik>:<slug>` — Phase 0 §3.1) or no real NDA/BLA
  number:
  - `is_bla` still derivable from Phase-0 `appl_type`; `n_8ks_30_180_clean` still sourceable by CIK.
  - **But the substrate-derived features (`priority`, `SubmissionClassCode`, `n_prior_filings`) cannot be
    sourced** (no Drugs@FDA submission record for a surrogate appno) → they fall to default → weaker,
    base-rate-compressed rank. This is **acceptable degradation**, surfaced via:
    - `feature_quality='low'` (vs `'standard'`),
    - the scorer's own `confidence_flag` (`low_confidence_sponsor` when `n_prior_filings<2`),
    - and a `bc_pipeline_runs.log` per-name `coverage` entry.
  - **No row is dropped for a missing appno** — the score still ranks (the digest shows it with the
    low-coverage caveat). The display layer (Phase 3/4) reads `feature_quality`/`confidence_flag` to badge it.

---

## 2. THE WEEKLY CRON WIRING

### 2.1 Worker entrypoint + Modal function

- **New worker:** `modal_workers/bc_score/run_weekly.py` exposing `run_weekly(client=None, *, apply=True,
  snapshot_date=None, limit=None, application_number=None) -> dict` (pure orchestration; testable with a fake
  client; CLI dry-run default mirrors `bc_class_precedent_refresher._cli`). It:
  1. Opens a `bc_pipeline_runs` row via the reusable helper (§5): `pipeline_name='bc_weekly_score'`,
     `status='running'`, `snapshot_date=today`, `started_at=now()`, capturing `run_started_at` for the
     `scored_at` stamp (§3.3).
  2. **Reads the universe:** `DISTINCT ON (application_number) … ORDER BY snapshot_date DESC, built_at DESC`
     from `bc_application_features` where `pdufa_date IS NOT NULL` and `appl_type IN ('NDA','BLA')` (the
     Phase-0 output: `{application_number, sponsor_cik, sponsor_name, appl_type, pdufa_date, ticker, cik,
     is_biosimilar_bla, designations}`). If empty → close `status='succeeded', n_processed=0,
     reason='empty_universe'`, return.
  3. **Substrate pull (§1.3a):** ensure `fda_application_submissions` is populated for these appnos + their
     sponsors (targeted Drugs@FDA pull via the existing writer).
  4. For each app: compute `ref_date` (§3.1) → `build_features_pit(...)` → upsert `bc_application_features`
     (today's snapshot, M14 cols + `feature_quality`) → `score_nda(feature_dict)` → upsert `bc_rubric_scores`
     (band + locked-2025 percentile + `p_crl` internal) with `features_id` linking the feature row.
  5. Tally `n_processed`, `n_failed`, per-name outcomes + the within-snapshot ordering in `log` (jsonb);
     refresh the matview.
  6. Close the run `status = 'succeeded' | 'partial'` (`partial` if `n_failed>0` or the matview refresh
     lagged) in a `finally` (`failed` + `reason=<exception>` on uncaught throw).
- **Modal function (`modal_workers/app.py`):**
  ```python
  @app.function(image=image, timeout=600, secrets=[scanner_secrets, supabase_secrets])
  def bc_weekly_score_once() -> dict:
      from modal_workers.bc_score.run_weekly import run_weekly
      from modal_workers.shared.supabase_client import SupabaseClient
      return run_weekly(SupabaseClient(), apply=True)
  ```
  `scanner_secrets` carries `OPENFDA_API_KEY` + `SEC_USER_AGENT` (for the 8-K EFTS count) + `POLYGON_API_KEY`
  (carried but **unused** in v1 — band-only, §9); `supabase_secrets` the service-role write creds. (Confirm
  `scanner_secrets` carries `OPENFDA_API_KEY`; it already carries `POLYGON_API_KEY`/`SEC_USER_AGENT` per
  Phase 0 §5.2.)

### 2.2 Scheduling — weekly, AFTER the Phase-0 universe build; reuse `dispatch_weekly` (5-cron cap)

> **5-cron cap:** Modal's plan allows 5 scheduled functions and the app is at its cron budget. **Do NOT add
> a new `@modal.Cron`.** Fold `bc_weekly_score` into the existing **`dispatch_weekly`** dispatcher.

- **(a) Registry-driven (preferred — gives the runtime kill switch).** **INSERT a `public.scanners` row**
  `name='bc_weekly_score', cadence='weekly', status='operational'` so `_load_cadence_names('weekly', …)` in
  `dispatch_weekly` (Sun 12 UTC) picks it up via `load_operational_names_by_cadence('weekly')` → spawns
  `bc_weekly_score_once`. **Retiming/pausing is then a DB UPDATE** (`scanner_registry_vs_db.md`: the DB row
  is authoritative). Suggested row fields: `default_scoring_profile='fda_event'` (placeholder — it is **not**
  a signal emitter), `signal_type_profile_map='{}'`, `endpoints='{}'`, `config='{}'`,
  `timeout_soft_s=300, timeout_hard_s=600`. (`scheduled_hour_utc` irrelevant for weekly — `dispatch_weekly`
  routes by cadence.)
- **(b) Code-level fallback:** if a `scanners` row is undesirable (Phase 1 is a *scorer*, not a fetcher),
  call `run_weekly(apply=True)` directly inside `dispatch_weekly` after `_dispatch(...)`, like the folded
  `reporting_weekly` invocation already there. Avoids registry semantics but loses the DB pause switch.
  **Recommend (a).**
- **Ordering:** `dispatch_weekly` runs Sun 12 UTC. Phase 0's universe cron is **daily** (Phase 0 §5.2, hour
  11 UTC), so by Sunday the universe is fresh — no explicit dependency wiring needed; Phase 1 reads whatever
  Phase 0 last wrote. (If Phase 0 ends up weekly too, ensure its bucket precedes Phase 1's, or sequence them
  in one cron.)

### 2.3 Completion gating on the universe feed

- The worker is **completion-gated**: it only scores applications present in `bc_application_features` with a
  non-NULL `pdufa_date`. An empty/partial Phase-0 feed yields `n_processed` = whatever exists; the run is
  honest-empty, never fabricates names.
- **If Phase 1 finds no `pdufa_date` row for *today*** (Phase 0 hasn't run on this snapshot), it scores the
  **latest** snapshot's rows and **writes its feature row on that same latest `snapshot_date`** (never
  invents a new snapshot) so the matview's DISTINCT-ON stays coherent. Record `scored_snapshot_date` in the
  run log.
- **Fail-loud:** the `bc_pipeline_runs` row is opened in a `try` and closed in `finally` (`failed` +
  `reason=<exception>` on uncaught throw) — the anti-pattern from `dispatch_observability_silent_swallow` /
  `cowork_session_halt`. Per-name failures are caught, logged, counted in `n_failed`, and do not abort the
  remaining names (`status='partial'`).

---

## 3. THE WRITE CONTRACT

### 3.1 `ref_date` per application (no look-ahead)

- `ref_date` = the application's **ORIG submission/filing date** (start of the review cycle), **NOT** the
  PDUFA date and **NOT** today. Sourced from the ORIG submission's `submission_status_date` in
  `fda_application_submissions` (the substrate pull). This is the point-in-time anchor `feature_assembly`
  enforces (`ref`; all source queries use `lt./lte./gte.` on `ref`), so the 8-K window `[ref−180, ref−30]`
  and sponsor-history `< ref` are leak-free. Using `pdufa_date` (the future event) would leak the outcome
  window.
- **Fallback chain:** ORIG `submission_status_date` → else `pdufa_date − 304 days` (standard ~10-month review
  clock; set `ref_date_estimated=true` in `_provenance`/`log`) → else `submission_date` from the Phase-0
  feature row if present. Record which was used per name.
- **Runnable assertion:** the builder asserts every source-row date used for a feature is `<= ref_date`
  (warnings/priority/class) or within the 8-K window (`<= ref−30`); **raise on violation** (mirrors the A0
  no-look-ahead check, `bc_v4_a0_cohort_confidence.md` §3.3). The builder is handed **only**
  `(application_number, sponsor_cik, sponsor_name, ticker, cik, ref_date, designations)` — **never** any
  future/outcome field — so look-ahead is structurally impossible.
- Persist the chosen `ref_date` to `bc_application_features.as_of_date` ("features as-of this date") and
  `submission_date` when known.

### 3.2 `bc_application_features` upsert (M14 columns onto Phase-0's snapshot row)

```
on_conflict = sponsor_cik,application_number,snapshot_date            # the live UNIQUE (= Phase 0's merge key)
prefer      = resolution=merge-duplicates,return=representation       # need the returned id for features_id FK
row = {
  # identity — MUST match Phase 0's row so merge-duplicates UPDATES (not a twin INSERT):
  sponsor_cik, sponsor_name, application_number, appl_type,           # NOT NULL — from Phase-0 row + appno prefix
  snapshot_date:     <today | scored_snapshot_date>,                  # NOT NULL — the merge key (§2.3)
  as_of_date:        ref_date,                                        # NOT NULL (point-in-time anchor)
  built_at:          now(),                                           # NOT NULL (bumped so latest_features picks this row)
  cycle_type:        'first_cycle_orig',                              # NOT NULL (v1 constant)
  is_biosimilar_bla: <carry Phase-0 value or false>,                  # NOT NULL
  pdufa_date:        <carry forward from latest Phase-0 feature row>, # DO NOT blank — matview G3 needs it
  has_bt, has_ft, has_aa: <carry Phase-0 designations; NULL not False when unknown>,
  # M14 feature columns Phase 1 fills (NULL-not-False / omit-not-0 when absent):
  review_priority:           'PRIORITY'|'STANDARD'|None,              # from substrate ORIG row
  submission_class_code:     <str>|None,                              # from substrate ORIG row
  n_prior_filings:           <int>|None,                              # None → column default 0
  n_8ks_30_180_clean:        <int>|None,                              # None → default 0
  n_drug_inspections_5y_fix: None,                                    # no live source → NULL (default 0)
  sponsor_has_warning:       <bool>|None,                             # NULL not False when unsourced
  sponsor_has_orphan_history:None, ctgov_failed_primary:None, ctgov_any_randomized:None,  # no source
  submission_date:   <ORIG date>|None,
  feature_quality:   'standard' | 'low',                             # 'low' if _coverage < COVERAGE_FLOOR (§6); CHECK-allowed
  required_feature_missing_count: <count of kept keys defaulted>,
}
```
Capture the returned `id` (from `return=representation`) to set `bc_rubric_scores.features_id`.

### 3.3 `bc_rubric_scores` upsert (band + locked-2025 percentile; p_crl internal)

```
scored_at   = run_started_at                                         # SAME timestamp for all names in the run → idempotent within-week
on_conflict = application_number,scored_at,scorer_name               # the live UNIQUE
prefer      = resolution=merge-duplicates,return=minimal
row = {
  application_number,
  scorer_name:    'M14_adjusted',                                    # MUST match matview literal (§0.5)
  scorer_version: <score['model_version'] = NDA_MODEL_VERSION>,      # rename model_version → scorer_version
  p_crl:              <float(score['p_crl']) or NULL on refusal>,    # PERSISTED (matview G1 reads it) — never DISPLAYED
  raw_p_uncalibrated: <float or NULL>,
  ci_low, ci_high:    <float or NULL>,
  risk_band:          <score['risk_band'] or NULL>,                  # the displayed rank tier
  oof_percentile_rank:<to_percentile(p_crl, nda_locked2025_ref) or NULL>,  # §0.6 — locked-2025 reference percentile (0..100)
  confidence_flag:    <score['confidence_flag'] verbatim>,           # standard | low_confidence_sponsor | …
  refusal_reason:     <score['refusal_reason'] or NULL>,
  features_id:        <the bc_application_features.id from §3.2>,
}
```
**NULL-not-False / placeholders:** refused rows → `p_crl/raw/ci/risk_band/oof_percentile_rank = NULL`,
`refusal_reason` set. Never write `0`/`false` for "unknown."

**Refusal handling:** if the scorer refuses (should be rare in v1 — the universe is first-cycle-original
non-biosimilar by construction), write the row with `p_crl=NULL, risk_band=NULL, refusal_reason=<reason>`.
The matview tiers it `refused` (it falls out of the candidate set, by design). **A refusal here is a
cohort/Phase-0 bug** (a supplement or biosimilar leaked into the universe); log it loudly and count it (it
feeds the §4 refusal-rate guard) — never silently dropped.

### 3.4 Matview refresh (else `bc_candidates` is stale)

`bc_candidates` is a **MATERIALIZED** view (`relkind='m'`) — Phase 1 must refresh it after all writes, else
the band/rank never surfaces. Call **`bc_refresh_candidates()`** (the `SECURITY DEFINER` RPC from
`002_bc_candidates_view.sql:84`) once at the end. It snapshots prior tiers, `REFRESH … CONCURRENTLY`
(advisory-locked — safe vs the hourly/04:00 callers), and diffs into `bc_candidate_transitions`.

> **Caveat:** `CONCURRENTLY` requires a UNIQUE index on the matview — **verify one exists** (the RPC ships in
> `002_bc_candidates_view.sql`; confirm whether the matview has the unique index, else the RPC must fall back
> to a plain `REFRESH`, a brief lock; the table is tiny). The RPC is invoked rather than issuing raw DDL over
> PostgREST. Record `matview_refreshed` in the run log; a refresh failure → `status='partial'` (scores are
> written; only the view lagged), surfaced loudly.

New scores then surface as `tier IN ('active','watchlist')` where G2 (tradeable) + G3 (in-window) also pass;
names failing G2/G3 sit at `gate2_failed`/`watchlist` — expected.

---

## 4. FAIL-LOUD + LIVENESS + (optional) refusal-rate guard

- **Liveness = "did this week's run write (and close `succeeded`) its `bc_pipeline_runs` row?"** No watchdog
  meta-system. The row is opened `running` and always closed (`succeeded`/`partial`/`failed`) in a `finally`.
- **`log` jsonb** carries per-name outcomes: `{appno: {scored: bool, risk_band, p_crl_internal, coverage,
  ref_date_source, refusal_reason?}}` + run-level `{n_in_universe, n_scored, n_refused, n_low_coverage,
  coverage_hist, within_snapshot_rank (§0.6/§9), rank_method, feature_provenance_summary,
  substrate_appnos_pulled, scored_snapshot_date, matview_refreshed, builder_option:'A_point_in_time'}`.
- **Refusal-rate guard (only after migration 005 lands):** if `n_refused / n_in_universe` exceeds a threshold
  (e.g. >10%), non-first-cycle/biosimilar names are leaking from Phase 0. **Pre-flight the live
  `operator_flags_source_check`** (re-introspect, per `migration_drift_sweep` discipline); if
  `bc_l2_refusal_spike` is allowed, raise `operator_flags(source='bc_l2_refusal_spike', severity='warn')`;
  else route the warning to `bc_pipeline_runs.log` (don't crash for a flag-sink gap). **Phase 1 does NOT
  require 005**; it ships using `bc_pipeline_runs` alone.

---

## 5. THE REUSABLE FAIL-LOUD HELPER

`modal_workers/shared/bc_pipeline_runs.py` — establish the open/close helper now (Phase 0/2/3 adopt it):
```python
open_run(client, *, pipeline_name, snapshot_date) -> run_id      # POST, return=representation → id; status='running'
close_run(client, run_id, *, status, n_processed, n_failed, cost_usd=0, log, reason=None)  # status ∈ {succeeded,partial,failed}
```
`cost_usd=0` always (no LLM on this path). `open_run` in a `try`, `close_run` in a `finally` so liveness
survives a crash.

---

## 6. COVERAGE FLOOR + RANK-DISPLAY GATING

Missing app_numbers → thin coverage **weakens the rank but does NOT block scoring** (the scorer degrades
gracefully). Implement:
- Per-row `_coverage` (from the builder) → persisted as `feature_quality` (`'low'` if `_coverage <
  COVERAGE_FLOOR`, else `'standard'`) + `required_feature_missing_count`.
- **`COVERAGE_FLOOR` default 0.5** (≥5 of 10 `_NDA_COVERAGE_KEYS` present). Below it, the row is still scored
  and stored, but flagged `feature_quality='low'` so the digest/dashboard can **de-emphasize its rank** (show
  with a caveat or drop from the headline ordering) — consistent with A0's display tiers. A module constant
  (promote to `bc_config` `l3.coverage_floor` only if Pedro wants runtime tuning).
- The run log emits a `coverage_hist` (buckets of `_coverage`) so a sudden coverage collapse (e.g. Drugs@FDA
  outage) is visible in `bc_pipeline_runs.log` without a separate alarm.
- Low-coverage rows lean on the model intercept and compress toward the base rate (a known band limitation,
  not a bug) — note this in the run log + the Phase-1 exit writeup.

---

## 7. TEST PLAN

All under `modal_workers/tests/` (pytest, repo convention). **No live network/DB** — fake the Supabase
client (reuse the param-aware `FakeClient` pattern from `modal_workers/tests/test_fda_crl_feature_assembly.py`,
which applies top-level `eq.` filters and ignores `select/order/and/or`).

1. **Scorer fidelity (regression alarm on the vendored model):** `score_nda` over
   `modal_workers/shared/fda_crl/testdata/example_input.csv` reproduces `example_output.csv` to the 8-decimal
   strings, and the `supplemental` row → `confidence_flag='refused'` (already covered by
   `test_fda_crl_scorers.py` — depend on it; do not duplicate). Proves the vendored model is intact before
   trusting it on the universe.
2. **Feature-parity (the load-bearing test):** `feature_builder_pit.build_features_pit(...)` output (keys +
   values + `_coverage`) **== `feature_assembly.assemble_nda_features(...)`** on a shared fixture asset where
   both inputs are available (a populated `fda_application_submissions` + an asset carrying both an
   `entity_id` for `assemble_nda_features` and a CIK for the PIT builder), via the same client stub. **This
   proves the live weekly features match the A0 OOS features**, so A0's AUC/CI transfers. Import the
   windows/coverage-keys constants from `feature_assembly` (no re-declaration).
3. **No-look-ahead:** inject a synthetic `fda_application_submissions` / 8-K row dated `ref_date + 1d`;
   assert the PIT builder excludes it (submissions `< ref`, 8-K `[ref−180, ref−30]`) AND raises if any used
   source-row date `> ref_date`. Assert the builder signature is never handed a future/outcome field.
4. **Scorer-input-key test:** assert every key the builder emits is one `score_row` recognizes (guards
   against alias drift between builder and vendored scorer).
5. **Write-contract (fake client):** assert upsert bodies + `on_conflict` targets +
   `scorer_name='M14_adjusted'` + `model_version→scorer_version` rename + `float()` casts + `scored_at ==
   run_started_at` (idempotency stamp) + `features_id` linkage + `pdufa_date` carried forward (not blanked) +
   NOT-NULL identity fields present + NULL-not-False for absent designations/warning + the
   `bc_application_features` upsert uses `on_conflict=sponsor_cik,application_number,snapshot_date`.
6. **`confidence_flag` CHECK-conformance:** generate the four scorer flag combinations (`standard`;
   `low_confidence_sponsor`; `low_confidence_sponsor;moderate_confidence_no_edgar_signal`;
   `…;probability_extrapolation`) and assert each matches the deployed regex (copy the §0.5 regex into the
   test).
7. **Refusal path:** a feature dict with `cycle_type='supplemental'` (or `is_biosimilar_bla=1`) ⇒ the worker
   writes a `bc_rubric_scores` row with `refusal_reason` set, `p_crl=NULL`, increments `n_refused`; assert it
   does NOT abort the run.
8. **Missing-appno degradation:** a universe row with a surrogate `EDGAR8K:<cik>:<slug>` appno ⇒ substrate
   features fall to default, `feature_quality='low'`, the row still scores (not dropped), a `coverage` entry
   lands in `log`.
9. **Empty universe:** no `bc_application_features` rows with non-NULL `pdufa_date` ⇒ run closes
   `status='succeeded', n_processed=0, reason='empty_universe'` (honest-empty, not failed).
10. **Fail-loud on crash:** force an exception mid-run (monkeypatch the builder to raise on row 2) ⇒
    `status='partial'`, row-1 score persisted, `n_failed≥1`, and the `bc_pipeline_runs` row still closes
    (`finally` stamps `finished_at`).
11. **Percentile reference:** `to_percentile(p_crl, nda_locked2025_ref)` on a known `p_crl` reproduces the
    expected percentile vs the vendored reference CSV (and is monotone: higher `p_crl` → higher percentile).
12. **Idempotency (live, gated; tiny `--limit 3`):** `--apply` once, snapshot counts; `--apply` again same
    `run_started_at`-week ⇒ no dup `bc_application_features` rows (composite UNIQUE) and `bc_rubric_scores`
    merged (UNIQUE on `application_number,scored_at,scorer_name`); a new week's timestamp ADDS history.
13. **End-to-end dry-run (live read-only, after Phase 0):** `run_weekly(apply=False)` scores every
    `pdufa_date` row, prints per-row band + coverage, computes the within-snapshot ordering; no DB writes.
14. **Matview-visibility (live, post-apply):** after `--apply` + refresh, query `bc_candidates`; assert
    scored rows show non-NULL `risk_band` + `p_crl` and a tier other than `gate1_failed` where `p_crl <=
    tau_nda_watchlist`.

---

## 8. RISKS & MITIGATIONS

1. **Substrate pull misses pending appnos (Drugs@FDA is approved-products-skewed).** A *pending* NDA/BLA may
   have no Drugs@FDA submission record yet → `priority`/`class`/`n_prior_filings` absent → base-rate rank.
   *Mitigation:* **expected, non-blocking degradation** (the scorer defaults gracefully); coverage flag +
   `confidence_flag='low_confidence_sponsor'` surface it; `is_bla` (dominant) and `n_8ks_30_180_clean` are
   still sourceable. A0 quantifies how much the rank weakens at low coverage.
2. **8-K count has no clean live link.** `documents` lacks `entity_id` (~424 rows, CIK in
   `extensions->'ciks'`); sponsor→8-K linkage is fragile for small-caps. *Mitigation:* count by **CIK via
   EFTS** (§1.3b, reuse Phase 0's EDGAR path), not the `documents` corpus; leave ABSENT (not 0) when no CIK
   resolves → scorer flags `moderate_confidence_no_edgar_signal`.
3. **`scorer_name` literal mismatch silently drops scores from the matview.** *Mitigation:* the write
   contract pins `'M14_adjusted'` (§3.3) and the write-contract test asserts it; a post-run check that
   `bc_candidates` row-count > 0 (when universe non-empty) catches a regression.
4. **`p_crl` leaking to the display surface** (violates band-only). *Mitigation:* Phase 1 *stores* `p_crl`
   (matview needs it) but the Phase 2/3/4 display contracts render only `risk_band`+percentile; documented
   here and in the Phase-2 synthesis contract (§1.3 there). Add a note in the digest/dashboard plans: **never
   render `bc_rubric_scores.p_crl`**, never an options-derived implied move (band-only v1, §9).
5. **Phase-0/Phase-1 same-day feature-row collision** on `(sponsor_cik, application_number, snapshot_date)`.
   *Mitigation:* Phase 1 carries Phase-0's `pdufa_date`/designations into its own snapshot upsert (newest
   `built_at` wins); never blanks `pdufa_date`; scores on the **scored snapshot's** `snapshot_date` (§2.3).
   Test #5 asserts carry-forward.
6. **`bc_rubric_scores` unbounded growth** (`scored_at` in the UNIQUE). *Mitigation:* single per-run
   timestamp (§3.3) → one row per app per week, idempotent within the week.
7. **A0/Phase-1 builder divergence** (two copies of the PIT builder). *Mitigation:* **one shared module**
   (`feature_builder_pit.py`); whichever phase ships first owns it, the other re-exports; the parity test
   (#2) pins it to `feature_assembly`'s definitions; import the windows/coverage-keys constants.
8. **`feature_quality` CHECK drift with Phase 0 — RESOLVED 2026-06-03 (Option A).** An earlier Phase-0 draft's
   `'phase0_*'` tokens (and an earlier Phase-1 draft's `'phase1_scored*'` tokens) violated the live CHECK ∈
   {standard,low,built_at_install}. *Resolution:* both phases write only `'standard'`/`'low'`; surrogate
   provenance lives on the `EDGAR8K:` appno prefix (§0.4).
9. **Matview not refreshed → product shows stale/empty bands.** *Mitigation:* explicit `bc_refresh_candidates()`
   step (§3.4) with `matview_refreshed` logged; refresh failure → `status='partial'` (loud), not swallowed;
   verify the matview UNIQUE index for `CONCURRENTLY`.
10. **openFDA 1,000/day shared-IP cap** if the per-sponsor Drugs@FDA fan-out grows. *Mitigation:* weekly
    cadence + ~20-name universe = trivial volume; ensure `OPENFDA_API_KEY` is set (`openfda_auth_params`);
    cache per-sponsor submission pulls within a run (`openfda_rate_limit_gap.md`).
11. **Scorer model swap silently changes scores.** *Mitigation:* `scorer_version` stamped on every score
    row; the fidelity test (#1) pins the vendored artifact; a re-vendor would surface the delta.
12. **Migration `20260615000000` is a DDL change to a *shared* schema.** *Mitigation:* `CREATE TABLE IF NOT
    EXISTS` for a *new* table with no dependents — additive and safe; apply disk-first via `db push`; the
    writer already targets it; re-introspect after apply.
13. **Weekly cron lands inside the 5-cron-capped app.** *Mitigation:* fold into `dispatch_weekly` (no new
    `@modal.Cron`); registry row gives the pause switch (§2.2).
14. **Refusal-flag write rejected (005 unapplied).** *Mitigation:* Phase 1's liveness is `bc_pipeline_runs`
    only; the refusal `operator_flags` write is opportunistic behind a live-CHECK pre-flight, with a
    `bc_pipeline_runs.log` fallback — never crashes the scorer (§4).

---

## 9. RECONCILIATION NOTES (merge of `bc_v4_phase1_live_score.md` + `bc_v4_phase1_live_scorer.md`, 2026-06-04)

This file is the **single canonical Phase-1 detail plan**; the former `…_live_scorer.md` was deleted. Where
the two source drafts genuinely conflicted, the resolution (with rationale) is:

1. **The scorer (vendored vs "vendor from `~/Downloads`") — RESOLVED via authoritative correction.** The
   `…_live_score.md` draft was correct: the M14 scorer is **already vendored**
   (`modal_workers/shared/fda_crl/nda_scorer.py`, model JSON byte-identical, verified). The `…_live_scorer.md`
   draft's §0.1/§3.1 ("scorer lives ONLY in `~/Downloads`, must be vendored", + new files
   `score_m14_adjusted.py` / `m14_adjusted_model.json` / a `test_score_m14_vendored.py` golden test) is
   **superseded** — Phase 1 **imports** `score_nda`; the only data artifact it vendors is the percentile
   reference CSV (point 4). Avoids a two-model-JSON drift hazard.

2. **`feature_quality` token values — HARD CONFLICT, RESOLVED to the live CHECK.** `…_live_score.md` wrote
   `'phase1_scored'` / `'phase1_scored_low_coverage'`; `…_live_scorer.md` verified the live CHECK ∈
   **{standard, low, built_at_install}** and wrote `'standard'`/`'low'`. The `…_live_scorer.md` values are
   correct (the `phase1_scored*` tokens would be **rejected** by the CHECK). Canonical: **`'standard'`/`'low'`,
   re-derived from coverage** (§0.4, §6, §3.2). This also aligns with the Phase-0 drift resolution (Option A,
   surrogate provenance on the `EDGAR8K:` appno prefix).

3. **`bc_pipeline_runs.status` values — HARD CONFLICT, RESOLVED to the authoritative correction.** BOTH
   drafts used `{running, ok, partial, error}` (and `…_live_score.md` §0.7 even claimed "No CHECK on
   status"). The authoritative correction pins the CHECK = **{running, succeeded, partial, failed}**.
   Canonical uses **`succeeded`/`partial`/`failed`** throughout (§0.7, §2.1, §2.3, §4, §5, §7). Honest-empty
   runs close **`succeeded`** (not `ok`).

4. **`oof_percentile_rank` definition — HARD CONFLICT, RESOLVED to the locked-2025 reference for the stored
   column.** `…_live_score.md` §0.6 = percentile of `p_crl` within the **locked-2025 M14 calibrated
   distribution** (`p_m14_cal`, vendored CSV). `…_live_scorer.md` §4.3 = **within-snapshot** rank (percentile
   among the ~20 names scored this run). The merge brief explicitly names "the oof_percentile NDA reference
   (locked_2025 p_m14_cal)", and a stored column should be **reproducible run-over-run** (a within-snapshot
   percentile shifts as the universe changes). **Resolution:** the **persisted `oof_percentile_rank` = the
   locked-2025 reference percentile** (§0.6, §3.3); the **within-snapshot ordering** the digest sorts on is
   **retained but moved to the run log** (`bc_pipeline_runs.log.within_snapshot_rank`, §4). Both needs are
   met without overloading one column. **Flag for Pedro/engineer:** if the digest must sort on a *persisted*
   field rather than the run log, revisit — but do not redefine the stored column to a non-reproducible
   value.

5. **PIT feature-builder module path — SOFT CONFLICT, RESOLVED to the Modal-importable shared path.**
   `…_live_score.md` → `modal_workers/shared/fda_crl/feature_builder_pit.py`; `…_live_scorer.md` →
   `analysis/bc_features/point_in_time_builder.py`. Canonical = **`modal_workers/shared/fda_crl/
   feature_builder_pit.py`** because the weekly cron runs in a Modal container that ships `modal_workers/`,
   and A0 can import from there. A0's `analysis/bc_a0/feature_builder.py` becomes a thin re-export. One
   module, one parity test (§1.2, §7.2). (Builder fn name: `build_features_pit`.)

6. **Worker file/name — SOFT CONFLICT, RESOLVED to one consistent name.** `…_live_score.md` →
   `modal_workers/bc_score/run_weekly.py`, `pipeline_name='bc_weekly_score'`, scanners
   `name='bc_weekly_score'`. `…_live_scorer.md` → `modal_workers/scanners/bc_weekly_scorer.py`,
   `pipeline_name='bc_weekly_scorer'`, scanners `bc_weekly_scorer`. Canonical = **`modal_workers/bc_score/
   run_weekly.py`** with **`bc_weekly_score`** as the single pipeline/Modal-function/scanners name
   throughout (§2, §5). The `…_live_scorer.md` `--apply/--limit/--snapshot-date/--application-number` CLI
   dry-run-default exemplar and the reusable `bc_pipeline_runs.py` helper are **kept** (§2.1, §5).

7. **Adopted from `…_live_scorer.md` (genuinely better, merged in):** the live `feature_quality` CHECK + the
   Phase-0 drift resolution (point 2); the explicit scorer-dict→DB-column mapping table (§0.5); the
   `confidence_flag` regex CHECK + conformance test (§0.5, §7.6); the reusable `bc_pipeline_runs.py` open/close
   helper (§5); the CLI/dry-run exemplar and `bc_class_precedent_refresher` reference; the
   `NotImplementedError` fact for the inspections fetcher (§0.3, §1.1); richer reuse-anchor line numbers; the
   `documents`-table 8-K rationale stated as a hard "use EFTS-by-CIK" decision. **Adopted from
   `…_live_score.md` (kept as the spine):** the overall section structure, the §0.2 input-key/coef table, the
   §1 feature-set ablation + drop justifications, the substrate "apply migration + targeted pull" framing, the
   §3 write-contract pseudocode, the §8 build order, the band-only product framing.

8. **Polygon options / band-only (Pedro 2026-06-03).** Neither draft contradicted band-only; the decision is
   stated up front and in §8.4. **Note:** the Polygon options code is at
   `modal_workers/providers/polygon/options_data.py` (NOT `modal_workers/shared/providers/polygon/…` as one
   correction wording suggested — verified path). Live key 403s on the options entitlement → **out of v1
   scope**; market-implied-move framing deferred to v1.1; synthesis `recommended_action` capped at `monitor`.
   `POLYGON_API_KEY` is carried in `scanner_secrets` but **unused** on this path.

---

## 10. BUILD ORDER (engineer can start immediately; exit-gate needs Phase 0's universe live)

1. **Apply** `supabase/migrations/20260615000000_fda_application_submissions.sql` (`supabase db push`);
   confirm `to_regclass('public.fda_application_submissions')` is non-NULL live. (No Phase-0 dependency.)
2. **Vendor the percentile reference CSV** (§0.6) into `modal_workers/shared/fda_crl/models/
   nda_m14_locked2025_reference.csv` (the `p_m14_cal` column). (No Phase-0 dependency.)
3. **Write/promote `feature_builder_pit.py`** (shared with A0) + its parity + no-look-ahead + input-key tests
   (#2/#3/#4). Pin definitions to `feature_assembly` (import its constants). (Testable on fixtures; no Phase-0
   dependency.)
4. **Write `modal_workers/shared/bc_pipeline_runs.py`** open/close helper (§5) + unit test.
5. **Write `modal_workers/bc_score/substrate.py`** — the targeted Drugs@FDA pull reusing
   `extract_submission_rows` / `_upsert_application_submissions` (§1.3a).
6. **Write `modal_workers/bc_score/run_weekly.py`** (read universe → substrate → PIT features → `score_nda`
   → upsert `bc_application_features` + `bc_rubric_scores` → `bc_refresh_candidates()` → close
   `bc_pipeline_runs`), `_cli` with `--apply` (dry-run default), + the write-contract / confidence-flag /
   refusal / missing-appno / empty-universe / fail-loud tests (#5–#10) and the percentile test (#11).
   (Verify the `bc_candidates` matview UNIQUE index for `CONCURRENTLY`.)
7. **Wire `app.py`** `bc_weekly_score_once` + INSERT the `public.scanners` `bc_weekly_score` weekly row
   (§2.1/§2.2).
8. **Dry-run live** (after Phase 0): `run_weekly(apply=False)` → **`--apply --limit 3`** → verify
   `bc_candidates` shows bands → full `--apply` → confirm the Sun-12-UTC `dispatch_weekly` run closes a
   `bc_pipeline_runs` row `succeeded`. **Exit gate (high-level plan P1):** scores present for the whole
   universe; `bc_application_features` has `feature_quality∈{standard,low}` snapshot rows; `bc_rubric_scores`
   has `scorer_name='M14_adjusted'` rows with `risk_band` + `oof_percentile_rank` populated and `p_crl`
   present; `bc_candidates` lights up `tier IN ('active','watchlist')` where G2/G3 pass; band thresholds
   verified against a hand-computed example.

### Reuse anchors (do NOT modify)

`modal_workers/shared/fda_crl/{nda_scorer.py (score_nda :206, NDA_MODEL_VERSION :25, load_model :42),
percentile.py (to_percentile :39), feature_assembly.py (assemble_nda_features :303, appl_is_bla :82,
_orig_submission :199, _n_prior_filings :213, _n_inspections_5y :235, _sponsor_has_warning :256,
_n_8ks_30_180 :274, _NDA_COVERAGE_KEYS :50, window consts :44-46), __init__.py}`;
`modal_workers/ingestion/openfda_ingest.py (extract_submission_rows :288, ingest_drugsfda_approvals :119,
_upsert_application_submissions :331)`; `modal_workers/shared/openfda_client.py (openfda_get :101,
openfda_auth_params)`; `modal_workers/shared/edgar_efts.py (efts_search :32)`;
`modal_workers/shared/sponsor_resolver.py (resolve_sponsor)`; `modal_workers/shared/supabase_client.py
(_rest_with_retry :145, upsert idiom, load_operational_names_by_cadence :247)`;
`modal_workers/scripts/bc_class_precedent_refresher.py (CLI + upsert + dry-run exemplar :202-278)`;
`modal_workers/app.py (dispatch_weekly :906, _dispatch :801, _load_cadence_names :747)`;
`002_bc_candidates_view.sql (bc_refresh_candidates() :84, scorer_name 'M14_adjusted' :29, p_crl gate :52)`.

> **Sequencing reminder:** Phase 1 is on the critical path **after** the Phase-0 GATE
> (`0 → 1(live) → 2 → 3`). It can be *built* in parallel (against an empty/seeded universe), but its
> exit-gate verification needs Phase 0's universe live. A0 (the offline cohort) runs independently and sets
> only the *display prominence* of the band — it is **not** a gate for shipping this weekly scorer.
