# FDA PDUFA Pipeline — Diagnostic
**Tool**: `tools/fda_pdufa_pipeline.py` v2.0
**Grade**: **A−** — most feature-rich scanner; small hygiene gaps

---

## What it does (verified)
1. Maintains a JSON watchlist of known PDUFA dates (`signals/pdufa_watchlist.json`).
2. Auto-discovers new PDUFA dates by searching EDGAR 8-K filings with regex (`discover_pdufa_from_edgar()`).
3. Cross-checks active watchlist entries against openFDA approvals database (`run_approval_crosscheck()`) to catch early approvals that would make entries stale (e.g., CORT Mar 25 2026).
4. Enriches entries on demand with ClinicalTrials.gov v2 trial data and openFDA approval history (`enrich_watchlist()`).
5. Emits signals for entries with `days_until_pdufa` ≤ 90 days (watchlist) or ≤14 days (active), with strength boosted by trial data, resubmission status, favorable AdCom votes.

## Current health (verified)
- **Today's live-run (S56, 2026-04-14)**: 10 watchlist signals (AXSM, MNKD, ARVN, PFE, LNTH, ARQT, IONS, AZN, VRDN, VERA). Wall-clock 26.9 s.
- **Compilation**: clean.
- **APIs**: ClinicalTrials.gov 200, openFDA 200.
- **Recent wins**: TVTX PDUFA (Apr 13) correctly approached T-4 imminent; cross-check caught CORT early approval; auto-discovery added entries without manual intervention.

## What's working (verified)
- **Watchlist corruption guard** (`MIN_EXPECTED_ENTRIES = 20`) — if file loaded with <20 entries, auto-discover won't save changes. Defensive design after past incidents.
- **Auto-discovery dedup** (two layers) — exact ticker+drug match OR auto-discover-blocks-any-existing-entry for that ticker.
- **DISQUALIFIED_TICKERS** (D-039) — static list with clear reasons. Prevents known-FP from reappearing (ZLAB, CORT, ORCA).
- **run_approval_crosscheck** (D-046) — querying openFDA each scan catches early approvals. Bounded by `max_checks=10` + `total_timeout=30s` to prevent scanner timeout. Well designed.
- **Imminent boost** (days ≤7 → strength +1, capped at 5).
- **Strength scoring aggregates multiple evidence sources** — base 2 + trial data + completed trial + resubmission + strong AdCom vote.

## Known issues (verified/inferred)

### Empty watchlist-table bug in today's report
Today's `reports/2026-04-14_daily_report.md` line 88–91:
```
## PDUFA Watchlist

| Ticker | Drug | PDUFA Date | Status |
|--------|------|-----------|--------|
## Next Steps
```
The table header emitted but zero rows. `run_post_scan.py` is the culprit, not this scanner — but inspected together they live in the same pipeline. Bug likely in post-scan report generation, not here.

### Regex fragility in PDUFA date extraction
`_extract_pdufa_date_from_filing()` uses two regex patterns that match phrases like "PDUFA ... action date of [Month Day, Year]". This is brittle:
- Some 8-Ks write "target action date of June 30, 2026" (handled).
- Others write "expected FDA decision in the second half of 2026" (won't be extracted — correct, too vague).
- Others write dates in (e.g.) "3Q26" format (missed).
- Some corporate releases use non-ASCII em-dashes around dates (missed).
- **Impact**: auto-discovery is conservative (misses some valid dates). Not a false-positive risk.
- **Fix**: add a third pattern for "expected ... by/in [Month Year]" — coarser but catches the vague cases with lower strength. ~30 min.

### Strength scoring ceiling effects
- `_assess_strength()` caps at 5. Many candidates hit strength 3 quickly (base 2 + trial data) and only edge to 4 with trial completed. The 7-dimension rubric would benefit from finer gradations.
- **Inferred**: Today's report shows all 10 FDA signals at strength 2 (low). VRDN and AXSM in watchlist both had trial data — so either `enrichment` is empty or `_assess_strength` isn't being called per entry. Worth a trace.
- **Fix**: verify enrichment runs populated `entry["enrichment"]["trial"]` for active entries. Possible drift between `enrich_watchlist()` (manual `--enrich` CLI flag) and the scan-time strength assessment that expects `enrichment` to be present.

### DISQUALIFIED_TICKERS static (no expiry)
- CORT will stay disqualified forever even though future indications could revive it. Add optional `expiry_date` field.
- Same structure could hold `ZLAB` (China NMPA milestones — if US PDUFA ever set, it'd be valid).
- Low priority. Maybe one clean-up every 6 months.

### Watchlist freshness
- `pdufa_watchlist.json` entries have an `added_date`. No audit exists for "entries older than 6 months without enrichment refresh."
- **Fix**: add a staleness flag; surface in report if entries haven't been enriched in ≥60 days.

## Data-structure observations (verified from source)
- Watchlist entry schema: 15 fields including `enrichment` (dict), `status` enum, `is_resubmission` bool.
- Signal raw_data: drug_name, indication, pdufa_date, days_until_pdufa, nda_type, is_resubmission, adcom_date, adcom_vote, application_number, notes.
- Signal URL: ClinicalTrials.gov NCT link if available, else FDA.gov.

## What to build next (ranked)

**P1** (next session):
1. **Trace why today's signals are all strength 2**. Verify `_assess_strength` is reading populated enrichment. Might be a simple bug fix.
2. **Add staleness flag** to watchlist (60-day enrichment refresh audit).

**P2** (1–2 weeks):
3. **Expiry on DISQUALIFIED_TICKERS** (30 min).
4. **AdCom calendar integration**. FDA publishes AdCom schedules (public). A scheduled AdCom 1–4 weeks before a PDUFA is high-signal — the vote is publicly reported and often predicts the outcome. Currently populated manually per entry.
5. **Competitor surveillance**. Per D-034 + working/session26_vrdn_reveal_impact.md, competitor Phase 3 topline readouts can invalidate a PDUFA candidate's franchise thesis (e.g., Amgen's elegrobart SC readout broke VRDN). Currently manual. Lightweight automated monitor: scheduled weekly search per active candidate.

**P3** (speculative):
6. **Options-IV mispricing scan** — single most-valuable PDUFA signal per the strategy spec. Requires paid options data (Polygon, Tradier). Out of free-data budget.
7. **KOL-sentiment scanner** — biotech Twitter / Seeking Alpha commentary aggregation. Noisy, but worth a light prototype.
8. **Historical PDUFA outcome database** — for precedent drug analysis. openFDA has the data; we just need to synthesize it (approval rates by sponsor size, first-in-class vs. follow-on, resubmission-after-CRL base rates).

## Signal quality context
FDA PDUFA has produced the most candidates overall (VRDN, VERA, AXSM, TVTX, ARVN in recent history). The CORT early-approval incident (S40) was a genuine data-quality miss, now fixed. TVTX PDUFA approval capture (S56 today) is the clearest win: caught the T-4 signal, sustained approach, captured the Apr 14 intraday gap even with yfinance 24h lag.

## Synergy hooks
- **EDGAR M&A + FDA PDUFA** — biotech gets acquired right before PDUFA is a classic pattern. Convergence engine should detect.
- **ESMA shorts + FDA PDUFA** — binary catalyst with crowded short = maximum asymmetry. Also convergence territory.
- **Congressional HELP committee + FDA PDUFA** — weaker signal (HELP oversight is legislative, not approval process) but worth capturing.

## Verification notes
- Source code read in full (985 lines); all structural claims verified.
- Today's run data verified against `reports/2026-04-14_daily_report.md`.
- Watchlist-table-empty bug verified by direct read of today's report (lines 88–91).
- TVTX + VRDN + CORT history verified against SESSION_STATE.md, DECISIONS references, OPEN_QUESTIONS.md.
