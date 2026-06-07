# BC-FDA Light v4 — v1 positioning (band-only, honest caveats)

> Written 2026-06-07 at go-live of the daily digest. This is the "what v1 claims / does
> NOT claim" note so the product is never oversold. Read before widening recipients.

## What v1 IS
A **fail-loud daily email digest** of the ~15–20 in-window pending FDA PDUFA names
(NDA/BLA), each carrying:
- a **risk BAND** (`low|moderate|elevated|high`) + an **out-of-fold percentile rank**;
- **days-to-PDUFA**, application type, ticker;
- (once P3 synthesis is on) a short **AI synthesis** of what changed today, gated by a
  deterministic threshold — the LLM can never widen its own gate.

Monitoring + capture is deterministic Python (insider Form 4 + 8-K/news), liveness is a
`bc_pipeline_runs` row per run (a missing row is the alert — no watchdog meta-system).

## What v1 does NOT claim (the caveats that keep it honest)
1. **No calibrated CRL probability is shown.** `p_crl` is persisted-internal only
   (`bc_rubric_scores`), structurally omitted from `bc_digest_rows()` and never rendered
   (CI-guarded). Headline AUC 0.810 rests on 9 CRLs; the reproduced CI floor is **0.594 <
   0.70** — so the **band is a caveated secondary tag, never a primary sort key**. Current
   live output is band-skewed to `low` (coverage-limited on pending names) — treat the
   band as a soft prior, not a verdict.
2. **No market-implied-move / options-IV surface.** Polygon options are entitlement-blocked
   (403, confirmed). v1 is **band-only**: the digest **omits the implied-move column
   entirely** (it never renders "unavailable"). The "framed vs the market-implied move"
   differentiator therefore **does not exist in v1** — do not market it until v1.1.
3. **No refit / feedback loop.** The outcome labeler (P4) is **LOGGING ONLY** — it writes
   `bc_prediction_outcomes` and NEVER touches `bc_refit_log` / `l7.*` (enforced by
   `test_run_labeler_never_touches_refit_tables`). 26 CRLs/yr < the `l7.refit_min_crl_events=30`
   floor, so a refit is **not earned**; the dial stays unread.

## v1.1 path (flag-flip, no code change)
Buy a Polygon options tier (~$29–199/mo) → `UPDATE bc_config SET value='true' WHERE
key='l4.options_enabled'` → the dormant options/IV stream activates and the digest's
implied-move column lights up (synthesis already carries the dormant fields). Re-probe the
403 with a single `get_chain()` before flipping.

## Reliability posture
- **Liveness:** every cron/task opens+closes a `bc_pipeline_runs` row (digest, monitor,
  labeler). A missing today-row = the alert.
- **Single-host Cowork** is the LLM-side liability (the monitor classify/synthesis runs on
  Pedro's Mac Cowork session). Mitigated by the **`bc_cowork_stale` freshness guard** (P3):
  if synthesis is older than ~26h, raise the flag and the digest falls back to the
  deterministic render. The deterministic digest + band never depend on Cowork.
- **Recipients** are bc-owned (allowlist / dev override), **decoupled from v3
  `notifications_prefs`** — the v3 user pool is never queried in warm-up (test-enforced).
