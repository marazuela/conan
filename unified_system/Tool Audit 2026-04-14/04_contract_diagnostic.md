# Government Contract Monitor — Diagnostic
**Tool**: `tools/contract_monitor.py` v1.1
**Grade**: **C** — runs cleanly but zero-signal for many recent sessions; bottleneck is the mapping table

---

## What it does (verified)
Polls USAspending.gov for federal contract awards ≥$25M over last N days (default 7). Sorts by Start Date desc to surface genuinely new awards (not legacy contracts with recent modifications — a bug-fix documented in-code). Matches recipient names to public-company tickers via `CONTRACTOR_TICKER_MAP` (hardcoded). Applies mcap floor ($300M). Emits standard pipeline signals classified as mega/very-large/large/award by dollar size.

## Current health (verified)
- **Today's live-run (S56, 2026-04-14)**: **0 signals**. 1.3 s wall-clock. USAspending POST 200.
- **Last N runs**: SESSION_STATE references "Contract 0" across multiple consecutive sessions. Signal is sparse.
- **Compilation**: clean.

## What's working (verified)
- **USAspending.gov POST API** stable, free, no auth — verified accessible April 2026.
- **Stale-award post-filter** — scanner correctly excludes contracts whose Start Date is >180 days old but has a recent *action* date (modifications). This was a real bug; the fix is clean.
- **Dollar-classification tiering** (`_classify_award_size`) — 1B/250M/50M thresholds mapped to strength 5/4/3.
- **UNMATCHED HIGH-VALUE logging at INFO** — scanner prints unmatched recipients ≥$50M. This is the right design: it surfaces mapping-table gaps for manual review.
- **Private-company exclusion** — tickers explicitly set to `None` in the map (e.g., Bechtel, Deloitte) → scanner skips them.

## Known issues (verified / inferred)

### Root cause of low yield: mapping-table coverage
- `CONTRACTOR_TICKER_MAP` has ~60 entries covering defense primes, big IT/cyber, some healthcare/infra. USAspending publishes hundreds of large awards weekly; most recipients are either private (acceptable miss) or small public contractors not yet in the map.
- **Every unmatched ≥$50M award that is actually public is a missed signal.**
- **Fix needed**: (a) scrape INFO-level logs from `logs/` across last 10–20 runs, (b) triage unmatched recipient names, (c) add real public companies to the map with a linked ticker. One-time effort: ~2–3 hours. Ongoing: ~15 min/week.

### Mcap floor mismatch with rest of pipeline
- Contract uses **$300M** floor. Other scanners use **$215M** (`MARKET_CAP_FLOOR_MM = 215`). This is a small inconsistency — awards to $215M–$300M mid-caps are disproportionately material (largest-to-revenue ratio) and we're screening them out.
- **Fix**: align floor to $215M. 1-line change.

### No materiality-vs-revenue scoring
- Scanner uses absolute dollar size. Strategy spec says: "Award > 10% of trailing-12-month revenue = highly material." Not currently computed.
- **Effort**: ~1 day — need a revenue source (yfinance has `info["totalRevenue"]`), careful with small-cap fresh-IPO revenue volatility.

### No contract-type discrimination
- New awards ("new work"), renewals, and option-exercises are all flagged with same strength (by dollar tier). Strategy spec says new work is the strongest signal.
- **Fix**: parse award description or contract type code more carefully. Medium effort.

## Data-structure observations (verified from source)
- `raw_data`: recipient_name, award_amount, awarding_agency, description (200-char truncation), start_date, award_type, award_id, matched_contractor.
- Source URL: `https://www.usaspending.gov/award/{internal_id}` — clickable, useful.
- Dedup key: `(recipient, award_id)` — tight.

## What to build next (ranked)

**P1** (next session):
1. **Align mcap floor to $215M** (1-line change, 1 min).
2. **Expand CONTRACTOR_TICKER_MAP** from recent UNMATCHED logs (2–3 hrs one-shot; biggest yield driver).

**P2** (1–2 weeks):
3. **Materiality-vs-revenue scoring** — strength boost when award/revenue > 10%. Uses yfinance.
4. **Contract-type weighting** — new work strength+1, renewal strength-0, option strength-1.
5. **Press-release gap detection** — scanner runs every 3 h. If USAspending shows the award today but the company's press-release feed is silent for 24 h, that's the information-asymmetry window. Flag it.

**P3** (speculative):
6. **Sector concentration heatmap** — weekly total dollar flow by awarding agency × sector. Provides macro context for individual signals.
7. **Competing-bidder inference** — if the award notes "full and open" + single bidder, that's unusual and may signal a classified program or a sole-source justification.

## Signal quality context
**INFERRED**: Contract monitor is currently pulling weight primarily as a convergence input (EDGAR + Contract defense name = stack). Zero-signal days are not a failure mode — they mean no ≥$300M-mcap public company won a ≥$25M award with a Start Date in the last 7 days that matched the map. Real bottleneck is the map. Fixing that should lift signal rate ~3–5× (speculated).

## Synergy hooks
- **Contract + Congressional (Armed Services)** — primary defense convergence. Tuberville / Fallon / Calvert trading defense primes in the same two weeks USAspending awards hit = highest-quality stack.
- **Contract + EDGAR earnings-surprise** — a company that wins a $500M+ contract two weeks before earnings has a high probability of beating guidance.
- **Contract + FDA** — when DoD/HHS contracts are for vaccines or medical countermeasures (e.g., HHS BARDA), they can cross into FDA-adjacent candidate territory.

## Verification notes
- Source code read in full; all structural claims verified.
- Today's zero-signal result verified against `reports/2026-04-14_daily_report.md`.
- "Zero-signal across many sessions" INFERRED from SESSION_STATE §Tool Health ("Contract 0") + general S56 summary.
- Mcap-floor discrepancy ($300M vs. $215M) verified against line 54 of `contract_monitor.py` and `MARKET_CAP_FLOOR_MM = 215` in other scanners.
