# Token & Signal Optimization — Follow-up Plan (2026-05-23)

Companion to PR `fix/token-and-signal-optimization-2026-05-23`.

That PR landed the **Phase A plumbing** (5 fixes that are semantics-preserving
and need no eval gate) and the **Phase B routing/escalation logic** (4 fixes,
all env-flag-revertible). This document tracks the **Phase C** items that
were deferred from that PR because they (a) require an eval gate per D-103,
(b) need a schema migration with backfill, or (c) are bigger architectural
changes that deserve their own PR + review.

The Phase A/B fixes are the ones that should noticeably reduce daily email
volume and per-asset Tier-1 cost. Phase C unblocks the things that move
**prediction accuracy** — which is gated on closing the eval-data loop.

## Status snapshot at the time of this writing

| # | Item | Phase | Status |
|---|------|-------|--------|
| 1 | Reactor `docSetHash` propagation | A.1 | shipped |
| 2 | 24h hard halt in drainer | A.2 | shipped |
| 3 | Sub-agent prompt cache_control | A.3 | shipped |
| 4 | Stage 7 + batch ensemble through `OrchestratorClient.call` | A.4 | shipped |
| 5 | Email subject-line tagging | A.5 | shipped |
| 6 | Asset+direction 24h cooldown in fanout | B.1 | **already-existed**, verified |
| 7 | Tighten Tier-1 escalation (stable-high-conviction suppress) | B.2 | shipped (env-flag revertible) |
| 8 | Material-change gate for scheduled Tier-2 fanout | B.3 | **already-existed**, verified |
| 9 | Dispersion-based abstain in ensemble | B.4 | shipped (env-flag revertible) |
| 10 | Backfill 1502 staged eval cases | C.1 | **deferred** — operational (see §1) |
| 11 | Define explicit prediction target schema | C.2 | shipped (env-default backfill = `price_move` / 30d / `forward_return_t30_calendar`) |
| 12 | Wire K-NN precedent skill into Stage 4 | C.3 | shipped (real `_knn_similarity` over iter-4 features in `compute.py`, results threaded into `Stage4Anchor.similar_cases`) |
| 13 | Market-side gate (options IV vs LLM conviction) | C.4 | shipped (env-flag off by default: `ORCH_ENABLE_MARKET_SIDE_GATE`) |
| 14 | `convergence_signature` belt-and-suspenders dedup | C.5 | shipped (signature computed in Stage 10, partial unique index on `(asset_id, signature) WHERE superseded_by IS NULL`) |
| 15 | Role-diverse ensemble (Opus + Sonnet + Haiku) | C.6 | shipped (env-flag off by default: `ORCH_ENABLE_ROLE_DIVERSE_ENSEMBLE`; D-127 still gates promotion-to-default until eval data closes) |
| 16 | RAG `corpus="all"` → targeted-corpus default | C.7 | shipped (recommend_stage_1_rag_corpora chooses targeted set from anchor signals; `ORCH_STAGE_1_RAG_CORPORA` overrides) |

## §1 Backfill 1502 staged eval cases (C.1)

**Problem.** D-103 paired-bootstrap gate requires n≥200 resolved cases per
arm. Today the harness has 271 cases, but a large staged set
(`audit/sub_agent_schema_drift_2026-05-23.md` §S-3 etc. has the count) is
unresolved because no outcome label has been written. The calibration loop
(D-123) and the eval gate (D-103) cannot progress until this is closed.

**Plan.**
1. Run `modal_workers/scripts/label_forward_returns.py` end-to-end over
   the staged inputs. The script already exists and the labeling rule is
   defined (price-based forward returns at +5/+10/+20 trading days). The
   work is operational, not engineering — kick off a backfill job and
   verify resolution count crosses n=200 per outcome bucket.
2. Once n≥200 per bucket, the nightly calibration refit job
   (`modal_workers/scripts/nightly_calibration_refit.py`) will start
   promoting curves through the D-123 promotion gate without manual
   override.
3. The downstream effect — `convergence_assessments.conviction_pct_calibrated`
   diverging from `conviction_pct` — is what the fanout
   `shouldSendAssessmentImmediateEmail` gate uses for material-change detection.
   Today they're equal because calibration is no-op, which means small
   conviction wobbles never look material. After this lands, the existing
   gate gets its teeth back.

**Estimated effort.** 4-8h ops + 1-2 days backfill runtime.
**Blocking risk.** Need to confirm `label_forward_returns.py` is currently
runnable against the production export — check
`audit/fda_review_v3_cutover_2026-05-11.md` §labeling status before
kicking off.

## §2 Explicit prediction-target schema (C.2)

**Problem.** `convergence_assessments` today is ambiguous about what the
LLM is predicting. `thesis_direction` + `conviction_pct` could mean:
  - "stock will move LONG within 30 trading days" (price target)
  - "FDA approval will land within PDUFA window" (regulatory target)
  - "AdCom will vote favorable" (event target)
The eval harness defaults to a +20d forward-return label, which is right for
one of these targets and wrong for the other two. Without an explicit
target field, calibration mixes targets — the curve is averaging across
populations that don't share a true conditional distribution.

**Plan.**
1. Add four columns to `convergence_assessments`:
   - `target_type` text (one of: `price_move`, `regulatory_outcome`,
     `event_outcome`)
   - `horizon_days` int (the window for the prediction; null if event-anchored)
   - `event_anchor` text (catalyst event_id or null)
   - `label_rule` text (`forward_return`, `approval_decision`,
     `adcom_recommendation`, …)
2. Stage 9 extractor prompts get a new required field
   `prediction_target` that maps to (`target_type`, `horizon_days`,
   `event_anchor`). Schema validation + golden test.
3. Backfill: every existing row gets `target_type='price_move',
   horizon_days=20, label_rule='forward_return'` to match
   `label_forward_returns.py` historical labeling. New rows must emit
   the target.
4. Eval harness reads `label_rule` per row and dispatches to the right
   labeling function. Stratified calibration curve per `target_type`.

**Gate.** This is a prompt change and a schema change — D-103 paired
bootstrap applies. Plan: ship behind a feature flag, run replay cassette
with old vs new prompts on 271 historical cases, accept only if
calibration accuracy doesn't regress and direction-accuracy doesn't drop.

**Estimated effort.** ~2 weeks (1w engineering, 1w eval-loop validation).

## §3 K-NN precedent skill into Stage 4 (C.3)

**Problem.** `.claude/skills/compare-to-historical-precedents/SKILL.md`
exists and is invoked by some operator queries, but is NOT in the
orchestrator Stage 4 anchor pipeline. Stage 4 today computes a
reference-class signature and looks up a coarse base rate from
`compute.py:128`; it doesn't pull the actual K-NN neighbors that the
skill would produce.

**Plan.**
1. Add `stage_4b_precedent_lookup` after the existing Stage 4 anchor.
   Calls the precedent-skill helper (`compare-to-historical-precedents`)
   with the current asset's iter-4 features.
2. Returns top-K (default 5) similar resolved cases with their realized
   outcomes + similarity scores.
3. Stage 5 conviction generator gets the K-NN summary in its context.
   Stage 7 constitutional check uses it as a sanity bound (already has a
   `base_rate_check` field — extend to `precedent_check`).
4. Persist the K-NN id list to `convergence_assessments.similar_resolved_case_ids`
   (the column exists — Stage 4 only populates it from the coarse anchor
   today, replace with K-NN output when available).

**Gate.** D-103 paired bootstrap — this is an arch change. Replay
cassette before/after on 271 cases; accept only on Brier-score
improvement.

**Estimated effort.** ~1 week engineering + 3-5 days eval validation.

## §4 Market-side gate (C.4)

**Problem.** The LLM has no idea what the market already believes. When
LLM conviction is 70% LONG and options IV implies a 2σ move is already
priced in, the alpha is much smaller than when conviction is 70% LONG and
options are pricing in flat. The current alert pipeline doesn't know
this — it emails based on conviction alone.

**Plan.**
1. After Stage 8 (calibration), call `modal_workers/providers/polygon/options_data.py`
   to get implied move + IV percentile for the asset (when ticker is
   non-null).
2. Compute `expected_value_bps` = (calibrated_conviction - market_implied_pct) *
   directional_payoff_bps. The column already exists in the schema.
3. Add a fanout gate: when `expected_value_bps < EV_THRESHOLD_BPS`,
   downgrade band immediate → watchlist with reason `low_ev_vs_market`.
4. UI: dashboard already has an EV column — surface this gate decision
   so operators can see WHY a high-conviction signal was watchlisted.

**Dependencies.** Polygon options API key must be live in prod env (today
the Tier-2 path short-circuits when the key is missing — see commit
489fb33 on the parent branch). Verify before lighting this up in prod.

**Estimated effort.** ~1 week.

## §5 `convergence_signature` belt-and-suspenders dedup (C.5)

**Problem.** PR-2 (the `document_set_hash` content-aware dedup, fixed
this PR by also stamping it on the orchestrator_runs row in the reactor)
catches most duplicate orchestrator runs. But two adjacent docs that
produce identical Stage 9 output (same direction, same conviction, same
thesis_summary) can still write two consecutive
`convergence_assessments` rows that look identical. This is rare but
real (AXSM 17-emit episode had several such pairs).

**Plan.**
1. Add `convergence_assessments.convergence_signature` text column.
2. Stage 10 persistence computes signature = md5(direction + bucketed_conviction
   + cited_prose_hash + key_facts_id_set).
3. Partial unique index on (asset_id, convergence_signature) WHERE
   superseded_by IS NULL — catches duplicate Stage 9 outputs at the DB
   layer.
4. Conflict policy: on 23505, supersede the prior row in place rather
   than insert.

**Estimated effort.** 1-2 days.

## §6 Role-diverse ensemble — deferred per D-127 (C.6)

D-127 explicitly deferred role-diverse ensembles (Opus synthesis +
Sonnet adversary + Haiku extractor) until the eval data loop closes.
Reason: with temperature already deprecated on Claude 4.5+ (see the
in-flight `client.py` change that this PR builds on), ensemble diversity
comes from MODEL diversity. Without n≥200 per arm we can't prove the
diversity is worth the cost.

**Action when C.1 closes.** Revisit D-127. Concretely:
- Run a 50-case A/B with same-model ensemble vs role-diverse ensemble
  using replay cassette.
- Accept role-diverse if Brier score improves AND avg_cost_per_run stays
  under $20.

## §7 RAG `corpus="all"` → targeted-corpus default (C.7)

**Problem.** `modal_workers/rag/hybrid_search.py:250-277` shows
`corpus="all"` runs 4 separate embed+rerank calls (literature, filings,
labels_aes, news) and concatenates. Stage 1 synthesis defaults to `all`
without thinking — every Stage 1 call burns 4x the Voyage budget.

**Plan.**
1. Add a hint field to the orchestrator anchor:
   `recommended_rag_corpora: list[str]`.
2. Anchor logic: regulatory-history-heavy assets → `["filings","labels_aes"]`;
   trial-pending assets → `["literature","filings"]`; PDUFA-imminent →
   `["filings","news","labels_aes"]`.
3. Stage 1 passes the recommended set instead of `all`. Operator can
   still override via the dashboard "Force corpus=all" button for
   diagnostic runs.
4. Fall back to `all` only when anchor confidence is low.

**Eval.** Not a prompt change — pure retrieval scope. Sanity check: run
replay cassette and assert no Stage 1 ends up missing a fact it cited in
the legacy `all` run. If it does, expand the targeted set.

**Estimated effort.** ~3-5 days.

## Order of execution

Recommended sequence once you decide to start Phase C:

1. **C.1 backfill first.** Everything else benefits from a closed eval
   loop — D-103 paired-bootstrap criterion stops being aspirational.
2. **C.5 convergence_signature.** Cheapest insurance against any AXSM-shaped
   repeat. Land independently of the eval gate.
3. **C.7 targeted-corpus default.** Cuts Voyage spend by 50%+ on Stage 1.
   No eval gate needed (retrieval scope, not prompt).
4. **C.2 schema migration.** Long path — coordinate with operator UX
   because the dashboard will need a target_type column.
5. **C.3 K-NN precedent + C.4 market-side gate.** Both are arch changes;
   schedule after C.1 closes so the eval gate can adjudicate.
6. **C.6 role-diverse ensemble.** Last — needs eval data + the §C.2
   schema landed so we know what the prediction target IS.

## Env flags currently in play

These were added by this PR. Default values listed; flip to revert.

| Flag | Default | What it does |
|------|---------|--------------|
| `ORCH_SUB_AGENT_DISABLE_PROMPT_CACHE` | unset (off, i.e. caching on) | Set `=1` to disable sub-agent prompt caching |
| `TIER2_ESCALATION_SUPPRESS_STABLE_HIGH_CONVICTION` | `1` (on) | Set `=0` to restore legacy "always escalate ≥60% conviction" |
| `TIER2_ESCALATION_MATERIAL_CONVICTION_DELTA` | `5.0` | Min conviction Δ (pp) to fire high_conviction reason when stable-suppression is on |
| `ORCH_DISABLE_ENSEMBLE_DISPERSION_ABSTAIN` | unset (off, i.e. abstain on) | Set `=1` to disable dispersion-based band downgrade |
| `ORCH_ENSEMBLE_DISPERSION_ABSTAIN_PCT` | `15.0` | Conviction stddev ceiling (pp) before abstain |
| `ORCH_ENSEMBLE_DIRECTION_ABSTAIN_FRAC` | `0.6` | Majority-direction fraction floor before abstain |
| `ORCH_STAGE_1_RAG_CORPORA` | unset (use anchor recommendation) | CSV override for the Stage 1 RAG corpus set; `all` keeps legacy 4-corpus fanout |
| `ORCH_ENABLE_MARKET_SIDE_GATE` | unset (off) | Set `=1` to allow band downgrade `immediate → watchlist` when expected_value_bps below threshold |
| `ORCH_MARKET_GATE_EV_THRESHOLD_BPS` | `0.0` | Min EV (bps) before market-side gate fires |
| `ORCH_ENABLE_ROLE_DIVERSE_ENSEMBLE` | unset (off) | Set `=1` to swap streaming ensemble for the Opus/Sonnet/Haiku role-diverse path |
| `ORCH_ROLE_ENSEMBLE_OPUS_MODEL` / `_SONNET_MODEL` / `_HAIKU_MODEL` | Opus 4.7 / Sonnet 4.6 / Haiku 4.5 defaults | Override individual ensemble role models when the role-diverse path is on |
| `ORCH_POST_MORTEM_WINDOW_DAYS` | `60` | Days after catalyst before a post-mortem is enqueued (used when no catalyst event found) |

Document them in `DECISIONS.md` if any become permanent post-soak.

## What still blocks the eval-loop closure (C.1, D-127 promotion)

C.1 is operational rather than engineering — it requires running
`label_forward_returns.py` against the staged inputs until each outcome
bucket reaches n≥200 resolved. Once that's done, two things follow
automatically:

1. **D-127 unblocks** — the eval harness can run paired-bootstrap
   on same-model vs role-diverse ensemble (now feature-flagged off)
   and decide whether to promote it to default.
2. **Calibration becomes meaningful** — `nightly_calibration_refit.py`
   now refits per `target_type` (stratified Phase C work), so the curves
   stop blending populations. Until n is large enough per stratum the
   refit will still skip those buckets safely.

When C.1 closes, flip the relevant env flags above to `=1`, run the
paired bootstrap on the 271+ resolved cases, and only promote what
clears the D-103 gate.
