# SESSION_STATE — Non-US Primary-Source Discovery System (Tool 2)

## TOP HEADLINE

Operational cycle 2026-04-16T16:26Z: **lse_rns window=1 → 36 raw / 2 watchlist (ATYM + SEIT, both major_shareholder_change @ 23.0), 0 immediate. tdnet window=3 → 135 raw / 1 triaged / 0 resolved (FIGI-resolve defect recurred, 4th cycle — now confirmed 364A0.T alphanumeric mapping 404). asx skipped (already finalized at 01:38Z earlier this UTC date, cursor 426/426, finalize stage=done). sedar still blocked on missing `working/ca_universe.json`. No new candidate markdowns; 2 new watchlist JSONs.**

Prior cycle 2026-04-16T23:56Z (earlier today): ITRK.XLON candidate markdown written for EQT Rule 2.4-style possible offer.

## Current phase

**Phase 3 (Australia) steady-state operating.** Phase 4 (Canada SEDAR+) code complete, still blocked on `working/ca_universe.json` file.

## Maintenance 2026-04-16T13:47Z — GREEN

All 4 active scanners healthy: lse_rns 200 (511 ms), tdnet 200 (1,466 ms), asx 200 (986 ms), sedar (yfinance `.news`) 200 (1,547 ms on SHOP.TO probe). `py_compile tools/*.py` clean. Deps reinstalled (yfinance 1.2.2, pandas 2.3.3, requests 2.33.1). Signal log 166 entries, 0 dup hashes, 0 orphans. OpenFIGI cache 133 files, all resolved. ASX universe 1.9 days old (fresh). 23 candidates, 0 true drift. No new boilerplate rules recommended. See HEALTH_LOG.md.

Next maintenance concrete action: **Re-probe HKEx and BMV endpoints with real URLs once Phase 5 / Phase 9 scanner modules land.** Until modules exist, skip those probes (current behavior). Secondary: once `working/ca_universe.json` is built, add sedar cache-hit-rate to the per-scanner audit. Optional: stamp `resolver_cache_hit: bool` on resolved signals in `tools/openfigi_resolver.py` for per-scan observability. New: add LSE `all-data` cache warm-up to maintenance so operational LSE window-3/7 runs don't hit the cold-sandbox wall.

## This cycle — new artifacts (2026-04-16T16:26Z operational)

- `candidates/watchlist/ATYM_XLON_2026-04-16.json` — **NEW** (lse_rns watchlist, major_shareholder_change, score 23.0).
- `candidates/watchlist/SEIT_XLON_2026-04-16.json` — **NEW** (lse_rns watchlist, major_shareholder_change, score 23.0).
- `signals/lse_rns_2026-04-16_processed.json` — overwritten (2 watchlist signals after novelty dedup; 0 immediate).
- `signals/tdnet_2026-04-16_processed.json` — empty (1 triaged survivor dropped at OpenFIGI resolve — 404 on `364A0.T`).
- No new candidate markdowns this cycle.

### Prior cycle artifacts (2026-04-16T23:56Z — still current)

- `candidates/ITRK_XLON_eqt-possible-offer.md` — lse_rns immediate, score 28.0; EQT Rule 2.4-style possible offer for FTSE 100 TIC company Intertek Group; two related signals 11:37Z + 13:39Z merged into one candidate under D-004.
- `signal_log.json` appended with 13:39Z ITRK immediate (signal_id 52c36d90dd51433b1d2caffde1fa6564).
- No new watchlist JSONs this cycle (9 LSE watchlists already written at 06:50Z cycle, all novel-deduped today).
- `PROGRESS_LOG.md` appended with 2026-04-16T23:56Z session entry.

## Next concrete action

1. **Deep-dive ITRK.XLON** — top priority. PUSU deadline ~2026-05-14 (default 28 days under UK Takeover Code). Determine: (a) undisturbed price pre-leak, (b) any indicative offer price from EQT, (c) competing-bidder risk (TIC industry consolidation), (d) board disposition, (e) antitrust complexity. Position sizing guidance is 1–3% satellite given 2.4→2.7 historical conversion rate is ~55%.
2. **Build `working/ca_universe.json`** — invoke `python3 -m tools.ca_universe --throttle 0.2 --boards tsx,tsxv` so SEDAR+ scanner can produce non-zero raw signals. Standing #1 blocker.
3. **Investigate tdnet triage→resolve drop (now 4 cycles in a row — PROMOTE PRIORITY):** window=3 again produced 1 survivor that dropped at OpenFIGI resolve. Confirmed today's 404 is on `364A0.T` (another alphanumeric 5-char ticker — same family as `469A0`). Fix `ticker_local`→`.T` normalization in `tools/openfigi_resolver.py` or upstream in `tools/tdnet_scanner.py`: strip trailing `0` when the ticker is 5 chars AND position 4 is a letter.
4. **LSE wall-clock in sandbox:** lse_rns window=3 and window=7 both timed out this cycle at 44s budgets; window=1 ran cleanly and produced 35 raw / 1 immediate. Maintenance should pre-warm LSE `all-data` cache so operational runs can use wider windows again.
5. **Patch `tools/tdnet_scanner.py` thesis_direction classifier** for buyer-side TOB cases (8001 Itochu defect). Did not resurface this cycle.
6. **Begin Phase 5 (Hong Kong HKEx)** — scanner module `tools/hkex_scanner.py` not yet built.

## Steady-state confirmations

- ASX chunked scanner stable at chunk=40, throttle=0.04.
- tdnet pipeline with `--window 3` fits cleanly inside a 44s bash budget.
- lse_rns pipeline with `--window 1` fits in 44s; `--window 3/7` does NOT on cold sandbox (first-time `all-data` fetch is the bottleneck).
- yfinance not installed in every sandbox — always reinstall at cycle start.
- Novelty dedup compressing fresh-immediate counts for mature scanners — expected; real signals still surface (ITRK is today's example — Rule 2.4 was a genuinely new event at 11:37Z that was not already logged).
- Foreground bash + `timeout 44` + chunked mode remains the durable run pattern.
- Reporting Hub remains read-only consumer of this system's state.

## Active candidates (24 total, +0 this cycle)

| ID | Score | Status | Notes |
|----|-------|--------|-------|
| ITRK_XLON | 28.0 | candidate (new) | eqt-possible-offer (Rule 2.4; 2.4→2.7 conversion risk ~55%) |
| 1878_XTKS | 35.0 | candidate | daito-trust-global-tender-offer-correction |
| PDI_XASX | 28.0 | candidate | predictive-discovery-robex-merger-update |
| WBC_XASX | 30.0 | candidate | westpac-hy2026-items-impacting-results |
| PTSB_XLON | 33.0 | candidate | bawag-recommended-cash-offer |
| 9601_XTKS | 31.0 | candidate | shochiku-osaka-building-impairment |
| 2972_XTKS | 35.0 | candidate | sankei-real-estate-tender-offer |
| 6058_XTKS | 31.0 | candidate | vector-special-losses |
| CCL_XASX | — | candidate | cuscal-institutional-placement |
| (+15 prior XTKS candidates) | | | |
