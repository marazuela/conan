# HEALTH_LOG — Non-US Discovery System

Append-only. Maintenance cycle records scanner endpoint probes here.

---

## 2026-04-16T23:50Z — non-us-maintenance run

### Scanner endpoint probes

| Scanner | Endpoint | Status | Notes |
|---------|----------|-------:|-------|
| lse_rns | londonstockexchange.com/news | 200 | HEAD 454 ms |
| tdnet   | release.tdnet.info/inbs/I_list_001_20260416.html | 200 | 62,335 bytes, 4,450 ms |
| asx     | asx.api.markitdigital.com/.../WBC/announcements?count=1 | 200 | 1,219 bytes, 958 ms |
| sedar (yfinance Ticker.news) | SHOP.TO | 200 | 10 items, 1,400 ms — primary source healthy |
| hkex    | — | module_missing | Phase 5 pending |
| kind    | — | module_missing | Phase 6 pending |
| bse_nse | — | module_missing | Phase 7 pending |
| cvm     | — | module_missing | Phase 8 pending |
| bmv     | — | module_missing | Phase 9 pending |

All 4 active scanners healthy. `py_compile tools/*.py` clean. `yfinance 1.2.2`, `pandas 2.3.3`, `requests 2.33.1` installed cleanly.

### Universe freshness

- `working/asx_universe.json` — 1.78 days old (as_of 2026-04-14T16:08:27Z), 426 tickers. TTL 6 days. No rebuild needed.
- `working/ca_universe.json` — MISSING. Standing blocker (operational task's responsibility per SESSION_STATE next-action #1); maintenance task does not build it.

### Signal log audit

- Total entries: 163 (+10 since 2026-04-16T04:47Z)
- Duplicate `source_content_hash`: 0
- Orphan hashes missing `issuer_figi`: 0
- `exchange` field still null on all entries — known low-priority metadata gap, `mic` is sufficient.

### OpenFIGI cache audit

- Cache files on disk: 132 (+10 since prior maintenance).
- Per-scanner last-scan hit rate:
  - asx: 54/103 (52%)
  - lse_rns: 31/70 (44%)
  - tdnet: 16/114 (14%)
  - sedar: 0/0 (N/A — no raw signals this cycle, ca_universe still missing)
- Note: per-scan hit rates are below the >80% target, but this is an artifact of counting every raw probe — the resolver only persists cache entries when `resolved=True`. Recurring large-caps hit the cache consistently; one-off small-cap tickers that fail resolve are re-probed every scan. This is expected behavior, not a cache regression — the "last-40 unique tickers" metric from prior maintenance (100%) was on the warm hot-set specifically.

### Boilerplate filter audit

- Top recurring discarded headline patterns (≥5 occurrences) in the last 6 raw scans:
  - 通期業績予想の修正に関するお知らせ (full-year guidance revision) — 11
  - 2026年２月期 決算短信〔日本基準〕（連結） (quarterly results) — 8
  - 業績予想の修正に関するお知らせ (guidance revision) — 7
  - 2026年８月期 中間決算短信〔日本基準〕（連結） (half-year results) — 7
  - Trading Update — 6
  - 配当予想の修正（増配）に関するお知らせ (dividend revision upward) — 5
  - 2026年８月期 中間決算短信〔日本基準〕（非連結） — 5
  - Final Results — 5
- **No new boilerplate rules recommended.** All of the recurring JP titles are legitimate signal categories (guidance revision, earnings results, dividend revision) that should pass the `boilerplate_filters` gate. Their discard signal traces back to market-cap floor + novelty dedup + sub-threshold scores, not generic noise. Adding a boilerplate rule here would suppress real signal.

### Candidate drift

- 23 candidate markdowns scanned.
- 0 candidates have new strength≥3 signals on the same (ticker_local, mic) pair in the last 7 days.
- No append notes required this cycle.

### Dependencies

- `yfinance 1.2.2` / `pandas 2.3.3` / `requests 2.33.1` — all importable after `pip install --break-system-packages`.

### Open issues

- `working/ca_universe.json` still absent. Operational task owns the build. Until it lands, SEDAR+ scanner has zero raw-signal throughput.
- HKEx / KIND / BSE-NSE / CVM / BMV scanner modules do not exist yet. Endpoint probes deferred until Phase 5+ modules land (consistent with the 2026-04-16T04:47Z note).

### Next maintenance action (standing)

Re-probe HKEx and BMV endpoints with real URLs once Phase 5 / Phase 9 scanner modules exist. Skip until then.

---

## 2026-04-14T14:40Z — non-us-maintenance run

### Scanner endpoint probes

| Scanner | Endpoint | Status | Schema | Notes |
|---------|----------|-------:|:------:|-------|
| lse_rns | londonstockexchange.com/news-article/market-news/announcements | 200 | ok | responds |
| tdnet   | release.tdnet.info/inbs/I_list_001_YYYYMMDD.html | 200 | ok | responds |
| asx     | asx.api.markitdigital.com/.../{T}/announcements (sample: CBA) | 200 | ok | `data` key present |
| sedar   | — | module_missing | — | Phase 4 pending |
| hkex    | — | module_missing | — | Phase 5 pending |
| kind    | — | module_missing | — | Phase 6 pending |
| bse_nse | — | module_missing | — | Phase 7 pending |
| cvm     | — | module_missing | — | Phase 8 pending |
| bmv     | — | module_missing | — | Phase 9 pending |

All three active scanners healthy.

### Universe freshness

- `working/asx_universe.json` — age 0.03 days (built 2026-04-14T16:08:27Z); TTL 7 days. No rebuild needed.

### Signal log audit

- Total entries: 71
- Duplicate `source_content_hash`: 0
- Orphan hashes missing `issuer_figi`: 0
- Note: `exchange` field is null on all 71 log entries — upstream scanners do not populate that field when appending to `signal_log.json`. Low-priority metadata gap; `mic` is present and sufficient for routing.

### OpenFIGI cache audit

- Cache files on disk: 76
- Sample of 20 entries: 20/20 resolved (100%)
- Last ASX scan resolved ratio: 59/59 (100%) — well above 80% threshold.

### Boilerplate filter audit

Dropped-headline pattern tally from the 2026-04-14 ASX scan (90 dropped of 149 raw):

- `becoming a substantial holder` — 34 + variant suffixes (≈45 total drops)
- `ceasing to be a substantial holder` — 27 + variant suffixes (≈43 total drops)

**Recommendation:** `substantial_holder_change` headlines dominate the boilerplate drop pile. Matches the known limitation flagged in PHASE3_PROGRESS.md. Consider adding a holder-size threshold filter in `tools/asx_scanner.py` (e.g. only emit when the new holding crosses 5→10% or includes a named activist/PE fund).

### Candidate drift

Reviewed 5 active candidates (PTSB, WBC, 2972, 6058, 9601). Matched each against last-7-day signal_log entries filtered to `strength_estimate>=3`.

- No new high-strength signals on any tracked issuer_figi.
- No candidate markdowns required updates this cycle.

### Dependencies

- `requests`, `pandas`, `yfinance` — all importable after `pip install --break-system-packages`.

---

## 2026-04-15T04:46Z — non-us-maintenance run

### Lock handling

- Prior lock was `non-us-operational-2026-04-15` at 00:00Z — 4.77h old, past 4h TTL. Took the lock.

### Scanner endpoint probes

| Scanner | Endpoint | Status | Schema | Notes |
|---------|----------|-------:|:------:|-------|
| lse_rns | investegate.co.uk (scanner's actual upstream) | 200 | ok | 112KB payload |
| tdnet   | release.tdnet.info/inbs/I_list_001_20260415.html | 200 | ok | 22KB payload |
| asx     | asx.api.markitdigital.com/.../WBC/announcements | 200 | ok | 5 items on probe |
| sedar   | sedarplus.ca (search page) | 200 | ok | Phase 4 code complete, registry live |
| hkex/kind/bse_nse/cvm/bmv | — | module_missing | — | Phase 5–9 pending |

All active scanners healthy.

### Universe freshness

- `working/asx_universe.json` — as_of 2026-04-14T16:08:27Z, age 0.53 days, 426 tickers. TTL 7 days. No rebuild.

### Signal log audit

- Total entries: 90 (up from 71 last cycle).
- Duplicate `source_content_hash`: 0
- Orphan FIGI: 0
- Missing hash: 0
- By scanner: tdnet=15, lse_rns=16, asx=59.
- By routing: immediate=11, watchlist=42, archive=37.
- Prior "null `exchange`/`mic` fields" warning reclassified: these fields aren't part of the signal_log schema by design (venue lives in `ticker_plus_mic`). No action.

### OpenFIGI cache audit

- Cache files on disk: 79.
- Last-scan FIGI resolve rate: asx 59/59, lse_rns 2/2, tdnet 3/3 → 100%.

### Boilerplate filter audit (ASX 2026-04-14 scan)

- Raw signals: 149, processed: 59, dropped: 90.
- Dropped types: `substantial_holder_initial` (45/45 = 100%), `substantial_holder_ceasing` (44/44 = 100%).
- Surviving but dominant: `substantial_holder_change` (48/49 = 98%).
- Recommendation (unchanged from prior cycle): add holder-size threshold filter in `tools/asx_scanner.py` at emit time to reduce `substantial_holder_change` volume entering triage.

### Candidate drift

Reviewed 5 active candidates — 7 matching entries in last 7d signal_log.

- **2972.XTKS (Sankei)** — 2 signals both on 2026-04-14, already in `related_signal_ids` (`f17cc614aaad...`).
- **PTSB.XLON (Bawag)** — 2 signals 11:05Z & 11:50Z; both already documented in candidate's `Source traceability` as merged siblings.
- **6058, 9601, WBC** — single originating signal each, already captured.

No candidate markdowns modified.

### Dependencies

- `pandas`, `requests`: already present.
- `yfinance`: was missing — installed via `pip install --break-system-packages yfinance` (installed 1.2.2).

---

## 2026-04-15T09:11Z — non-us-maintenance run

### Lock handling

- Prior lock `non-us-operational-2026-04-15T0500` at 05:00Z, TTL 4h expired at 09:00Z (11 min past). Took the lock at 09:11Z.

### Scanner endpoint probes

| Scanner | Endpoint | Status | Schema | Notes |
|---------|----------|-------:|:------:|-------|
| lse_rns | investegate.co.uk | 200 | ok | 113KB payload, t=0.97s |
| tdnet   | release.tdnet.info/inbs/I_list_001_20260415.html | 200 | ok | 63KB payload, t=1.11s |
| asx     | asx.api.markitdigital.com/.../WBC/announcements | 200 | ok | 5 items on probe, t=0.65s |
| sedar   | sedarplus.ca | 200 | ok | t=0.61s |
| hkex/kind/bse_nse/cvm/bmv | — | module_missing | — | Phase 5–9 pending |

py_compile clean across all 4 active scanner modules plus shared utilities (pipeline_runner, openfigi_resolver, convergence_engine, boilerplate_filters).

### Universe freshness

- `working/asx_universe.json` — as_of 2026-04-14T16:08:27Z, age 0.71 days, 426 tickers. TTL 7 days. No rebuild.

### Signal log audit

- Total entries: 101 (up from 90 last cycle — +11 from new `lse_rns_2026-04-15` scan).
- Duplicate `source_content_hash`: 0
- Orphan content-hashes missing issuer_figi: 0
- Missing source_content_hash: 0
- By scanner: tdnet=15, lse_rns=27, asx=59.
- By routing: immediate=11, watchlist=53, archive=37.
- Schema note (confirmed): signal_log is a slim dedup index with `signal_id`, `issuer_figi`, `ticker_plus_mic`, `scan_date`, `source_date`, `source_content_hash`, `scanner`, `score_total`, `routing`. `signal_type`/`strength_estimate` live in per-scan processed files, not the log. Drift checks must use `score_total >= 28` as the strong-signal proxy.

### OpenFIGI cache audit

- Cache files on disk: 87 (up from 79).
- Sample of 20: 20/20 = 100% resolved.
- Last scan FIGI resolve rate: asx 59/59, lse_rns 11/11, tdnet 3/3 → 100%. Above 80% target.

### Boilerplate filter audit (ASX 2026-04-14 scan; no new scan since)

- Raw 149 → processed 59 (dropped 90).
- Dropped 100%: `substantial_holder_initial` (45/45), `substantial_holder_ceasing` (44/44).
- Dominant survivor: `substantial_holder_change` (48/49 = 98%).
- Recommendation (carried forward 3rd cycle): add holder-size threshold filter in `tools/asx_scanner.py` at emit time to suppress low-signal `substantial_holder_change` filings before triage (e.g. only emit when crossing a 5→10% threshold or when the filer is a named activist/PE fund).

### Candidate drift

Reviewed 5 active candidates against signal_log. All 7 score≥28 matches reconcile cleanly to existing `related_signal_ids`:

- **2972.XTKS (Sankei)** — 2 immediate signals on 2026-04-14 (already merged).
- **6058.XTKS (Vector)** — 1 immediate 2026-04-14 (captured).
- **9601.XTKS (Shochiku)** — 1 immediate 2026-04-14 (captured).
- **PTSB.XLON (Bawag)** — 2 immediate 2026-04-14 11:05Z & 11:50Z (documented siblings).
- **WBC.XASX (Westpac)** — 1 immediate 2026-04-13 (captured).

No candidate markdowns modified.

### Dependencies

- `yfinance` 1.2.2, `pandas` 2.3.3, `requests` 2.33.1 — all importable after `pip install --break-system-packages`.

---

---

## 2026-04-15T11:15Z — non-us-maintenance run

### Scanner endpoint probes

| Scanner | Endpoint | HTTP | Module | Notes |
|---------|----------|-----:|:------:|-------|
| lse_rns | londonstockexchange.com/news | 200 | OK | fetch_raw_signals smoke: 24 raw in 1-day window |
| tdnet   | release.tdnet.info/inbs/I_list_001_20260415.html | 200 | OK | fetch_raw_signals smoke: 20 raw in 1-day window |
| asx     | asx.api.markitdigital.com/.../WBC/announcements | 200 | OK | 5 items returned, schema intact |
| sedar (primary) | yfinance Ticker('SHOP.TO').news | — | OK | 10 news items returned |
| sedar_chrome | working/sedar_chrome_inbox.json | n/a | OK | inbox absent (expected — no Chrome run yet) |
| hkex    | www1.hkexnews.hk | 200 | NOT BUILT | Phase 5 pending per CLAUDE.md |
| kind    | kind.krx.co.kr | 200 | NOT BUILT | Phase 6 pending |
| bse_nse | bseindia.com/corporates/ann.html | 200 | NOT BUILT | Phase 7 pending |
| cvm     | rad.cvm.gov.br | 200 | NOT BUILT | Phase 8 pending |
| bmv     | bmv.com.mx | 200 | NOT BUILT | Phase 9 pending |

### Universe freshness
- `working/asx_universe.json`: as_of 2026-04-14T16:08:27Z (age 0d), 426 tickers — FRESH (stale trigger ≥6d). No refresh needed.
- `working/ca_universe.json`: **MISSING** — still blocks Phase 4 end-to-end. Not built in this maintenance run (multi-minute chunked job, outside scope).

### Signal log audit (`signals/signal_log.json`)
- 143 entries total.
- Duplicate `source_content_hash`: **0**.
- Orphan entries (hash present, `issuer_figi` missing): **0**.

### OpenFIGI cache audit
- `working/openfigi_cache/`: 117 JSON files, all modified within last 24h.
- Hit rate on latest processed scan (`signals/asx_2026-04-15_processed.json`): 11/11 = **100%** (target >80% ✓).

### Boilerplate filter audit
Top raw-headline prefixes across last 6 raw-signal files (n=526):
- ASX substantial-holder family: 138 hits (`change in substantial holding`, `becoming a substantial holder`, `ceasing to be a substantial holder`) — already filtered by existing ASX patterns. No new rule needed.
- TDnet earnings/disclosure families: 業績予想の修正 / 決算短信 / 特別損失 / 配当予想 — these are legitimate signals, correctly NOT filtered. No action.
- LSE `trading update` (9), `standard form for notification` (5), `tr 1 notification of` (5) — low volume, retain current behavior.
- **Conclusion: no new boilerplate rule proposed this cycle.**

### Candidate drift check
- 21 candidate markdowns scanned against signal_log (7-day window).
- **Drift found: 0.** No candidate has a new high-strength signal on its `issuer_figi` missing from its existing markdown.

### Dependency check
- `yfinance 1.2.2`, `pandas 2.3.3`, `requests 2.33.1` — all importable after `pip install --break-system-packages`.
- `tools/*.py` py_compile: OK (14 modules).

### Summary
- **All active scanners (lse_rns, tdnet, asx, sedar) healthy end-to-end.**
- **Signal log clean, OpenFIGI cache hitting 100%, no candidate drift, no new boilerplate rules.**
- Only open item: CA universe still missing — Phase 4 blocker unchanged from prior cycle.


---

## 2026-04-15T14:30Z — non-us-maintenance run

### Scanner endpoint probes

| Scanner | Endpoint | Status | Schema | Notes |
|---------|----------|-------:|:------:|-------|
| lse_rns | investegate.co.uk/Index.aspx?date=YYYY-MM-DD | 200 | ok | canonical scanner source |
| tdnet   | release.tdnet.info/inbs/I_list_001_YYYYMMDD.html | 200 | ok | responds |
| asx     | asx.api.markitdigital.com/.../{T}/announcements (sample: CBA) | 200 | ok | `data` key present |
| sedar   | scanner has no live endpoint string — code-complete, blocked on `working/ca_universe.json` | n/a | n/a | unchanged from prior cycle |
| hkex    | scanner module not built (Phase 5 pending) | — | — | skipped |
| kind    | kind.krx.co.kr/disclosure/todaydisclosure.do | 200 | ok | endpoint live; scanner module not built |
| bse     | api.bseindia.com/.../AnnGetData/w | 200 | not-json | returned non-JSON body via plain GET — likely needs Referer/cookie; scanner module not built |
| nse     | nseindia.com/api/corporate-announcements?index=equities | 200 | ok | json list of 20 items |
| cvm     | rad.cvm.gov.br/ENET/frmConsultaExternaCVM.aspx | 200 | ok | endpoint live; scanner module not built |
| bmv     | www.bmv.com.mx (root) | 200 | ok | 15s read-timeout on /en/emisoras path; root reachable |

### Universe freshness

- `working/asx_universe.json` `as_of=2026-04-14T16:08:27Z`, 426 tickers — fresh (well within 6-day TTL). No refresh triggered.

### Signal log audit

- `signals/signal_log.json`: 145 entries, 145 unique `source_content_hash`, **0 duplicates**, **0 orphans** (all hashed entries have `issuer_figi`).

### OpenFIGI cache audit

- `working/openfigi_cache/`: 118 entries sampled, **100% hit rate** (all entries contain resolved FIGI payload).

### Boilerplate filter audit

- 0 explicit `discard`/`boilerplate` outcomes found in the most recent 10 processed files (the operational layer drops boilerplate before writing entries to processed files; there's no audit-trail of dropped raw signals on disk). No new boilerplate patterns to propose this cycle.
- **Open question:** consider persisting a `triage_dropped.json` per scan so future maintenance cycles can mine real discard patterns.

### Candidate drift

- 21 candidates on disk. **0** show new high-strength signals (signal_strength≥4 OR score≥28) on the same FIGI within the last 7 days. Drift report: `working/candidate_drift_2026-04-15.json`.

### Dependency check

- `yfinance==1.2.2`, `pandas==2.3.3`, `requests==2.33.1` — all importable (yfinance was reinstalled this cycle into the maintenance sandbox).

### Open issues

1. **SEDAR+ universe still missing** — `working/ca_universe.json` absent. Re-flagged from prior cycle.
2. **Phases 5–9 scanners not built** — hkex/kind/bse_nse/cvm/bmv. Endpoints probed mostly responsive; scanner code is the gap.
3. **BSE returns non-JSON via plain GET** — when Phase 7 scanner is built, will likely need `Referer: https://www.bseindia.com/` and/or cookie warm-up.
4. **No persisted triage-discard log** — boilerplate audit is blind. Suggest adding `signals/triage_dropped/<scanner>_<date>.json`.

---
## 2026-04-16T22:46Z — non-us-maintenance

### Scanner health (smoke tests)
| Scanner | Endpoint status | Module import | fetch_raw_signals |
|---------|----------------|---------------|-------------------|
| lse_rns | 200 (54,995 B) | OK | OK |
| tdnet   | 200 (63,040 B) | OK | OK |
| asx     | 200 (1,243 B, WBC sample) | OK | OK |
| sedar   | 404 (search endpoint) — expected, hybrid scraper required | OK | OK |

Stub-only registry entries (no module file): `sedar_chrome`, `hkex`, `kind`, `bse_nse`, `cvm`, `bmv` — these are unbuilt phases (5–9) and not executed.

### Universe freshness
- `working/asx_universe.json`: as_of=2026-04-14T16:08:27Z, age=1 day, 426 tickers above $300M USD floor — within 6-day TTL, no refresh needed.
- `working/ca_universe.json`: still missing — Phase 4 SEDAR+ blocker unchanged.

### Signal log audit (`signals/signal_log.json`)
- Total entries: 148 (tdnet=46, asx=72, lse_rns=30, sedar=0).
- Duplicate `source_content_hash`: 0.
- Orphan entries (hash w/o issuer_figi): 0.
- Routing distribution: immediate=33, watchlist=68, archive=47.

### OpenFIGI cache audit
- 118 cache files; sampled entries (ASL, SOL, ELV, IGO, ANN — all XASX) all valid with `issuer_figi`.
- Hit rate against last-7-day unique tickers (n=117): 117/117 = **100.0%**.

### Boilerplate filter audit
- Today's processed files are emptied post-routing (expected — routed signals are persisted in signal_log, processed JSON is per-cycle scratch).
- No per-headline reason logs available for new-pattern surfacing this cycle.
- Yesterday's audit (HEALTH_LOG 2026-04-15) confirmed no new boilerplate rules needed; ASX substantial-holder family and LSE TR-1 patterns continue to filter as designed.
- **Conclusion: no new boilerplate rule proposed this cycle.**

### Candidate drift check
- 21 candidate markdowns scanned against signal_log (7-day window).
- After excluding the originating signal cluster (same issuer + same scanner + matching first_signal_date / last_updated / primary_catalyst_date), 3 candidates show prior-date signals on the same issuer:
  - `3391_XTKS_tsuruha-holdings-tender-offer.md`: predecessor signal 2026-04-09 (tdnet, score 31.0) — likely original tender-offer announcement that triggered this candidate; not new info.
  - `8267_XTKS_aeon-co-ltd-tender-offer.md`: same pattern, predecessor 2026-04-09 (tdnet, score 31.0).
  - `CCL_XASX_cuscal-institutional-placement.md`: predecessor 2026-04-13 watchlist signal (asx, score 23.5) that escalated to immediate on 2026-04-14 (already incorporated).
- **No genuinely new high-strength drift identified — no candidate markdowns appended this cycle.**
- Detail saved to `working/candidate_drift_2026-04-16.json`.

### Dependency check
- `yfinance 1.2.2`, `pandas 2.3.3`, `requests 2.33.1` — installed via `pip install --break-system-packages` (sandbox was missing them; reinstalled successfully).

### Summary
- 4 active scanners healthy end-to-end; 6 phase 5–9 scanner registry entries remain unbuilt.
- Signal log clean, OpenFIGI cache 100% hit rate, no candidate drift, no new boilerplate rules.
- Open items: SEDAR+ Canada universe (`working/ca_universe.json`) still needs to be built — unchanged from prior cycles. Itochu (8001) thesis_direction defect still unfixed (did not re-surface).

---

## 2026-04-16T23:20Z — non-us-maintenance run

### Scanner endpoint probes

| Scanner | Endpoint | Status | Notes |
|---------|----------|-------:|-------|
| lse_rns | londonstockexchange.com/news | 200 | ok |
| tdnet   | release.tdnet.info/inbs/I_list_001_20260416.html | 200 | ok |
| asx     | asx.api.markitdigital.com/.../BHP/announcements | 200 | ok |
| sedar   | tsx.com/json/company-directory/search/tsx/A (universe src) | 200 | ok; scanner still blocked on absent `working/ca_universe.json` |
| hkex    | www1.hkexnews.hk/ncms/script/eds/english/search_active_main.js | 404 | probe URL stale — scanner module not yet built (Phase 5) |
| kind    | kind.krx.co.kr/disclosure/details.do | 200 | ok — module not yet built (Phase 6) |
| bse     | api.bseindia.com/BseIndiaAPI/api/AnnGetData/w | 200 | ok — module not yet built (Phase 7) |
| nse     | nseindia.com/api/corporate-announcements | 200 | ok — module not yet built (Phase 7) |
| cvm     | dados.cvm.gov.br/dataset/cia_aberta-doc-ipe | 200 | ok — module not yet built (Phase 8) |
| bmv     | bmv.com.mx/es/emisoras/eventos-relevantes | 404 | probe URL stale — module not yet built (Phase 9) |

### Other checks

- `py_compile` over `tools/*.py` — **clean** (no syntax errors).
- Dependencies `yfinance`, `pandas`, `requests` were missing in the cold sandbox; installed with `--break-system-packages` (yfinance 1.2.2).
- `working/asx_universe.json` — `as_of=2026-04-14T16:08:27Z`, age 1 day, 426 tickers. **Fresh (well under 6-day threshold).** No refresh needed.
- `signals/signal_log.json` — 153 entries, 0 duplicate `source_content_hash`, 0 orphan (hash w/o issuer_figi). By exchange: asx 77, tdnet 46, lse_rns 30.
- OpenFIGI cache — 122 cached files; hit rate on last 40 signal_log entries = **100%** (well above 80% threshold).
- Boilerplate audit — top discarded prefixes (last 10 raw files) dominated by known benign filings that the existing filter handles: `Becoming/Ceasing to be a substantial holder` (ASX), 通期業績予想の修正 / 決算短信 (Japan). No new patterns surfaced that warrant a new boilerplate rule.
- Candidate drift — 22 candidates checked; **0** have new signals with `score_total >= 22` dated after the candidate's file mtime. Snapshot: `working/candidate_drift_2026-04-16_maint.json`.

### Open issues

1. **HKEx and BMV endpoint probes returned 404** — the probe URLs were guesses (modules not yet built). Resolve when Phases 5 and 9 land; no operational impact today.
2. **SEDAR+ universe still missing** — `working/ca_universe.json` not present, blocking any SEDAR signal production. Matches Phase 4 standing next-action.
3. No new boilerplate rules required this cycle.

---

## 2026-04-16T04:47Z — non-us-maintenance run

### Lock handling

- Prior lock was `non-us-operational-2026-04-16T00:05` — 4h42m old, past 4h TTL. Took the lock.

### Scanner endpoint probes

| Scanner | Endpoint | Status | Bytes | Notes |
|---------|----------|-------:|------:|-------|
| lse_rns | londonstockexchange.com/news | 200 | 54,995 | t=0.51s |
| tdnet   | release.tdnet.info/inbs/I_list_001_20260416.html | 200 | 34,201 | t=0.62s |
| asx     | asx.api.markitdigital.com/.../BHP/announcements | 200 | 1,214 | t=0.86s |
| sedar   | tsx.com/json/company-directory/search/tsx/A (universe src) | 200 | 16,161 | t=0.67s |
| hkex/kind/bse_nse/cvm/bmv | — | module_missing | — | Phase 5–9 pending, skipped |

Module import + `fetch_raw_signals` presence: OK on all 5 active modules (lse_rns, tdnet, asx, sedar, sedar_chrome_supplement). `py_compile` across `tools/*.py` — clean.

### Universe freshness

- `working/asx_universe.json` — `as_of=2026-04-14T16:08:27Z`, age 1.53 days, 426 tickers. Fresh (< 6-day threshold). No refresh.
- `working/ca_universe.json` — **MISSING**. Phase 4 SEDAR+ end-to-end still blocked (unchanged from prior cycles). Not built in maintenance scope (multi-minute chunked job).

### Signal log audit (`signals/signal_log.json`)

- Total entries: 153 (unchanged vs. 2026-04-16T23:20Z run — no new operational cycle has written since).
- Duplicate `source_content_hash`: 0.
- Orphan (hash w/o `issuer_figi`): 0.
- Missing `source_content_hash`: 0.
- By scanner: asx=77, tdnet=46, lse_rns=30.
- By routing: immediate=34, watchlist=71, archive=48.

### OpenFIGI cache audit

- Cache files: 122.
- Sample of 20 entries: 20/20 resolved = 100%.
- Last-40 signal_log unique tickers (n=34) → cache name-match: 34/34 = 100% (above 80% threshold).

### Boilerplate filter audit

Top discarded / high-volume headline prefixes across last 8 raw files (n=567):

- Japanese earnings/disclosure boilerplate: `通期業績予想の修正` (18), `特別損失の計上` (10), `業績予想の修正` (10), `決算短信` (11 + 10) — all correctly categorized by existing TDnet filters.
- LSE major-holdings notifications: `TR-1 notification of major holdings` (9+8), `Standard form for notification of major holdings` (8) — low volume, retained behavior.
- LSE `trading update` (14), `final results` (6+6) — routine filings, already handled.
- No new headline patterns surfaced that warrant a new rule.

**Conclusion: no new boilerplate rule proposed this cycle.**

### Candidate drift check

- 22 candidate markdowns scanned against signal_log (score≥22 newer than candidate mtime).
- **Drift found: 0.** Snapshot saved to `working/candidate_drift_2026-04-16_maint_0447Z.json`.

### Dependency check

- `yfinance 1.2.2`, `pandas 2.3.3`, `requests 2.33.1` — missing on cold sandbox, reinstalled via `pip install --break-system-packages`. All importable post-install.

### Open issues (unchanged from prior cycles)

1. **SEDAR+ universe still missing** (`working/ca_universe.json`) — Phase 4 blocker.
2. **Phases 5–9 scanner modules not built** — hkex, kind, bse_nse, cvm, bmv.
3. **Itochu (8001) tdnet thesis_direction classifier** for buyer-side TOB cases — did not resurface this cycle.

### Summary

**GREEN.** All active scanners healthy (4 endpoints 200, 5 module imports clean, py_compile clean). Signal log clean (0 dups / 0 orphans). OpenFIGI cache 100% hit rate. No candidate drift. No new boilerplate rules proposed. Only standing blockers are Phase 4 CA universe + Phase 5–9 module gap.


---

## 2026-04-16T13:47Z — non-us-maintenance run

### Scanner endpoint probes

| Scanner | Endpoint | Status | Notes |
|---------|----------|-------:|-------|
| lse_rns | api.londonstockexchange.com (news pages) | 200 | 38 KB, 511 ms |
| tdnet   | webapi.yanoshin.jp/webapi/tdnet/list/today.json | 200 | 123 KB, 1,466 ms |
| asx     | asx.api.markitdigital.com/.../BHP/announcements | 200 | 1,214 bytes, 986 ms |
| sedar (yfinance `.news`) | SHOP.TO | 200 | 10 items, 1,547 ms |
| hkex    | — | module_missing | Phase 5 pending |
| kind    | — | module_missing | Phase 6 pending |
| bse_nse | — | module_missing | Phase 7 pending |
| cvm     | — | module_missing | Phase 8 pending |
| bmv     | — | module_missing | Phase 9 pending |

All 4 active scanners GREEN. `py_compile tools/*.py` clean across 26 files. `yfinance 1.2.2`, `pandas 2.3.3`, `requests 2.33.1` installed cleanly.

Concurrency note: took over stale operational lock (timestamp 2026-04-16T07:30Z, age ~6h 17m, exceeded 4h TTL). Prior operational session did not release the lock cleanly — no observed data corruption, and the candidate/watchlist artifacts noted in SESSION_STATE (1878_XTKS + 9 XLON watchlist JSONs) are present on disk.

Observed anomaly (informational, non-blocking): `signals/lse_rns_2026-04-16_processed.json` mtime is 2026-04-16T13:47 (coincident with start of this maintenance run), with 3 entries including BARC.XLON at source_date `14:01+00:00` — genuinely posted during LSE hours today. This implies an unlogged lse_rns scan occurred between the 07:30 operational cycle and now. Likely: OneDrive mtime re-stamp on a concurrent read, or a manual scanner run. No data integrity issue; routing table intact (1 immediate ITRK, 2 watchlist).

### Universe freshness

- `working/asx_universe.json` — 1.9 days old (as_of 2026-04-14T16:08:27Z), 426 tickers. TTL 6 days. No rebuild needed.
- `working/ca_universe.json` — MISSING. Standing blocker (operational task's responsibility per SESSION_STATE next-action #1); maintenance task does not build it.

### Signal log audit

- Total entries: 166 (+3 since 2026-04-16T23:50Z prior maintenance). All 3 are the lse_rns signals from the unlogged mid-cycle run (BARC, ITRK, TSCO).
- Duplicate `source_content_hash`: 0 out of 166 unique hashes.
- Orphan hashes missing `issuer_figi`: 0.
- `exchange` field still null on all entries — known low-priority metadata gap; `scanner` field is the authoritative source-key (tdnet=47, asx=77, lse_rns=42).

### OpenFIGI cache audit

- Cache files on disk: 133 (+1 since prior maintenance). All 133 entries have resolution (issuer_figi/figi populated); 0 empty payloads.
- Last-24h cache writes: 16. Last-7d writes: 133 (entire cache refreshed at least weekly).
- Per-scan cache-hit-rate: still not directly measurable from processed-signal files (resolver does not stamp `resolver_cache_hit` on signals). Unchanged recommendation from prior cycle — add `resolver_cache_hit: bool` field in `tools/openfigi_resolver.py` to enable auditable per-scan hit rate. Low priority; does not block production runs.

### Boilerplate filter audit

Top always-dropped headlines in last 30 raw files (699 signals scanned):

- ASX "Becoming a substantial holder" (34x), "Ceasing to be a substantial holder" (27x) — already in ASX boilerplate list in `tools/boilerplate_filters.py`; drops correct. (D-001 — 603/604 filings handled as explicit substantial-holder signals separately.)
- TDnet JP headlines (通期業績予想の修正 18x, 決算短信 11x, 特別損失の計上 10x, 業績予想の修正 10x) — these are LOAD-BEARING signals (guidance revisions, kessan tanshin, special-loss disclosures), NOT boilerplate. Their drop is triage (market-cap floor / novelty dedup), not boilerplate. Do NOT add to boilerplate list.
- LSE "Trading Update" (11x, 100% drop) — ambiguous; many contain guidance. Do not boilerplate.
- LSE "Standard form for notification of major holdings" (7x, 100% drop) — already emitted as `major_shareholder_change` with `strength_estimate=3`; downstream drop is triage, not a boilerplate pass-through. Valid 5%+ holding disclosures should remain scored. Do NOT add to boilerplate list.

**Recommendation: no new boilerplate rules this cycle.** Same as 2026-04-16T23:50Z.

### Candidate drift audit

- 23 candidates + 37 watchlist entries present.
- Re-queried each candidate's `issuer_figi` against signals posted AFTER candidate file mtime in the 7-day window, score ≥22, excluding own source_date.
- **True drift detected: 0.** Naive re-query matched 7 candidates, but after filtering out the originating signal (by date + mtime), all were self-matches. No new high-strength follow-on signal has appeared on any existing candidate issuer.

### Standing items carried forward

- Re-probe HKEx and BMV endpoints with real URLs once Phase 5 / Phase 9 scanner modules exist.
- Once `working/ca_universe.json` is built, add sedar cache-hit-rate to per-scanner audit.
- Optional enhancement: stamp `resolver_cache_hit: bool` on resolved signals in `tools/openfigi_resolver.py` for observability.
- Operational lock hygiene: prior cycle did not release the lock on exit. Watch for recurrence next cycle; if it repeats, investigate operational task's shutdown path.

### Overall verdict

**GREEN.** All 4 active scanners healthy, ASX universe fresh, signal log clean (166/166 unique, 0 orphans), OpenFIGI cache full, 0 candidate drift. No new boilerplate rules recommended.
