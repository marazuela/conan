# Unified Maintenance Run — 2026-04-17

- Window: 2026-04-17T16:55:21Z → 2026-04-17T16:57:17Z (UTC)
- Lock: acquired `unified-maintenance`, released cleanly (no conflict with `unified-operational`).

## Steps

1. **Pre-warm LSE cache** — SKIPPED. `tools/lse_rns_scanner.py` does not expose `--maintenance` or `--prewarm-cache` (only `--window` and `--dry-run`). Per task guidance, skipped rather than invented.
2. **OpenFIGI cache refresh** — NO-OP. Cache contains 134 entries (all ~1 day old). Cache tickers are JPX/LSE/ASX keyed by `<code>_<MIC>`; signal_log.json currently holds 108 distinct symbols, predominantly BSE/NSE/HKEX/KRX. Zero intersection, and no entries are >7 days old, so nothing to touch.
3. **10Y UST rate check** — NO UPDATE. Pulled `home.treasury.gov` daily yield curve CSV (2026 all). Most recent published: 04/16/2026 at 10Y = 4.32%. `RISK_FREE_RATE = 0.043` (4.30%) in `tools/run_post_scan.py:103` — delta is 2 bps, not material. Left value as-is.
4. **Prune convergence_report_*.json >14d** — NO-OP. Only one file present (`convergence_report_2026-04-16.json`, 1 day old).
5. **Pipeline dry-run** — OK. `tools/pipeline_runner.py --dry-run` returned `not_due` for all 15 scanners (edgar_filing_monitor, esma_short_scanner, fda_pdufa_pipeline, congressional_trading, lse_rns_scanner, tdnet_scanner, asx_scanner, sedar_plus_scanner, hkex_scanner, kind_scanner, bse_nse_scanner, cvm_scanner, bmv_scanner, courtlistener_scanner, sec_enforcement_scanner). No dispatch errors.
6. **Reports dir** — not touched (per task directive).

No scanners were triggered. Lock released.
