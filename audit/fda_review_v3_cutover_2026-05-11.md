# FDA Agent-Review v3 Cutover Audit — 2026-05-11

One-shot, read-only measurement pass. No skill files, scanner rows, or scheduled
tasks were modified. Window: 30 UTC days ending 2026-05-11.

## Pre-flight checks

- D-128 present in `DECISIONS.md` (line 636: "Phase 4B foundation: Tier-2
  (Cowork bulk) runtime harness + `tier` column on convergence_assessments").
- Quota = 10/UTC day in all three skill frontmatters (regulatory, medical,
  microstructure). No drift.
- Supabase MCP reachable (project `xvwvwbnxdsjpnealarkh`).

## Measurements

### A — Per-kind cap binding rate (30d)

| agent_kind     | days_observed | days_hit_cap (c>=10) | days_under_5 | p50 | p95 | total_completed_30d |
|----------------|---------------|----------------------|--------------|-----|-----|---------------------|
| medical        | 1             | 1                    | 0            | 10  | 10  | 10                  |
| microstructure | 1             | 1                    | 0            | 10  | 10  | 10                  |
| regulatory     | 1             | 1                    | 0            | 10  | 10  | 10                  |

Only **one UTC day** in the 30-day window has any completed rows at all per kind.
Cap binding rate computed against observed days is 100%, but the n=1 sample
makes the "binding %" statistic non-actionable.

### B — Queue arrival vs drain (30d)

| agent_kind     | arrived_30d | drained_30d | arrived_vs_drained_pct_over | backlog_growing_flag |
|----------------|-------------|-------------|-----------------------------|----------------------|
| medical        | 32          | 10          | +220.0%                     | TRUE                 |
| microstructure | 32          | 10          | +220.0%                     | TRUE                 |
| regulatory     | 32          | 10          | +220.0%                     | TRUE                 |

All three kinds flag backlog-growing. But "drained_30d=10" came from a single
day's run (per A), not steady-state drainage — the queue isn't being worked the
other 29 days, which is a cron/dispatch problem, not a per-kind-cap problem.

### C — Per-event coordination gap (30d)

| events_with_any_completed_30d | events_full_coverage (3/3 kinds within 24h) | events_partial_coverage | full_coverage_pct | partial_coverage_pct |
|-------------------------------|---------------------------------------------|-------------------------|-------------------|----------------------|
| 14                            | 7                                           | 7                       | 50.0%             | 50.0%                |

50% partial coverage — above the 40% threshold for R2, but n=14 events.

### D — v3 overlap (30d)

| v2_completed_events_30d | v2_with_v3_match (tier 1 or 2, ±7d) | overlap_pct |
|-------------------------|--------------------------------------|-------------|
| 30                      | 9                                    | 30.0%       |

Below the 50% threshold for R3.

### E — v3 Tier-2 quality status

`SELECT * FROM operator_flags WHERE source='tier2_quality' AND resolved_at IS NULL`
→ **0 rows. Clean.** Tier-2 Brier is within threshold of Tier-1.

## Recommendation: R4 — INSUFFICIENT DATA

The 30-day window has only **30 completed rows total** across the three kinds
(below the 100-row R4 threshold), and those 30 rows landed in **one UTC day**
(measurement A), not distributed across the window. Every other downstream
inference — cap-binding rate, backlog-growth rate, coordination gap, v3
overlap — is contaminated by that single-day artifact. R1's "cap binding
>=30% of days" trigger is technically met (1/1 = 100%) but the population is a
sample of one and doesn't reflect steady-state. The hourly cron is not
producing the daily drains the cadence assumes.

The orthogonal signals are worth noting for the +14d rerun but should not
drive action now:

- E is clean (Tier-2 healthy), which is the right precondition for an
  eventual R3 (deprecate toward v3) — but D=30% is well short of the 50%
  threshold and based on only 30 v2-completed events.
- B's "backlog growing" flag is a measurement artifact of single-day drainage,
  not a true arrival-vs-throughput problem.

## Concrete next action

**Set a measurement bookmark for +14 days: 2026-05-25.**

Rerun this same prompt on that date. The intent is to let two more weeks of
hourly cron dispatches accumulate so the per-day distribution in A is wider
than n=1, the B arrived-vs-drained ratio reflects actual throughput, and D's
overlap denominator gets large enough to discriminate between R3 and "stay
the course."

No file changes. No cron changes. No scanner-row changes. No follow-up
enqueued — the rerun is on-Pedro to trigger.

## Open questions for Pedro (≤3)

1. **Why is the hourly cron only producing one day of completed drains in
   the past 30 days?** The cadence design (10/UTC day × 3 kinds × hourly
   wake-ups) should yield daily activity if the queue has rows — and B shows
   32 arrivals per kind. Is the Cowork scheduled task for one or more of
   these three skills paused, or is the hourly fire no-op'ing on something
   other than the quota check?
2. **Is the single-day burst (the only day with any completions) a backfill
   or a real production day?** If it was a manual catch-up run, the 30-row
   sample is even less representative than it looks.
3. **Should the +14d rerun bookmark be calendar-scheduled, or do you want
   to drive it manually?** The original prompt says "do not enqueue
   follow-ups" — I am leaving it to you, but want to confirm that's still
   the right call given the cron is the suspect.
