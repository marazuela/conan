# A0 — Score-cohort confidence build (rank-confidence note)

> **Workstream:** Light v4 (BC-FDA monitor), Phase 1 parallel track **A0**.
> **Status:** NON-BLOCKING, NOT a gate. The CRL score is a **ranking input** (risk-band), not a
> calibrated probability. A0 only calibrates **how prominently the band is shown in the product** —
> show-prominently / show-with-caveat / de-emphasize.
> **Why this track first:** it is the most immediately executable work in the whole plan because the
> data is already verified live (FDA CRL Transparency dump) and the scorer is pure-stdlib and importable
> as-is. It depends on **nothing** in Phase 0 (universe/PDUFA) and can run start-to-finish offline.
> **Author of plan:** detail-planning agent, 2026-06-03. All counts below were computed live against the
> 2026-06-01 Transparency export and the M14 artifacts; re-verify if those change.

---

## 0. TL;DR for the implementer

Build a clean **out-of-sample (OOS)** validation cohort for the M14 adjusted CRL scorer and report its
**AUC + Brier + bootstrap 95% CI + calibration slope**, honestly headlining the **CI floor**. Then write a
one-page note that maps that CI floor to a product display decision for the risk-band.

Pipeline:

```
FDA CRL Transparency dump  ──►  staging (local parquet/CSV + optional bc_ mirror)
   (439 recs, 426 COMPLETE RESPONSE, 100% FDA-keyed)
        │  filter letter_type=COMPLETE RESPONSE, letter_year∈{2025,2026}
        │  exclude biosimilars, confirm first-cycle-original, exclude locked-2025 ApplNos
        ▼
   POSITIVE class  (expected ~35–45 clean OOS CRLs; 54 raw before filtering)
        +
   NEGATIVE class  (matched first-cycle-original NDA/BLA APPROVALS, same window)
        │           (seed: reuse the 33 prospective-2026 label-0 rows; extend via Drugs@FDA approvals)
        ▼
   point-in-time feature build  (as-of submission date, reuse feature_assembly OR offline builder)
        ▼
   M14 scorer (import score_m14_adjusted.score_row as-is)  ──►  p_crl per cohort member
        ▼
   metrics: ranking_auc + brier_score + bootstrap CI + calibration slope
        ▼
   rank-confidence note  ──►  product display decision
```

**Headline expectation (must be stated honestly in the note):** the locked-2025 CI floor is already
**0.637 < 0.70** on 9 positives; the OOS slice is similarly small, so the OOS CI **will be wide**. The note
must report the **floor**, not just the point estimate, and the product mapping must be driven by the floor.

---

## 1. Verified facts this plan is built on (do not re-derive)

All verified live on 2026-06-03 by the planning agent:

### 1.1 Cohort source (FDA CRL Transparency dump)
- Index: `https://api.fda.gov/download.json` → `results.transparency.crl`. Live: `export_date=2026-06-01`,
  `total_records=439`, **single partition**
  `https://download.open.fda.gov/transparency/crl/transparency-crl-0001-of-0001.json.zip` (~1.0 MB zipped,
  ~5.1 MB unzipped).
- Root JSON shape: `{"meta": {...}, "results": [ ...439 records ]}`.
- **Per-record fields** (verified):
  `letter_date` (MM/DD/YYYY), `letter_year` (string, e.g. `"2025"`), `letter_type`, `approval_status`
  (`Approved`/`Unapproved`), `file_name`, `application_number` (**list**, e.g. `["NDA 215344"]`,
  `["BLA 761385"]`), `company_name`, `company_rep`, `company_address`, `approver_name`, `approver_title`,
  `approver_center` (list), `text` (OCR body).
- `letter_type` distribution: **426 `COMPLETE RESPONSE`**, 5 null, 4 `TENTATIVE APPROVAL`, and one each of
  `REFUSAL TO FILE`, `PROVISIONAL DETERMINATION`, `RESCIND COMPLETE RESPONSE`,
  `CORRECTED PROVISIONAL DETERMINATION`. **Only `COMPLETE RESPONSE` is the positive class.**
- `letter_year` by year: 2024=69, **2025=59, 2026=14** (COMPLETE-RESPONSE-only: 2025=59, 2026=14, total
  73 in-window).
- `application_number` is clean and present on **412/426** CR records; 11 CR records carry multiple appnos.

### 1.2 The two file_name-scheme gotchas (CRITICAL — the spec's first-cycle heuristic is wrong for the OOS slice)
- The spec said to read `Orig1s000` vs supplement from `file_name`. **That token DOES NOT EXIST in the
  2025–26 slice.** Verified scheme shift:
  - 2018–2023 CR: 192/205 carry `Orig1s000`, 6 use `CRL_*`.
  - 2024 CR: **mixed** — 7 `Orig1s000`, 57 `CRL_*`.
  - **2025–2026 CR: 0 `Orig1s000`, 73/73 use `CRL_<TYPE><appno>_<YYYYMMDD>.pdf`** (e.g.
    `CRL_BLA761385_20250730.pdf`).
  - ⇒ **You cannot determine first-cycle-original from `file_name` for the OOS cohort.** Use Drugs@FDA
    submission lookup + text body instead (see §3.3).
- The `text` body is also a weak first-cycle signal: **72/73 CR texts contain "resubmit"** (boilerplate
  "If you decide to resubmit…"), so resubmission text-mining is unreliable. "ORIGINAL" appears in 59/73.

### 1.3 M14 splits + label reconciliation (the cohort is self-validating)
- M14 split columns (from `build_adjusted_m14.py`): `in_train_2018_2023`, `in_val_2024` (calibration),
  `in_test_2025` (**locked test**), `in_prospective_2026`. Label column = `CRL_label_strict`.
- Locked-2025 test artifact:
  `/Users/Pico/Downloads/BC_scoring_rubrics_export/NDA_M14_adjusted/data/locked_2025_predictions_m14_adjusted.csv`
  — **130 rows, 129 distinct ApplNo, 9 CRL positives** (`CRL_label_strict=1`). Reproduced locked AUC=0.809
  (model card: 0.8104), bootstrap 95% CI **[0.637, 0.954]**, Brier=0.0635.
- **Reconciliation (verified):** the 9 locked-2025 positives are EXACTLY the 9 Transparency CR appnos that
  fall in the locked set: `211241, 215244, 218571, 218879, 761211, 761427, 761440, 761451, 761458`.
  ⇒ The Transparency dump and the M14 labels agree on the 2025 positives. The cohort source is trustworthy.
- Prospective-2026 artifact: `data/prospective_2026_predictions_m14_adjusted.csv` — **33 rows, all
  `CRL_label_strict=0`**, all NDA/BLA first-cycle-original (the M14 build already scope-filtered them).
  **Zero collision** with Transparency 2026 CRLs and zero with 2025 CRLs ⇒ these 33 are clean, ready-built
  OOS NEGATIVES.

### 1.4 OOS cohort sizing (computed)
- Distinct CR appnos in 2025–26 (digit-normalized): **63** (NDA 36, BLA 27, ANDA 0).
- Overlap with locked-2025 ApplNos: **9** (= the 9 positives above).
- **OOS positives (CR 2025–26, digit-normalized appno NOT in locked CSV) = 54**
  (45 from 2025 + 11 distinct 2026 appnos). This matches the workstream's "~59 / ~40–55" estimate.
  - **Why 45 of 59 2025 CRLs are absent from the locked test** (the crux): the M14 build's
    `in_test_2025` set only carried 9 positives because the build filtered for first-cycle-original
    NDA/BLA and/or those CRLs were not yet posted at build time. So the 54 raw OOS positives still need the
    first-cycle + biosimilar filter (§3) before they are M14-scope; **expect ~35–45 to survive** (biosimilar
    text-flag hits 15/73 ≈ 21%, plus an unknown sNDA/resubmission fraction).
- `approval_status` for all 73 in-window CRs = `Unapproved` ⇒ **right-censored** post-CRL resolution
  (not needed for the CRL-vs-approval label; the label is "got a CRL", which the Transparency record IS).

### 1.5 Live infrastructure constraints (shape the whole build)
- **bc_ schema is live & empty:** `bc_applications=0`, `bc_application_features=0`, `bc_rubric_scores=0`,
  `bc_pipeline_runs=0`; `bc_config=11` seeds present (project `xvwvwbnxdsjpnealarkh`).
- **The feature source tables `feature_assembly.py` reads DO NOT all exist live.** Verified:
  `fda_application_submissions` → **MISSING**, `fda_drug_inspections` → **MISSING**, `fda_warning_letters`
  → **EXISTS** (cols: `letter_id, firm_name, firm_name_norm, issue_date, letter_url, issuing_office,
  subject, sponsor_ticker, source, refreshed_at`). `documents`/`entity_id` 8-K path depends on entity links
  that the cohort sponsors will not have.
  ⇒ **Running `assemble_nda_features` unchanged against the live DB would silently default n_prior_filings,
  inspections, priority, class-code, and 8-Ks to 0/absent** (the module degrades to `[]` on missing tables
  by design). This is the single most important adaptation decision (§3.2): A0 must **source the features
  itself**, not assume a populated DB.

### 1.6 The scorer (importable as-is)
- File: `/Users/Pico/Downloads/BC_scoring_rubrics_export/NDA_M14_adjusted/scripts/score_m14_adjusted.py`.
  Pure stdlib (`csv/json/math`). Entry points: `score_row(row: dict, model) -> dict` and
  `score_csv(in, out)`. Loads `model/m14_adjusted_model.json` relative to the script.
- **Refusal rules** (the ONLY two): `cycle_type != "first_cycle_orig"` → refused;
  `is_biosimilar_bla == 1` → refused. **All other missing features default to 0** (so the cohort filtering in
  §3 is what enforces scope, not the scorer).
- **Output keys:** `p_crl`, `raw_p_uncalibrated`, `ci_low`, `ci_high`, `risk_band`
  (`low<0.08≤moderate<0.15≤elevated<0.25≤high`), `confidence_flag`, `refusal_reason`, `model_version`.
- **Input contract** (verified from `examples/example_input.csv`), column names the scorer recognizes:
  `ApplNo, SponsorName, ApplType, ReviewPriority, SubmissionClassCode, cycle_type, is_biosimilar_bla,
  n_prior_filings, n_drug_inspections_5y_fix, n_8ks_30_180_clean` (plus aliases `has_bt/has_ft/has_aa`,
  `sponsor_has_warning`, `ctgov_*`). Model features (14):
  `n_prior_filings_log, priority, is_bla, type5_or_3, sponsor_has_orphan_history, sponsor_has_warning,
  n_drug_inspections_log, has_bt, has_ft, has_aa, n_8ks_30_180_clean, ctgov_failed_primary,
  ctgov_any_randomized`.
- **`feature_assembly.assemble_nda_features` output keys map 1:1 to the scorer input aliases** — confirmed
  by reading both. Its `ref_date` is the point-in-time guard (all source queries use `lt./lte./gte.` on
  `ref`). The three no-source features (`ctgov_failed_primary`, `ctgov_any_randomized`,
  `sponsor_has_orphan_history`, ~4.7% of |coef|) are intentionally absent → default 0.

### 1.7 Metrics reuse (precise — there is a scale trap)
- `orchestrator_runtime/eval_harness/metrics.py`:
  - **`ranking_auc(predictions, outcomes)`** — Mann–Whitney U, **scale-agnostic**, no sklearn. **USE AS-IS**
    for AUC (predictions = p_crl 0–1, outcomes = CRL label 0/1).
  - `calibration_curve(...)` and `aggregate(...)` assume **0–100 conviction scale** and
    `direction_correct` semantics → **NOT directly usable** for a 0–1 CRL probability. Do **not** route
    p_crl through them. (You may pass `[p*100]` buckets if you want a bucketed reliability table, but the
    headline calibration slope should come from the M14 method below.)
- `modal_workers/shared/fda_calibration_math.py`:
  - **`brier_score(predictions, outcomes)`** — 0–1 inputs, outcomes ∈ {0,1}. **USE AS-IS** for Brier.
  - `evaluate_guardrails(...)`, `bounded_drift(...)` — guardrail/CI helpers; **NOT applicable** here (those
    gate a refit; A0 is not a refit and not a gate). Mention but do not invoke.
- **Bootstrap CI + calibration slope** are NOT in the stdlib metrics module. The reference implementation is
  in `build_adjusted_m14.py`: `boot_ci(y, p, seed=123, n=2000)` (sklearn `roc_auc_score`),
  `cal_slope_intercept(y, p)` (sklearn `LogisticRegression` on `logit(p)`). **Reuse these two functions
  verbatim** (copy into the A0 module or import the script). They depend on numpy + sklearn, which the M14
  build env already has. This matches how the locked-2025 CI [0.637, 0.954] was produced, so the OOS CI is
  computed the identical way (apples-to-apples).

---

## 2. Cohort construction (exact, reproducible)

### 2.1 Ingest the Transparency dump → staging
1. Fetch the index `https://api.fda.gov/download.json`; read `results.transparency.crl.partitions[0].file`
   (do not hardcode the partition URL — read it, so a future re-partition does not break us). Capture
   `export_date` and record it in the run log (provenance).
2. Download the `.zip`, unzip, load `results` (list of 439). **Defensive parse:** glob can pick up stale
   files; load the exact unzipped filename, assert root has keys `{"meta","results"}` and
   `len(results) >= 400`, else fail loud.
3. Persist **raw** records to `data/a0/crl_transparency_raw_<export_date>.parquet` (and a `.json` copy) so
   the cohort is reproducible from a frozen snapshot even after FDA updates the dump.

### 2.2 Filter to the positive cohort
Apply in order, logging the surviving count at each step (the funnel is the auditable artifact):
1. `letter_type == "COMPLETE RESPONSE"` → 426.
2. `int(letter_year) in (2025, 2026)` → **73**.
3. Parse `application_number[]` with regex `^\s*(NDA|BLA|ANDA)\s*0*(\d+)`; keep `(appl_type, digits)`.
   Drop ANDA (none in-window) and any record with no parseable NDA/BLA appno. **De-dupe by
   digit-normalized appno** (strip prefix + leading zeros). One CRL letter = one cohort positive; for the
   11 multi-appno letters, emit one row per distinct appno but flag them (pseudo-replication risk, §6).
   → **63 distinct appnos**.
4. **Exclude locked-2025 ApplNos** (the OOS cut): load
   `locked_2025_predictions_m14_adjusted.csv`, digit-normalize `ApplNo`, set-subtract.
   → **54 raw OOS positive appnos**.
5. **Exclude biosimilars** (§2.4) and **confirm first-cycle-original** (§2.5). → **expected ~35–45**.

### 2.3 The NEGATIVE class (matched first-cycle-original NDA/BLA approvals, same window)
The label is **CRL vs no-CRL-by-the-decision** on first-cycle-original NDA/BLA. Negatives = applications
that reached a first-cycle action in 2025–26 and did **not** get a CRL (i.e. were approved, or are the
M14-build's labeled-0 originals).

**Two-tier sourcing (do tier A first; it is free and already scope-clean):**

- **Tier A — reuse the M14 prospective-2026 negatives (33 rows, verified clean).**
  `data/prospective_2026_predictions_m14_adjusted.csv` is 33 first-cycle-original NDA/BLA rows, all
  `CRL_label_strict=0`, **zero collision** with Transparency CRLs. These already have `ApplNo, SponsorName,
  ApplType, ReviewPriority` and M14-pipeline-built features (`p_m14_cal` is even precomputed for a sanity
  cross-check). **Use them as the 2026 negative spine.** (They are OOS w.r.t. the locked-2025 test because
  2026 ∉ test years.)

- **Tier B — pull 2025 first-cycle-original NDA/BLA approvals from Drugs@FDA** to balance the 2025
  positives. Use `ingest_drugsfda_approvals(since=2025-01-01, until=2026-06-01, ...)` semantics, but for A0
  pull **directly** (offline) via `openfda_get("drug/drugsfda.json", {"search": ...})`:
  - Search clause: `submissions.submission_status:"AP" AND submissions.submission_status_date:[20250101 TO
    20260601]` restricted to original submissions (`submissions.submission_type:"ORIG"`), NDA+BLA
    (application_number prefix). Page with `limit=100, skip=…` until short page (reuse the paging loop shape
    from `openfda_ingest._openfda_get`). Set `OPENFDA_API_KEY` if available to dodge the 1,000/day cap
    (`openfda_client.openfda_auth_params`).
  - For each approval record, keep the **ORIG submission** whose `submission_status_date` is the first-cycle
    action date; that date is the negative's `ref_date` (point-in-time anchor, §3).
  - **De-dupe** against the positive appnos and against Tier A.
- **Target ratio:** aim for **roughly balanced-to-2:1 negatives:positives** (≈ 70–90 negatives for ~40
  positives). Do NOT pull the full approval universe (hundreds) — a wildly imbalanced cohort makes Brier
  uninformative and AUC dominated by easy negatives. Cap negatives at ~2× positives, sampled to match the
  positive year mix (≈ 80% 2025 / 20% 2026), seed-fixed for reproducibility.
- **Provenance guard:** every negative must be confirmable as first-cycle-original NDA/BLA (the ORIG
  submission exists and predates any later supplement) and **must not** appear in the Transparency CR set
  for 2025–26 (else it is a mislabeled positive). Assert this set-disjointness in a test (§7).

### 2.4 Biosimilar exclusion (two signals, OR them, then human-spot-check)
Biosimilars are out of M14 scope and the scorer refuses `is_biosimilar_bla=1`. Detect via:
1. **Text-mine** the `text` body: flag if any of `biosimilar`, `interchangeab`, `351(k)`, `351\(k\)`
   (case-insensitive). Verified to flag **15/73** in-window CRs (all BLAs).
2. **Purple Book cross-check** (authoritative): the FDA Purple Book lists licensed biological products and
   marks biosimilar/interchangeable. Download the Purple Book database export
   (`https://purplebooksearch.fda.gov/` → downloadable CSV) and match by BLA application number. Any BLA
   appno present in the Purple Book with `BsUFA`/biosimilar marker → exclude. (NDAs are never biosimilars,
   so this only narrows the BLA subset.)
- **Decision:** exclude a BLA if (text-flag OR Purple-Book-biosimilar). Emit the excluded list with the
  triggering signal into the funnel log for audit. Set `is_biosimilar_bla=1` on excluded rows so that even
  if one leaks into scoring, the scorer refuses it (defense in depth).
- **Spot-check:** because biosimilar mislabeling directly flips a positive, **manually eyeball the ~15
  flagged BLAs** (company_name + drug from the text) before finalizing — 15 rows is cheap and this is the
  highest-leverage data-quality step.

### 2.5 First-cycle-original confirmation (file_name is useless here — use Drugs@FDA + text)
Since `Orig1s000` is absent in 2025–26 (§1.2):
1. **Primary signal — Drugs@FDA submission history.** For each positive appno, query
   `drug/drugsfda.json?search=application_number:"<NDA|BLA><appno>"`. From `submissions[]`:
   - first-cycle-original ⟺ the **ORIG** submission (`submission_type` starts with `ORIG`) exists AND there
     is **no prior CRL action** on the same appno before the Transparency `letter_date` (no earlier
     `submission_status` indicating a complete-response/resubmission cycle). If an ORIG submission's
     `submission_status` is itself the CRL, that is a first-cycle CRL → **keep** as positive.
   - If the matching submission is a **supplement** (`submission_type` startswith `SUPPL`) → it is an
     sNDA/sBLA → **exclude** (out of scope).
   - If there is an earlier resubmission/second-cycle on the appno before this letter_date → **exclude**
     (resubmission, out of scope).
2. **Fallback signal — text body.** When Drugs@FDA has no usable submission (some 2026 appnos may not be
   posted yet), parse the `text` for "we have completed our review of this **original** New Drug
   Application / Biologics License Application" vs "your **resubmission**" / "Class 1/2 resubmission". Treat
   ambiguous as **exclude** (conservative — a false positive in the cohort is worse than a dropped row given
   small-n).
3. Record `cycle_type='first_cycle_orig'` only for confirmed rows; everything else is dropped from the
   cohort (not scored). The scorer's refusal is the backstop, but A0 should not feed it known supplements.

### 2.6 Expected final cohort (state in the note)
- Positives: **~35–45** (54 raw − biosimilars − supplements/resubmissions).
- Negatives: **~70–90** (33 prospective-2026 + ~40–55 matched 2025 approvals, capped at ~2× positives).
- **n_total ≈ 110–130; positives ≈ 30–35%.** This is small — the CI will be wide; that is the expected,
  honest result and the entire point of A0 (it sets display prominence, it is not a gate).

---

## 3. Point-in-time feature build (as-of submission date)

### 3.1 The anchor date (`ref_date`) per cohort member — NO LOOK-AHEAD
- **Positives:** `ref_date` = the application's **submission/filing date** (the start of the review cycle),
  NOT the CRL `letter_date`. Sourced from the ORIG submission's `submission_status_date` in Drugs@FDA. If
  the ORIG submission date is unavailable, fall back to `letter_date − 304 days` ONLY as a last resort
  (standard 10-month review clock) and flag the row as `ref_date_estimated=true`. **Rationale:** the model
  must see only data available when the application was filed; using the CRL date would leak the outcome
  window's events into the features. `feature_assembly` already enforces this via `ref_date`.
- **Negatives:** `ref_date` = the ORIG submission `submission_status_date` (same convention). For the
  Tier-A prospective-2026 rows, `event_dt` in the CSV is the catalyst (action) date — back it off to the
  filing date the same way, or reuse the M14-built features directly (they were already built point-in-time
  by the M14 pipeline; see §3.4).

### 3.2 Feature sourcing — DECISION: offline builder, not live `assemble_nda_features`
Because the live DB is missing `fda_application_submissions` and `fda_drug_inspections` (§1.5), calling
`assemble_nda_features(client, …)` unchanged would default most features to 0 and produce a meaningless
score. **Two options; the plan recommends Option B for the positives and reuse for the negatives:**

- **Option A (rejected for A0):** ingest Drugs@FDA + inspections + warning letters into the live tables
  first, then call `assemble_nda_features` against the DB. This is the *production* Phase-1 path but is
  heavy for an offline OOS study, mutates shared tables, and re-creates missing tables — out of scope for a
  non-blocking note.
- **Option B (recommended):** build a **thin offline feature builder** that produces the exact scorer-input
  dict per cohort member, sourcing each feature point-in-time directly from FDA bulk/API, mirroring
  `feature_assembly`'s definitions (so results are comparable to the eventual live path):
  - `is_bla` / `ApplType`: from the appno prefix (`appl_is_bla`).
  - `priority` + `SubmissionClassCode`: from the ORIG submission record (Drugs@FDA), exactly as
    `_orig_submission` reads them.
  - `n_prior_filings`: distinct prior **ORIG** appnos for the same sponsor with
    `submission_status_date < ref_date` — query Drugs@FDA by `sponsor_name` (the bulk drugsfda export has
    sponsor_name on every record). Mirror `_n_prior_filings`.
  - `n_drug_inspections_5y_fix`: count FDA inspections for the sponsor in `[ref−5y, ref]`. Source =
    openFDA `drug/inspections` (or the FDA inspection-classifications bulk). Mirror `_n_inspections_5y`.
    If unsourceable for a sponsor, **leave absent** (scorer defaults 0) and increment a coverage counter.
  - `sponsor_has_warning`: 1 if the sponsor has a warning letter with `issue_date ≤ ref_date`. The live
    `fda_warning_letters` table EXISTS — query it via PostgREST by `firm_name_norm`/`sponsor_ticker`
    (mirror `_sponsor_has_warning`), OR use the openFDA warning-letter feed offline. (This is the one
    feature that can reuse the live table directly.)
  - `has_bt/has_ft/has_aa`: designation flags. For positives these are rarely available point-in-time
    without a curated source; **default absent (0)** and record in coverage. (Phase-0 will build the
    designation source; A0 does not block on it.)
  - `n_8ks_30_180_clean`: 8-K count in `[ref−180, ref−30]` for the sponsor's CIK. Source = EDGAR
    full-text/submissions API by CIK (resolve sponsor→CIK via the existing `sponsor_resolver` /
    `fda_assets` mapping where possible). If the sponsor has no resolvable CIK (many small/foreign
    sponsors), **leave absent**; the scorer flags `moderate_confidence_no_edgar_signal`. Record coverage.
  - `ctgov_failed_primary`, `ctgov_any_randomized`, `sponsor_has_orphan_history`: **always absent** (no
    production source, ~4.7% |coef|), matching `feature_assembly`'s documented honest gap.
  - Attach a per-row `_coverage` fraction over the high-signal features (same `_NDA_COVERAGE_KEYS` set), so
    the note can report cohort feature completeness.
- **Keep the offline builder definitions byte-aligned with `feature_assembly`** (same windows: inspections
  5y, 8-K `[ref−180, ref−30]`; same priority/class mapping) so the OOS metrics transfer to the live weekly
  scorer. Add a unit test that the offline builder and `assemble_nda_features` produce identical dicts on a
  shared fixture asset (§7).

### 3.3 No-look-ahead VERIFICATION (must be an explicit, runnable check)
1. **Date-filter assertion:** every Drugs@FDA / inspection / warning / 8-K query used for features must
   carry an upper bound `≤ ref_date` (warnings/priority/class) or a window strictly before `ref` (8-K
   `≤ ref−30`, inspections `≤ ref`). Add an assertion in the builder that the max source-row date used for
   any feature is `≤ ref_date`; fail loud otherwise.
2. **CRL-text isolation:** the feature builder must NOT read the Transparency `text`/`letter_date` for any
   numeric feature — only for label + biosimilar/first-cycle gating. Enforce by passing the builder only
   `(appno, sponsor_name, cik, ref_date)`, never the CRL record.
3. **Spot audit:** for 3 hand-picked positives, manually confirm in Drugs@FDA that no feature input
   post-dates `ref_date`. Document in the note.
4. **Leakage canary:** shuffle the labels and re-run; AUC must collapse to ≈0.5 (sanity that no label
   signal leaked into features). (Reuse `perm_p` from `build_adjusted_m14.py` for a permutation p-value.)

### 3.4 Shortcut for negatives
The 33 prospective-2026 negatives were already feature-built point-in-time by the M14 pipeline and even
carry `p_m14_cal`. **Reuse their precomputed `p_m14_cal` directly** for those rows (and cross-check by
re-scoring through the imported scorer — they must match within float tolerance; if not, investigate a
scorer/version drift). This removes 33 feature builds and anchors the negative side on the model authors'
own point-in-time features.

---

## 4. Scoring + metric computation

### 4.1 Scoring (import the scorer as-is)
1. `import importlib.util` to load
   `/Users/Pico/Downloads/BC_scoring_rubrics_export/NDA_M14_adjusted/scripts/score_m14_adjusted.py`
   (or `sys.path.insert` its dir). **Do not re-implement** the math. Call `score_row(feature_dict, model)`
   per cohort member; `model = load_model()` once.
2. Each `feature_dict` carries `cycle_type='first_cycle_orig'`, `is_biosimilar_bla=0` (we pre-filtered),
   the scorer-input columns from §3.2, plus `ApplNo`/`SponsorName` for traceability.
3. Collect `p_crl` (float), `risk_band`, `confidence_flag`, and the binary `label` (1 for Transparency CR,
   0 for negatives). **Drop any row the scorer refuses** (should be none after §2 filtering — if any refuse,
   that is a cohort-construction bug; log + investigate, do not silently include).

### 4.2 Metrics
- **AUC:** `ranking_auc([p_crl…], [label…])` from `eval_harness/metrics.py` (USE AS-IS).
- **Brier:** `brier_score([p_crl…], [label…])` from `fda_calibration_math.py` (USE AS-IS).
- **Bootstrap 95% CI on AUC:** `boot_ci(y, p, seed=123, n=2000)` (copied verbatim from
  `build_adjusted_m14.py`; sklearn). Report `[p2.5, p97.5]`. This is the identical method behind the
  locked-2025 [0.637, 0.954], so OOS-vs-locked is apples-to-apples.
- **Calibration slope + intercept:** `cal_slope_intercept(y, p)` (verbatim). Slope ≈ 1 = well-calibrated;
  slope < 1 = over-confident.
- **Permutation p-value** (signal check): `perm_p(y, p, n=2000)`.
- **Reliability table (optional):** bucket p_crl into the scorer's bands (low/moderate/elevated/high) and
  report observed CRL rate per band — this is what the product actually shows, so band-level hit rates are
  the most decision-relevant number. (Can use a simple groupby; do NOT route through
  `calibration_curve`'s 0–100 buckets.)
- **Sensitivity runs** (report all three; small-n means the headline must be robust):
  1. Full OOS cohort.
  2. **NDA-only** vs **BLA-only** (the model's biggest coefficient is `is_bla=+1.27`; BLA CRLs dominate the
     2026 slice — check the band still ranks within each type).
  3. **Collapse multi-appno CRL letters to one row** (pick the primary appno) to bound pseudo-replication.
  4. **2026-only** positives (the strictly-OOS, never-seen-by-build slice, n≈11) as the most conservative
     read.

### 4.3 Acceptance for the *note* (not a gate)
There is **no pass/fail**. The deliverable is the numbers + the display decision. Record:
`n, n_pos, AUC, AUC_CI=[lo,hi], Brier, calibration_slope, perm_p, per-band observed rates, feature
coverage`, and the same for each sensitivity run.

---

## 5. Deliverable: the rank-confidence note (one page)

**File:** `tasks/bc_v4_a0_rank_confidence_note.md` (the human-facing output). Structure:

1. **Headline (one sentence):** e.g. *"On an out-of-sample cohort of N first-cycle-original NDA/BLA
   decisions (P CRLs) from 2025–26 that the M14 model never saw, the CRL risk-band achieves AUC=X
   (95% CI [lo, hi]); the CI floor is `lo`."*
2. **Method box:** cohort source (Transparency dump, export_date), OOS definition (exclude locked-2025),
   filtering funnel (the surviving-count table from §2.2), negative source, feature builder, no-look-ahead
   checks passed. One paragraph; link this plan for detail.
3. **Results table:** the §4.2 metrics for all sensitivity runs, **CI floor bolded**.
4. **Per-band reliability:** observed CRL rate within each displayed band (low/moderate/elevated/high) — the
   most product-relevant evidence.
5. **The product display decision (the actual output):** a single rule keyed on the **AUC CI floor** and
   per-band separation:

   | OOS AUC CI floor | Band separation (high vs low observed rate) | Display decision |
   |---|---|---|
   | floor ≥ 0.70 | high-band CRL rate ≥ 2× low-band | **Show band prominently** (primary rank column next to implied move) |
   | 0.60 ≤ floor < 0.70 | bands still monotone | **Show band with a caveat** (secondary, labelled "directional risk-rank, wide CI") |
   | floor < 0.60 OR bands not monotone | — | **De-emphasize** (tooltip/detail only; do not rank the digest by it) |

   - **Expected landing (be honest):** locked-2025 floor was 0.637; the OOS slice is comparably small, so
     the realistic outcome is the **middle row — "show with a caveat."** The note should pre-state this
     expectation and let the computed number confirm/adjust it.
6. **Explicit caveats:** small-n, wide CI, right-censored `approval_status`, feature-coverage gaps
   (inspections/8-K/designations often absent for small sponsors → many rows lean on defaults), biosimilar
   mislabel risk, pseudo-replication from multi-appno letters. State that **this calibrates display only and
   is NOT a go/no-go gate** (the score is a ranking input by the endorsed direction).
7. **One-line maintenance note:** re-run when the Transparency dump adds a full new year of resolved CRLs
   (refit cadence ≈ 1 CRL-cohort/yr) — do not re-run weekly.

---

## 6. Files to create

All under the repo root `/Users/Pico/Documents/Claude/Projects/Conan/`. **A0 is an offline study — keep its
code self-contained; do not wire it into the daily/weekly crons.**

| Path | Purpose |
|---|---|
| `modal_workers/fetchers/universe/openfda_crl_transparency.py` | Fetch index → resolve partition → download/unzip → parse → write raw snapshot parquet/json. (Also the future Phase-3 resolved-outcome source; build it reusable but A0 only needs the read path.) |
| `analysis/bc_a0/build_cohort.py` | §2: filter CRs, OOS-cut, biosimilar + first-cycle filters, build positive+negative cohort → `data/a0/cohort_<export_date>.parquet`. Emits the funnel-count log. |
| `analysis/bc_a0/feature_builder.py` | §3.2 offline point-in-time feature builder (mirrors `feature_assembly` definitions) + §3.3 no-look-ahead assertions. |
| `analysis/bc_a0/score_and_metrics.py` | §4: import M14 scorer, score cohort, compute AUC/Brier/CI/cal-slope/perm-p + sensitivity runs; write `data/a0/metrics_<export_date>.json`. Copies `boot_ci`/`cal_slope_intercept`/`perm_p` from `build_adjusted_m14.py`. |
| `data/a0/` (dir) | Frozen artifacts: raw dump snapshot, cohort parquet, metrics json, funnel log. (Git-ignore the large raw json; commit cohort + metrics.) |
| `tasks/bc_v4_a0_rank_confidence_note.md` | §5 the human-facing one-page deliverable. |
| `tests/bc_a0/test_cohort_and_features.py` | §7 test plan. |

**Optional bc_ mirror (traceability, not required):** after computing, you MAY upsert the cohort into
`bc_applications` + `bc_application_features` (with `feature_quality='built_at_install'`, `as_of_date=ref`)
and the scores into `bc_rubric_scores` (`scorer_name='M14_adjusted'`) tagged with a distinguishing
`snapshot_date` so they never mix with production scoring. This gives the dashboard a way to show the OOS
cohort later. **Gate this behind a `--mirror` flag, default off**, and never write to `bc_prediction_outcomes`
(that table is for live resolved catalysts). Apply nothing to migration 005; A0 writes no operator_flags.

---

## 7. Test plan

1. **Source-shape guard:** assert the downloaded dump has `{"meta","results"}`, `len(results) ≥ 400`, and
   `letter_type=='COMPLETE RESPONSE'` count ≥ 400 (regression alarm if FDA changes the schema).
2. **Cohort funnel snapshot test:** with the frozen 2026-06-01 snapshot, assert exact counts at each step:
   CR=426, in-window CR=73, distinct in-window appnos=63, locked-overlap=9, raw OOS positives=54. Lock these
   as fixtures; if FDA updates the dump, the test surfaces the delta intentionally.
3. **Label reconciliation test:** assert the 9 locked-2025 positives ⊆ Transparency CR appnos (the
   self-validation in §1.3); fail if it drifts.
4. **Negative-disjointness test:** assert `negative_appnos ∩ transparency_CR_appnos(2025–26) == ∅` (no
   mislabeled positive in the negative class).
5. **Feature-parity test:** offline `feature_builder` output == `assemble_nda_features` output on a shared
   fixture asset (same dict keys + values), proving the OOS features match the live weekly path.
6. **No-look-ahead test:** for a fixture positive, inject a synthetic source row dated `ref_date + 1d` and
   assert the builder excludes it (date-filter holds); run the label-shuffle/`perm_p` canary and assert
   AUC≈0.5 / p high.
7. **Scorer-import test:** `score_row` on `examples/example_input.csv` reproduces
   `examples/example_output.csv` (proves the import path + model load are intact); and the 33 prospective
   negatives re-scored match their CSV `p_m14_cal` within 1e-6.
8. **Metrics-reuse test:** `ranking_auc` + `brier_score` on the locked-2025 CSV (`p_m14_cal`,
   `CRL_label_strict`) reproduce AUC≈0.810 / Brier≈0.0635 (proves the metric wiring matches the model card
   before trusting it on the OOS slice).
9. **CI-method parity test:** `boot_ci` on the locked-2025 CSV reproduces ≈[0.637, 0.954] (seed 123,
   n=2000) — confirms the OOS CI is computed the same way as the published locked CI.

---

## 8. Risks & mitigations

- **Small-n / wide CI (primary, expected):** ~35–45 positives → AUC CI floor likely < 0.70. *Mitigation:*
  this is the intended finding; the note headlines the floor and maps it to "show with caveat." A0 is
  explicitly non-blocking — a wide CI does not stop the product, it just sets band prominence.
- **Pseudo-replication:** 11 CR letters carry multiple appnos; one sponsor can have several CRLs. Treating
  each appno as independent overstates n. *Mitigation:* §4.2 sensitivity run #3 collapses multi-appno
  letters; report both; flag sponsor-clustered rows. (Could add a sponsor-level cluster bootstrap if the
  point estimate is borderline.)
- **Biosimilar mislabeling (flips a positive):** text-flag catches 15/73 but may miss/over-call.
  *Mitigation:* OR with Purple Book + manual eyeball of the ~15 flagged BLAs (§2.4); set
  `is_biosimilar_bla=1` so the scorer refuses any leaker.
- **First-cycle misclassification (file_name token gone in 2025–26):** the spec's `Orig1s000` heuristic
  returns 0 here. *Mitigation:* §2.5 Drugs@FDA-submission-based determination + conservative text fallback;
  ambiguous → exclude.
- **Feature coverage gaps (live tables missing):** `fda_application_submissions` /`fda_drug_inspections`
  absent live; many cohort sponsors lack a resolvable CIK for 8-Ks. *Mitigation:* offline builder sources
  Drugs@FDA / openFDA / EDGAR directly; absent features default 0 (matching production behaviour); report
  per-row `_coverage`; note that low-coverage rows lean on the intercept and thus compress toward the base
  rate — a known limitation of the band, not a bug.
- **Right-censored negatives:** `approval_status` is 100% `Unapproved` for in-window CRLs (expected — the
  label is "got a CRL", which is observed). For the negative class, a 2026 application "pending, not yet
  CRL'd" is a soft negative that could later flip to CRL. *Mitigation:* prefer **approved** negatives
  (Drugs@FDA `submission_status:"AP"`) over still-pending ones; flag any pending negative; the 2026
  prospective-CSV negatives are the M14 authors' own labels.
- **Reproducibility drift:** FDA updates the dump in place. *Mitigation:* freeze the snapshot by
  `export_date` under `data/a0/`; all tests pin to the frozen counts; re-run is a deliberate, logged event.
- **Scope creep into a gate:** someone may read the AUC as a go/no-go. *Mitigation:* the note states in two
  places that this calibrates display only; the endorsed direction demoted the score to a ranking input.

---

## 9. Non-blocking / sequencing note

A0 is the **most immediately executable** track in the Light-v4 plan: the cohort source is verified live,
the scorer imports as-is, and the metrics helpers exist. It has **zero dependency on Phase 0** (universe /
PDUFA sourcing) or the daily monitor, runs entirely offline, mutates no production tables (mirror is
opt-in), and is **explicitly NOT a project gate** — the live weekly scorer (Phase 1) ships regardless of the
A0 number; A0 only decides how prominently the resulting risk-band is shown. It can be executed in parallel
with, or ahead of, everything else.
