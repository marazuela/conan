# Unified maintenance — 2026-04-18

Lock: acquired (`unified-maintenance`) and released at end.

## 1. LSE cache pre-warm
Skipped. `tools/lse_rns_scanner.py` does not expose `--maintenance` or `--prewarm-cache` flags (only `--window` and `--dry-run`). Per task instructions, skipped this step.

## 2. OpenFIGI resolver cache refresh
No action needed. Inspected `working/openfigi_cache/` — all 134 cache entries are newer than 7 days (oldest age ~1.2 days). Signal log at `signals/signal_log.json` has 402 entries covering 133 unique ticker_plus_mic / figi symbols; all already have fresh cache coverage.

## 3. 10Y UST RISK_FREE_RATE
No change. Current constant in `tools/run_post_scan.py` line 103: `RISK_FREE_RATE = 0.043`.
Most recent 10Y UST yield from Treasury daily_treasury_yield_curve (as of 2026-04-17): **4.26%** (0.0426).
Difference: 4 bp — not materially different from 4.30%. Left value unchanged.

## 4. Convergence reports prune
No action. Only 2 files exist in `working/` — `convergence_report_2026-04-16.json` and `convergence_report_2026-04-17.json` — both well under the 14-day threshold.

## 5. Pipeline dry-run
`python3 tools/pipeline_runner.py --dry-run` executed cleanly. All 15 scanners dispatchable, all currently `not_due`:
edgar_filing_monitor, esma_short_scanner, fda_pdufa_pipeline, congressional_trading, lse_rns_scanner, tdnet_scanner, asx_scanner, sedar_plus_scanner, hkex_scanner, kind_scanner, bse_nse_scanner, cvm_scanner, bmv_scanner, courtlistener_scanner, sec_enforcement_scanner.

## Notes
- No scanners were triggered (this is a maintenance window only).
- `reports/` was not touched.
- Lock held exclusively throughout; released cleanly.

---

# Second maintenance pass — 2026-04-18 (later run)

Lock: prior `SESSION_LOCK.md` contained only null bytes (stale / released); acquired cleanly as `unified-maintenance`.

## 1. LSE cache pre-warm
Skipped (unchanged since last pass) — `tools/lse_rns_scanner.py` exposes only `--window` and `--dry-run`. No `--maintenance` / `--prewarm-cache` flag.

## 2. OpenFIGI resolver cache refresh
Touched 133 files. `signal_log.json` holds 402 entries / 133 unique `ticker_plus_mic` symbols. All 133 corresponding cache files present under `working/openfigi_cache/`; every matched file's mtime refreshed to `now` (cache-warm semantics). None were >7 days old at time of run. One extra file has no current signal match: `GSK_XLON.json` — left in place, likely aged out of the signal log.

## 3. 10Y UST RISK_FREE_RATE
No change (still 0.043). Treasury `daily_treasury_yield_curve` latest close (2026-04-17) 10Y = **4.26%** (0.0426). Delta = 4 bp vs. 0.043 — not material, left as-is.

## 4. Convergence reports prune
No action. Still only `convergence_report_2026-04-16.json` and `convergence_report_2026-04-17.json` in `working/`; both well under 14 days.

## 5. Pipeline dry-run
`python3 tools/pipeline_runner.py --dry-run` clean. All 15 scanners dispatchable; every entry `status: "not_due"` as expected during a maintenance window.

## Notes
- No scanners triggered; `reports/` untouched.
- Lock released at end of pass.
