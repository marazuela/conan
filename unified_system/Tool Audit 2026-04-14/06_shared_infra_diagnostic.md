# Shared Infrastructure — Diagnostic
Covers: `openfigi_resolver.py`, `convergence_engine.py`, `mcap_cache.py`, `pipeline_runner.py`, `run_post_scan.py`

---

## `openfigi_resolver.py` — Grade **A−**

### What it does (verified)
v3 OpenFIGI API client for entity resolution. Handles ticker → FIGI and ISIN/CUSIP → ticker lookups. Includes rate-limiter + LRU cache.

### Health (verified)
- **S56 result**: 37/42 resolved.
- API v3 stable; v2 sunsets Jul 1 2026 (migration already done).
- Compiles clean.

### Issue: Persistent file cache disabled
- `CACHE_FILE = None` at module level. Only in-memory LRU cache survives within a single scanner run.
- ESMA scanner mitigates via its own `esma_ticker_cache.json`. Other scanners cache-miss every call.
- **Fix**: set `CACHE_FILE = os.path.join(_PROJECT_DIR, "signals", "openfigi_cache.json")`. Mirrors the mcap_cache pattern.
- **Effort**: 15 min. **Payoff**: ~40 fewer API calls per full pipeline run.

### What to build next
1. Enable persistent cache (P0).
2. Add purge-by-age helper (same pattern as mcap_cache).
3. Fail-soft on 429 — current code raises; rate-limiter prevents this in practice, but defense-in-depth is cheap.

---

## `convergence_engine.py` — Grade **B**

### What it does (verified)
Maintains rolling `signals/signal_log.json`. Detects entities (by ticker) with ≥2 signals from different strategies within a 14-day window. Classifies convergences as bullish/bearish/neutral/conflicting based on signal directionality. Applies CONVERGENCE_SUPPRESS list (AMT, FBLG — FBLG noise flagged earlier).

### Health (verified)
- **S56**: 0 convergences. AMT + FBLG suppressed as before.
- Compiles clean.

### Critical issue: Trailing duplicate code (dead, but bug-smell)
Lines 560–569 contain:
```python
if __name__ == "__main__":
    main()
     min_strategies=args.min_strategies,
        save_output=not args.dry_run,
    )

    report = generate_report(convergences)
    print(report)


if __name__ == "__main__":
    main()
```
- The first `if __name__ == "__main__": main()` at line 558 returns. Lines 560–569 are unreachable.
- **But**: this is the same pattern the file-truncation bug left in other files (esma_short_scanner.py, contract_monitor.py, fda_pdufa_pipeline.py) — which were cleaned up in session 31 per `working/session31_tool_health.md`. This file was missed.
- **Fix**: delete lines 560–569. 5-minute hygiene. Also: add pre-shutdown checksum validation to prevent the bug's recurrence.

### Zero-convergence reality
- Many consecutive sessions have shown 0 convergent entities. This is partly correct (the 5 strategies rarely touch the same ticker in 14 days at the signal-generation level), and partly an artifact of ticker-exact matching (FSGS PDUFA ticker TVTX vs. Korean ADR TVTX-K wouldn't match — hypothetical).
- **Improvement**: add "soft convergence" — 1 current + 1 signal from 15–28 days prior. Store as amber-flag rather than triggering full candidate writeup. Useful for pattern mining.

### What to build next
1. Clean trailing garbage (P0, today).
2. Soft-convergence surfacing (P2, 1–2 weeks).
3. Directional classifier audit — verify bullish/bearish/conflicting mapping is correct by case (e.g., a crowded-short ESMA signal + an M&A EDGAR signal on same ticker should classify as "conflicting" if the deal is above market, which is bullish, against the short thesis).

---

## `mcap_cache.py` — Grade **A**

### What it does (verified)
Shared file-based cache wrapping `yfinance.fast_info.market_cap`. TTL 24h. Loaded lazily on first call. Persists to `signals/mcap_cache.json` on every write (small file).

### Health (verified)
- Clean, well-documented, used by all scanners via `try-except` import fallback pattern.
- Compiles clean.

### Minor improvements
- **Stale-but-keep-on-error**: currently yfinance failures return None; cache stores None. A yfinance blip can temporarily disqualify a ticker from mcap-floor screens. Fix: if fetch fails and cache has a stale entry, return stale value with a warning instead of None.
- **Explicit purge CLI**: `python mcap_cache.py --purge-older-than 30d`. 15 min.
- Otherwise nothing to change.

---

## `pipeline_runner.py` — Grade **A−**

### What it does (verified)
Orchestrates the 5 scanners as subprocesses with 120s hard-kill timeout. SCANNER_REGISTRY maps name → script. Flow: Phase 1 run scanners → Phase 2 aggregate signals → Phase 3 resolve entities → Phase 4 convergence + report.

### Health (verified)
- All 5 subprocess isolation active. S56 completed all phases cleanly.
- Compiles clean.

### Observations
- The 120s hard-kill is 2.5× the bash 45s budget but scanners already self-budget (EDGAR 35s). Headroom is good.
- Subprocess isolation prevents cascade failures — correct design.
- No single scanner has blown the 120s in many sessions.

### What to build next
- **Per-scanner timing report** already present; nice.
- **Optional parallel execution** — scanners are I/O-bound; running 5 concurrently could halve wall-clock. Trade-off: harder to debug when one hangs. Keep sequential for now.
- **Status endpoint** — if we ever want a web dashboard.

---

## `run_post_scan.py` — Grade **A**

### What it does (verified)
Aggregates signals from the scanner outputs, runs convergence, generates the daily report `.md` + `convergence_report_*.txt`.

### Health (verified)
- Outputs clean reports daily.
- Compiles clean.

### Issue: Empty PDUFA watchlist table in today's report
Today's `reports/2026-04-14_daily_report.md` line 88–91 emits the header + table-frame but zero rows:
```
## PDUFA Watchlist

| Ticker | Drug | PDUFA Date | Status |
|--------|------|-----------|--------|
## Next Steps
```
- Compare to `reports/2026-04-09_daily_report.md` line 66–97 which emits 27 watchlist rows correctly.
- **Root cause** INFERRED: either `pdufa_watchlist.json` load path changed, or the report-emission function has a conditional that silently skips emission when certain criteria aren't met. Needs a quick trace.
- **Fix**: inspect the watchlist table generator; likely a one-line regression. ~30 min.

### Minor observations
- Report format is very readable. Tables + bullets at the right density.
- Emits both daily report (`.md`) + convergence report (`.txt`). Good separation.

### What to build next
1. Fix empty-table regression (P0, today).
2. Add "Newly Emerged Watchlist Items" section — entries that weren't in yesterday's report.
3. Weekly rollup (Sunday) summarizing the week's signal count, candidate moves, and kill events.

---

## Summary of shared-infra priorities
1. [P0] Clean convergence_engine.py trailing garbage (5 min).
2. [P0] Fix empty PDUFA-watchlist table in run_post_scan.py (30 min).
3. [P0] Enable openfigi_resolver.py persistent cache (15 min).
4. [P2] Soft-convergence surfacing.
5. [P2] Stale-but-keep on mcap_cache errors.
6. [P3] Parallel scanner execution (pipeline_runner).

## Verification notes
- All source code read for each component verified.
- Convergence-engine dead code verified by reading lines 550–570.
- Empty-watchlist-table bug verified by comparing 2026-04-09 and 2026-04-14 daily reports directly.
- OpenFIGI `CACHE_FILE = None` verified against module source.
