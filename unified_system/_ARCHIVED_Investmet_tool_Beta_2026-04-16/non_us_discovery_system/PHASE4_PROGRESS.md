# Phase 4 (Canada / SEDAR+) — Build Progress Log

**As of:** 2026-04-14
**Status:** CODE COMPLETE — awaiting sandbox availability for end-to-end run.

## Decision (operator confirmed 2026-04-14)

**Hybrid approach.** yfinance `Ticker.news` is the primary per-scan source; a once-daily Claude-in-Chrome pass against SEDAR+ "today's filings" supplements it to catch non-syndicated (especially French and TSXV small-cap) filings.

## Endpoint probe summary

| Source | Status | Role |
|--------|--------|------|
| `sedarplus.ca/csa-party/records/` | Blocked (PerfDrive JS challenge on raw HTTP) | Covered via Chrome supplement only |
| Per-issuer SEDAR+ profile pages | Blocked | Covered via Chrome supplement only |
| `newswire.ca` | Accessible but 215K-char HTML, no RSS/date-filter/pagination | Fallback firehose only; not wired |
| **yfinance `Ticker('<SYM>.TO').news`** | **Working — SHOP.TO probe returned 10 dated items** | **Primary per-scan source** |
| Globe Investor / BNN Bloomberg | Accessible, structured data inconsistent | Stage-2 deep-dive web-research layer |

## What shipped (2026-04-14)

1. **`tools/ca_universe.py`** — TSX + TSXV universe enumerator. TMX company directory (`https://www.tsx.com/json/company-directory/search/{board}/{letter}`, iterated A-Z + 0-9) → yfinance `.TO` / `.V` market-cap enrichment → CAD→USD conversion (yfinance `CADUSD=X`, fallback 0.72) → ≥$300M USD filter → cached 7 days in `working/ca_universe.json`. CLI: `python3 -m tools.ca_universe --throttle 0.2 --boards tsx,tsxv`.

2. **`tools/sedar_rubric.py`** — 7-dim rubric for Canadian signals. Baselines: info_asymmetry=2 for TSX large-caps; TSXV +1; NI 43-101 +2 (technical, specialist coverage); French filings +1 (translation/coverage barrier). D-002 gate applied when French with translation_confidence < 0.85. Signal-type overrides for takeover_bid_circular, plan_of_arrangement, cease_trade_order / MCTO, material_change_report, guidance, early_warning_10pct, NI 43-101 / NI 51-101, etc.

3. **`tools/sedar_scanner.py`** — yfinance news scanner. 35+ headline patterns in `SEDAR_TITLE_RULES` covering: takeover bid, plan of arrangement, directors' circular, material change, guidance up/down, early warning 10%, NI 43-101 / NI 51-101, MCTO, cease trade order, impairment, restatement, bought deal / private placement, NCIB / SIB buybacks, special dividend, dividend cut, going concern, covenant breach, CCAA, receivership, MD&A. Emits common schema + `rubric_scores`. Per-ticker throttle 0.3s.

4. **`tools/sedar_chrome_supplement.py`** — companion tool for the once-daily Chrome pass. Reads `working/sedar_chrome_inbox.json` (operator drops scraped rows via Claude-in-Chrome), classifies via the same ruleset, emits signals with `raw_data.source_type = "sedar_chrome_supplement"`. Pipeline-compatible `fetch_raw_signals(window_days, **kwargs)` entrypoint. The Chrome-side capture step is manual until we build a Cowork shortcut for it.

5. **`pipeline_runner.py`** — `SCANNER_REGISTRY` now has `sedar` → `tools.sedar_scanner` and `sedar_chrome` → `tools.sedar_chrome_supplement`. Both can be driven with `--scanner sedar` / `--scanner sedar_chrome`.

6. **`strategies/strategy_ca_sedar.md`** — updated with probe results table and the hybrid decision.

## Files touched

- `strategies/strategy_ca_sedar.md` — updated
- `tools/ca_universe.py` — created
- `tools/sedar_rubric.py` — created
- `tools/sedar_scanner.py` — created
- `tools/sedar_chrome_supplement.py` — created
- `tools/pipeline_runner.py` — added `sedar_chrome` registry entry
- `PHASE4_PROGRESS.md` — this file

## What still needs to happen (next session)

**Blocker:** The Cowork bash sandbox returned "Workspace unavailable" on every probe in the 2026-04-14 session. The code is written and imports cleanly on paper, but was not exercised end-to-end.

When the sandbox is back:

1. Build the universe (~5 min, 7-day cache):
   ```
   cd "Investmet tool Beta/non_us_discovery_system"
   python3 -m tools.ca_universe --throttle 0.2
   ```
   Expected: 300-500 tickers above floor across TSX+TSXV.

2. Smoke-test the scanner on a small slice:
   ```
   python3 -m tools.sedar_scanner --window 7 --max 20 --throttle 0.3
   ```
   Expected: some classified signals on the large-cap slice (SHOP, RY, TD, ENB, CNQ, etc.).

3. Full end-to-end run through pipeline:
   ```
   python3 -m tools.pipeline_runner --scanner sedar --window 7
   ```
   Chunked runner may be needed if full universe can't complete inside one bash window — copy the pattern from `tools/asx_chunked_scan.py` / `tools/asx_finalize.py` if so.

4. Produce at least one XTSE or XTSX candidate markdown.

5. Mark Phase 4 COMPLETE, update memory (`project_non_us_discovery_phase3.md` → rename to `_phase4.md`), advance to Phase 5 (HKEx).

## Known risks / TODO

- **TMX directory endpoint may rate-limit or change schema.** If the A-Z sweep returns empty, fall back to a static TSX listed-companies CSV (e.g. the public TMX listing stats).
- **yfinance news volume varies hugely by ticker.** Canadian large-caps get ~10 items/week; mid-caps may get 2-3. Small-caps sometimes return 0. The Chrome supplement exists to cover the gap.
- **TSXV may produce noise.** If it does, raise the TSXV strength threshold to ≥4 at triage time (per `strategies/strategy_ca_sedar.md` §3).
- **Chrome supplement is not yet wired to a schedule.** That's a Phase 10 concern — wait until all 9 scanners are active before registering the once-daily SEDAR+ Chrome task.
