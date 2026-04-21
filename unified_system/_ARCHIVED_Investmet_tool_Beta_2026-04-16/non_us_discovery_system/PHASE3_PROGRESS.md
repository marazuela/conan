# Phase 3 (Australia / ASX) — Build Progress Log

**As of:** 2026-04-14
**Status:** COMPLETE ✅ — end-to-end pipeline produced candidate markdown.

## Summary

- Universe built: `working/asx_universe.json` (426 tickers ≥ $300M USD).
- Scanner built: `tools/asx_scanner.py` (30+ headline patterns).
- Rubric seeding built: `tools/asx_rubric.py` (baseline + signal-type overrides + price-sensitive / small-cap modifiers).
- End-to-end run (2026-04-14):
  - Raw signals: **149** across 426 tickers
  - Post-triage: 60 (89 boilerplate drops)
  - Post-resolve (OpenFIGI): 60 / 60
  - Post-dedup: 59
  - Routing: **5 immediate, 17 watchlist, 37 archive, 0 discard, 0 manual_review**
- First XASX candidate markdown produced:
  - `candidates/WBC_XASX_westpac-hy2026-items-impacting-results.md` (score 30)

## Completed

1. **Endpoint validation.** `https://asx.api.markitdigital.com/asx-research/1.0/companies/{TICKER}/announcements` (5-announcement cap; no firehose).
2. **Strategy doc.** `strategies/strategy_au_asx.md` reflects the universe-enumeration strategy.
3. **Universe enumerator.** `tools/asx_universe.py` — fetches ASX CSV, pulls market cap via yfinance, AUD→USD conversion, 7-day TTL cache.
4. **Scanner.** `tools/asx_scanner.py` — 30+ headline patterns, `isPriceSensitive` boost, emits the common signal schema AND rubric_scores (via `asx_rubric.py`).
5. **Rubric seeder.** `tools/asx_rubric.py` — ASX-specific 7-dimension rubric (English market, so info_asymmetry baselined low; small-cap bump; price-sensitive edge-decay bump).
6. **Chunked runners.** `tools/asx_chunked_scan.py` + `tools/asx_finalize.py` — on-disk checkpointing so the 426-ticker scan + OpenFIGI resolve can be completed across multiple 45-second bash windows without nohup.
7. **Pipeline plumbing.** `tools/pipeline_runner.py` now forwards `--max-tickers` and `--throttle` to scanners whose signature accepts them.
8. **Scheduled tasks registered** per INSTRUCTIONS §9:
   - `non-us-operational` (cron `20 */3 * * *`)
   - `non-us-maintenance` (cron `40 */3 * * *`)
   - `non-us-performance-report` (cron `45 1 * * *`)
   - `non-us-deep-dives` (cron `45 */4 * * *`)

## Top signals from the 2026-04-14 run

| Ticker | Score | Route | Signal type | Headline |
|--------|------:|-------|-------------|----------|
| WBC | 30.0 | immediate | results_items_impacting | Items Impacting Half Year 2026 Results |
| PDI | 29.0 | immediate | merger_agreement | PDI & Robex Merger Proceeding to Implementation |
| PDI | 29.0 | immediate | merger_agreement | Merger Implementation Timetable |
| RXR | 29.0 | immediate | merger_agreement | PDI & Robex Merger Proceeding to Implementation |
| RXR | 29.0 | immediate | merger_agreement | Merger Implementation Timetable |
| SMI | 27.5 | watchlist | equity_placement | Completion of Tranche 2 Placement |
| TBN | 26.0 | watchlist | rights_issue | Retail Entitlement Offer Information Booklet |
| TVN / CCL | 23.5 | watchlist | trading_halt | Trading Halt |

(PDI and RXR converge on the same merger event — candidates for D-001 cross-listing dedup or D-004 convergence merge; current code does not see them as same-issuer because they are two legs of the merger, not two listings of the same issuer.)

## Known limitations

- 5-announcement-per-ticker cap means low-volume filers may miss items older than the 5-ann window.
- `raw_data.url` is empty from the API; PDFs must be fetched from `asx.com.au/markets/company/<T>` at deep-dive time.
- Universe rebuild still requires ~6 minutes of yfinance calls; TTL 7 days means this runs weekly, NOT per-scan. Maintenance task will trigger rebuild when cache > 6 days old.
- `substantial_holder_change` emits a lot of low-value noise; could benefit from a holder-size threshold filter in `asx_scanner.py` in a future pass.

## Files touched (this phase)

- `tools/asx_universe.py` — created (rewritten cleanly in the last session after OneDrive sync lag corrupted on-disk copy)
- `tools/asx_scanner.py` — created + rubric_scores emission added
- `tools/asx_rubric.py` — created (new)
- `tools/asx_chunked_scan.py` — created (new; chunked runner with on-disk checkpoints)
- `tools/asx_finalize.py` — created (new; stage-based finalize with OpenFIGI resolve checkpointing)
- `tools/pipeline_runner.py` — added `_filter_scanner_kwargs` + `--max-tickers` / `--throttle` CLI args
- `strategies/strategy_au_asx.md` — updated
- `candidates/WBC_XASX_westpac-hy2026-items-impacting-results.md` — created (first XASX candidate)
- `signals/asx_2026-04-14_processed.json` — created (59 scored signals)
- `signals/signal_log.json` — appended 59 entries
- `working/asx_universe.json` — built (426 tickers)
- `working/asx_chunked_state.json` — created (scanner checkpoint; can be deleted after consumption)
- `working/asx_finalize_state.json` — created (pipeline checkpoint; can be deleted after consumption)

## Next phase

Phase 4 — Canada SEDAR+. Build `tools/sedar_scanner.py` per the contract:

```
fetch_raw_signals(window_days: int, **kwargs) -> list[dict]
```

Register in `SCANNER_REGISTRY` of `pipeline_runner.py`. Canadian filings on SEDAR+ cover two MICs — XTSE (Toronto) and XTSX (TSX Venture). Both should be emitted from the one scanner. Reference: `strategies/strategy_ca_sedar.md` for endpoint details.

Mark Phase 4 complete once one SEDAR+ candidate markdown exists.
