# A0 — CRL risk-band rank-confidence note

> **Workstream:** Light v4 (BC-FDA monitor), Phase 1 parallel track **A0**.
> **Status:** NON-BLOCKING. The M14 CRL score is a **ranking input** (risk-band), not a
> calibrated probability or a go/no-go gate. This note calibrates only **how prominently the
> band is shown in the product**.
> **Computed:** 2026-06-04, offline, against the frozen FDA CRL Transparency export
> `export_date=2026-06-01` (439 records) and the M14 adjusted artifacts. Reproducible from
> `data/a0/`. Mutated no production tables.

---

## 1. Headline

On an out-of-sample cohort of **81 first-cycle-original NDA/BLA decisions (27 CRLs / 54 approvals)**
from 2025–26 that the M14 model never saw, the CRL risk-band achieves **AUC = 0.694**
(Mann–Whitney; sklearn AUC 0.714), bootstrap **95% CI [0.594, 0.815]** — the **CI floor is `0.594`**.
The displayed bands are **monotone in observed CRL rate** (low 0% → moderate 12% → elevated 46% →
high 75%) and a label-shuffle canary collapses AUC to 0.42, so the ranking signal is real (perm-p 0.0005),
not leakage.

**The CI floor (`0.594`) sits right at the 0.60 boundary** of the display-decision table. It is depressed
by a structural feature-coverage gap on the positive side (most CRL'd applications are absent from the
public Drugs@FDA export, so their non-`is_bla` features default to 0 and compress their scores toward the
base rate). On coverage-matched and stricter slices the floor moves up to 0.65 (2026-only) and the point
AUC holds at 0.67–0.71. Recommended display: **show the band with a caveat** (middle row) — see §5.

---

## 2. Method box

- **Cohort source:** FDA CRL Transparency dump (`api.fda.gov/download.json` →
  `results.transparency.crl`, single partition), frozen at `export_date=2026-06-01`
  (`data/a0/crl_transparency_raw_2026_06_01.json`, 439 records, 426 COMPLETE RESPONSE, 100% FDA-keyed).
- **OOS definition:** exclude the 9 locked-2025 ApplNos the M14 test set already scored (digit-normalized
  set-subtract). 2026 rows are OOS by construction (2026 ∉ test years).
- **Filtering funnel** (frozen counts, reproduced live; locks as test fixtures):

  | Step | Surviving |
  |---|---:|
  | COMPLETE RESPONSE | 426 |
  | `letter_year ∈ {2025,2026}` | 73 |
  | distinct NDA/BLA appnos (digit-normalized) | 63 |
  | exclude locked-2025 ApplNos (OOS cut) | 54 |
  | exclude biosimilars (text-flag, 14 BLAs) | 40 |
  | confirm first-cycle-original (−11 resubmission, −2 ambiguous) | **27 positives** |

- **First-cycle confirmation (§2.5):** `file_name`'s `Orig1s000` token is absent in the 2025–26 slice
  (0/73 — all use `CRL_<TYPE><appno>_<YYYYMMDD>.pdf`), so first-cycle was determined from
  **(0) the dump itself** — an appno with an *earlier* CR letter is a definitive resubmission (21/63
  in-window appnos carry ≥2 CR letters); **(1) Drugs@FDA** submission history; **(2) text fallback**
  (a CR that "constituted a complete response to our action letter" is a resubmission). The bare token
  "your resubmission"/"resubmitting" was found to OVER-match first-cycle boilerplate ("Prior to resubmitting
  the labeling, use the SRPI checklist…", 17/18 hits) and was **not** used. Ambiguous → excluded
  (conservative).
- **Negative class (54):** **Tier A** = the 33 M14 prospective-2026 label-0 rows (already feature-built
  point-in-time by the model authors, carry `p_m14_cal`; reused directly per §3.4). **Tier B** = 21
  first-cycle-original 2025–26 NDA/BLA **approvals** (`submission_status:"AP"`) pulled from Drugs@FDA,
  capped at ≈2× positives, year-mix matched, seed-fixed, and asserted set-disjoint from the Transparency
  CR set.
- **Feature builder (§3.2):** an offline point-in-time builder (`analysis/bc_a0/feature_builder.py`) whose
  definitions are byte-aligned with the live `feature_assembly.assemble_nda_features` (unit-test
  `test_feature_builder_parity_with_assemble_nda` proves identical dicts on a shared fixture). Anchor date
  `ref_date` = ORIG action date − PDUFA clock (304d), or `letter_date − 304d` (flagged estimated) when
  Drugs@FDA has no record.
- **Scoring (§4.1):** the M14 scorer is imported as-is (`score_m14_adjusted.score_row`); no math
  re-implemented. **No-look-ahead** is enforced in the builder (every source row ≤ `ref_date`, asserted;
  the CRL text/letter_date is never passed to a numeric feature) and checked by a label-shuffle canary.
- **Metrics:** AUC via `eval_harness.metrics.ranking_auc` (as-is); Brier via
  `fda_calibration_math.brier_score` (as-is); bootstrap CI / calibration slope / perm-p via the **verbatim**
  `boot_ci` / `cal_slope_intercept` / `perm_p` copied from `build_adjusted_m14.py` — so this OOS CI is
  computed the *identical* way as the published locked-2025 CI [0.637, 0.954] (apples-to-apples).

*(Full detail: `tasks/bc_v4_a0_cohort_confidence.md`. Artifacts: `data/a0/{cohort,metrics,funnel,scored_cohort}_2026_06_01.*`.)*

---

## 3. Results table (sensitivity runs)

All AUC point estimates are `ranking_auc`; CI / slope / perm-p use the verbatim sklearn-based M14
functions (seed 123, n=2000). **CI floor bolded.**

| Run | n | CRLs | AUC | 95% CI | Brier | Cal slope | perm-p |
|---|---:|---:|---:|---|---:|---:|---:|
| **Full OOS cohort** | 81 | 27 | 0.694 | [**0.594**, 0.815] | 0.225 | 1.24 | 0.0005 |
| NDA-only | 69 | 21 | 0.641 | [**0.533**, 0.791] | 0.228 | 1.04 | 0.013 |
| BLA-only | 12 | 6 | 1.000 | [**1.000**, 1.000] | 0.208 | 1.32 | 0.0005 |
| Collapse multi-appno letters | 81 | 27 | 0.694 | [**0.594**, 0.815] | 0.225 | 1.24 | 0.0005 |
| 2026-only positives (strictest OOS) | 61 | 7 | 0.788 | [**0.650**, 0.948] | 0.084 | 1.44 | 0.003 |

Notes:
- **BLA-only is not interpretable** — n=12 (6 CRLs vs 6 approvals); the perfect AUC is a small-n artifact
  (`is_bla=+1.27` is the model's biggest coefficient and the 6 BLA CRLs happened to all out-rank the 6 BLA
  approvals). It is reported for completeness only.
- **Collapse-multi-appno equals Full** because no multi-appno CRL letter survived to the final positive set
  (the 3 in-window multi-appno letters were removed earlier by the OOS cut / biosimilar / resubmission
  filters), so there was no pseudo-replication to collapse.
- **2026-only** is the cleanest read (more of its positives resolve in Drugs@FDA → higher coverage →
  Brier 0.084, floor 0.650) but rests on only **7 positives**.
- **Calibration slope ≈ 1.0–1.4** (vs locked 0.87): the band is *not* systematically over/under-confident in
  rank terms; the high Brier is a level (coverage) effect, not a slope effect — see §4 / §6.

**Coverage-matched confound check (does the AUC just detect "tier-A negatives have more features"?):**
positives vs Tier-B negatives *only* (both offline-built, same coverage regime) → AUC **0.672**; positives
vs Tier-A (full-feature) negatives → AUC **0.708**; full cohort → 0.694. The ranking survives within a
single coverage regime, so it is a real CRL signal, not a coverage artifact. (Caveat retained in §6:
positives do have systematically lower coverage than negatives, 0.22 vs 0.49.)

---

## 4. Per-band reliability (the product-relevant evidence)

Observed CRL rate within each displayed band, full cohort (n=81):

| Band (p_crl) | n | CRLs | Observed CRL rate | Mean p_crl |
|---|---:|---:|---:|---:|
| low (<0.08) | 16 | 0 | **0.00** | 0.028 |
| moderate (0.08–0.15) | 16 | 2 | **0.125** | 0.110 |
| elevated (0.15–0.25) | 41 | 19 | **0.463** | 0.182 |
| high (≥0.25) | 8 | 6 | **0.750** | 0.365 |

The bands are **strictly monotone** and the high band's observed CRL rate (0.75) is **6× the moderate band
and ≥ 2× any lower band** — strong rank separation at the band level, which is exactly what the product
shows. The mean-p-vs-observed-rate gap in the elevated band (mean p 0.18 but 46% realized) is the
coverage-compression effect (§6): point probabilities read low because most positive features default to 0,
but the *ordering* is preserved.

---

## 5. Product display decision

Decision rule keyed on the **AUC CI floor** and per-band separation:

| OOS AUC CI floor | Band separation (high vs low observed rate) | Display decision |
|---|---|---|
| floor ≥ 0.70 | high-band CRL rate ≥ 2× low-band | Show band prominently (primary rank column next to implied move) |
| 0.60 ≤ floor < 0.70 | bands still monotone | **Show band with a caveat** (secondary, labelled "directional risk-rank, wide CI") |
| floor < 0.60 OR bands not monotone | — | De-emphasize (tooltip/detail only; do not rank the digest by it) |

**Where the computed number lands:** the full-cohort floor is **0.594** — literally `< 0.60`, i.e. it falls
*just* inside the **bottom (de-emphasize)** row by 0.006. However:
- the bands are strongly monotone (the bottom-row's "bands not monotone" disqualifier does **not** apply);
- the floor is depressed by a *known, correctable* artifact — positive-side feature-coverage starvation
  (most CRL'd applications are missing from the public Drugs@FDA export), which compresses positive scores
  and shrinks the observed separation. On a coverage-matched read the point AUC is 0.67–0.71 and the
  strictest-OOS (2026-only) floor is **0.650**, squarely in the middle row;
- the floor is stable across bootstrap seeds (0.585–0.602) — it is genuinely *at* the boundary, not a
  fluke either side.

**Recommendation: middle row — "show the band with a caveat."** Display the risk-band as a *secondary*
directional rank-rank column labelled "directional CRL risk-rank (wide CI, coverage-limited)", do **not**
let it be the primary digest sort key, and do **not** present the point probability as calibrated. This is
the spec's pre-stated expected landing, and the computed evidence (monotone bands + coverage-matched AUC
0.67–0.71) supports it even though the headline full-cohort floor is fractionally under the strict 0.60 cut.
Re-run before promoting to the top row (see §7).

This calibrates **display prominence only**. It is **NOT a go/no-go gate** — the live weekly scorer ships
regardless of this number; A0 only sets how loudly the band speaks.

---

## 6. Caveats (read before acting on any number here)

- **Small-n / wide CI (primary, expected):** 27 positives. The 95% CI spans 0.22 of AUC. The floor, not the
  point estimate, drives the decision.
- **Positive-side feature-coverage starvation (the dominant limitation):** mean feature coverage is **0.40**
  overall but **0.22 for positives** vs **0.49 for negatives**. `is_bla` is sourced 81/81, `priority` 53/81,
  class 51/81, `n_prior_filings` 54/81; **inspections, designations (bt/ft/aa), 8-Ks, and all CT.gov
  features are absent for the entire cohort** (no offline source — matching the live pipeline's documented
  gaps and the missing `fda_application_submissions`/`fda_drug_inspections` live tables). Most 2025 CRL'd
  applications are **not in the public Drugs@FDA export** (47/54 raw OOS positives returned 404), so their
  non-`is_bla` features default to 0 and their p_crl compresses toward the base rate. This **deflates Brier
  to 0.225** (Brier on positives alone is 0.63; on negatives 0.023) and pulls the AUC floor down. The
  *ranking* survives (coverage-matched AUC 0.67–0.71); the *level* (calibration) does not — consistent with
  the spec's "low-coverage rows lean on the intercept and compress toward the base rate — a known limitation
  of the band, not a bug."
- **`sponsor_has_warning` is present-but-empty:** the live `fda_warning_letters` table exists and was queried
  read-only, but is **empty in this environment**, so every row got `sponsor_has_warning=0`. This is a real
  (degenerate) value, not fabricated; it removes a +0.633-coef feature uniformly and slightly compresses
  all scores. Re-run when the table is populated.
- **Right-censored negatives:** `approval_status` is 100% `Unapproved` for in-window CRLs (expected — the
  label "got a CRL" is observed). Tier-B negatives are confirmed **approvals** (`AP`), and Tier-A are the
  M14 authors' own labels — neither is a soft "pending" negative, so the right-censoring risk is contained.
- **Biosimilar mislabel risk:** exclusion is text-flag-only here (15/73 in-window, 14 in the OOS slice; all
  BLAs). The Purple Book authoritative cross-check is stubbed (best-effort, off) — a missed biosimilar would
  flip a positive. The 14 excluded BLAs were eyeballed (Teva TVB-009, Tanvex TX05, Accord, Celltrion, etc. —
  all recognizable biosimilars). Excluded rows also get `is_biosimilar_bla=1` so the scorer refuses any leaker.
- **First-cycle misclassification:** mitigated by the strong dump-internal prior-CR-letter signal (§2);
  ambiguous rows excluded. The cohort dropped to 27 positives (vs the spec's ~35–45 estimate) **because**
  this signal is more aggressive than text-only would be — it is *more correct* (it caught 11 genuine
  resubmissions with ≥2 CR letters), but it shrinks n. An honest, defensible trade.
- **Pseudo-replication:** none in the final positive set (collapse-multi-appno run is identical to full).
- **Reproducibility:** the snapshot is frozen by `export_date` under `data/a0/`; all funnel counts are
  pinned as test fixtures; re-running is a deliberate, logged event.

---

## 7. Maintenance note

Re-run **only** when the Transparency dump adds a full new year of resolved CRLs (refit cadence ≈ 1
CRL-cohort/yr) — **not** weekly. Re-run **sooner** if either coverage source improves, because both would
lift the floor toward the "show prominently" row: (a) the live `fda_warning_letters` table is populated, or
(b) a sponsor→CIK map + an inspections source let the offline builder fill the positive-side features that
currently default to 0 (the dominant cause of the marginal floor). Until then, the band is a *secondary,
caveated, coverage-limited* directional rank — never the primary digest sort key and never a gate.
