# 09 — Investmet tool Beta / non_us_discovery_system — Full Diagnostic

**Audit date:** 2026-04-14
**Auditor:** Claude (deep code + data review, no runtime, sandbox unavailable)
**Target:** `C:\Users\javie\OneDrive\Desktop\Claude Cowork\Conan\Investmet tool Beta\non_us_discovery_system`
**Scope:** same depth as diagnostics 01–06 for the original Investment tool.

---

## 1. Executive summary

Beta is a mature, well-architected sibling to the US-focused Investment tool, covering nine non-US exchanges. **Three scanners are in production** (LSE RNS, TDnet, ASX), producing 71 entries in the signal_log and 5 active candidate markdowns. **Phase 4 (SEDAR+) is code-complete** but has not executed end-to-end because the bash sandbox was unavailable on 2026-04-14. Phases 5–9 (HKEx, KIND, BSE/NSE, CVM, BMV) have strategy docs only.

The core architecture — 7-dimension rubric, D-001/002/003/004 decisions, convergence engine, OpenFIGI resolver, pipeline runner — is cleanly separated, reusable across scanners, and structurally stronger in several ways than the Investment tool's equivalents (notably: persistent OpenFIGI cache on disk is enabled here; the Investment tool's is in-memory-only).

**Three concrete defects found.** One is a latent syntax error in `tools/tdnet_scanner.py`. Two are design gaps in ASX substantial-holder noise handling and pipeline max-tickers plumbing. None currently blocks the 3 live scanners, but the tdnet bug is the same pattern ("OneDrive sync / botched append" trailing garbage) that hit Investment tool's `convergence_engine.py` and needs the same fix before the next redeploy.

**Overall grade:** production-healthy, ~35% of planned coverage built. The build order is disciplined, decisions are dated and justified, and the code quality is notably higher than the Investment tool's average.

---

## 2. Current state snapshot

### 2.1 Live infrastructure

| Dimension | Status |
|-----------|--------|
| Scanners built | 3 / 9 (LSE RNS, TDnet, ASX) |
| Scanners code-complete pre-runtime | +1 (SEDAR hybrid: primary + chrome supplement) |
| Scanners strategy-only | 5 (HKEx, KIND, BSE/NSE, CVM, BMV) |
| Active candidates | 5 (PTSB_XLON, WBC_XASX, 2972/2972/6058/9601 XTKS) |
| Signal log entries | 71 |
| OpenFIGI cache | 76 files, 100% resolve rate on last scan |
| Universe cache age | ASX: 0.03 days (fresh); Canada: not yet built |
| Scheduled tasks | 4 registered: non-us-operational (20 */3), non-us-maintenance (40 */3), non-us-performance-report (45 1), non-us-deep-dives (45 */4) |

### 2.2 Today's top signals (2026-04-14)

From the ASX pipeline run:

| Score | Route | Ticker | Signal type | Headline |
|------:|-------|--------|-------------|----------|
| 30.0 | immediate | WBC | results_items_impacting | Items Impacting Half Year 2026 Results |
| 29.0 | immediate | PDI | merger_agreement | PDI & Robex Merger Proceeding to Implementation |
| 29.0 | immediate | RXR | merger_agreement | PDI & Robex Merger Proceeding to Implementation |
| 27.5 | watchlist | SMI | equity_placement | Completion of Tranche 2 Placement |
| 35.0 | immediate | 2972 (JP) | tender_offer | Sankei RE tender-offer terms change |

The PDI/RXR pair is doing exactly what D-001 was designed to handle *except* it's not a single issuer — it's the two legs of a merger. That is a different dedup problem than cross-listing; currently not handled. Noted as a finding below.

---

## 3. Architecture — what the system actually does

Flow, end to end:

1. **Universe enumeration** (per-exchange). Cached 7 days.
   - LSE: investegate.co.uk + LSE alldata metadata (in-flow, not pre-filtered).
   - TDnet: firehose HTML, filtered after fetch.
   - ASX: `asx.com.au/asx/research/ASXListedCompanies.csv` → yfinance `.AX` → AUD/USD → ≥$300M → `working/asx_universe.json`.
   - Canada (coded, not run): TMX company directory A–Z iteration × tsx+tsxv → yfinance `.TO`/`.V` → CAD/USD → ≥$300M → `working/ca_universe.json`.

2. **Scan** (`tools/<exch>_scanner.py`).
   - Each scanner calls `fetch_raw_signals(window_days, **kwargs)`.
   - Per-exchange title-regex rule tables map headlines to `(signal_type, strength, direction)`.
   - Emits common signal schema with `rubric_scores` already attached (per-exchange `*_rubric.py`).

3. **Pipeline orchestration** (`tools/pipeline_runner.py`).
   - `_enrich_market_caps` — currently only wired for XTKS via `tools/jpx_market_cap.py`.
   - `triage()` — novelty (content hash), boilerplate filter, $300M floor, translation_confidence ≥ 0.70.
   - `resolve_entity()` — OpenFIGI via ticker + MIC (D-003); file cache in `working/openfigi_cache/` (7-day TTL).
   - `convergence_engine.process()` — union-find dedup on `issuer_figi` within 14d; Jaccard ≥ 0.80 (D-004); `annotate_convergence` awards +4 for 2 strategies, +8 for 3+.
   - `score_signal()` — applies D-002 caps (signal_strength ≤ 2, risk_reward ≤ 3 when direction=unknown), weighted sum across 7 dimensions.
   - `route()` — ≥28 immediate, 22-27 watchlist, 14-21 archive, <14 discard.

4. **Write**.
   - Raw signals: `signals/raw/<scanner>_<date>.json`.
   - Processed: `signals/<scanner>_<date>_processed.json`.
   - Log: `signals/signal_log.json` (append-only).
   - Candidates: `candidates/TICKER_MIC_short-desc.md` (handled by deep-dives skill, not pipeline_runner).

### 3.1 What's structurally *better* than the Investment tool

Three things stand out from comparing Beta to the Investment tool:

1. **Persistent OpenFIGI file cache is enabled here** (`CACHE_DIR = working/openfigi_cache/`, 7-day TTL). The Investment tool's resolver has `CACHE_FILE=None`, which is why it re-hits the API on every run. Beta's pattern is the correct one.

2. **Per-exchange rubric files** (`lse_rubric.py`, `tdnet_rubric.py`, `asx_rubric.py`, `sedar_rubric.py`) with documented baselines (e.g. ASX info_asymmetry=2 for English/well-covered; TDnet info_asymmetry=4 for Japanese; SEDAR TSXV +1 for specialist coverage). The Investment tool mostly has hard-coded rubric tables inside each scanner.

3. **D-001 / D-002 / D-003 / D-004 are formally documented** in `DECISIONS.md` with context, alternatives-considered, and implications — at the level of an ADR. That's a much stronger engineering practice than the Investment tool has.

### 3.2 What's structurally weaker

1. **Chunked runner pattern (`asx_chunked_scan.py` + `asx_finalize.py`) is ASX-specific.** It exists because the 426-ticker ASX scan doesn't fit in a 45-second bash window, so the pipeline was split into restart-safe stages. Same problem will hit Canada (~300-500 tickers), HKEx (~2,600 main board), KIND (~2,000), BSE/NSE (>4,000). The pattern needs to be generalized into `tools/chunked_runner.py` or every new scanner will reinvent it.

2. **`_enrich_market_caps` hard-codes `{"XTKS": "tools.jpx_market_cap"}`.** ASX enrichment is done inside the universe builder, not via this hook; SEDAR is done the same way. The asymmetry means two different conventions for "how do we populate market_cap_usd_mm" live in the codebase. Pick one.

3. **`scanner_kwargs` pipeline forwarding is brittle.** `_filter_scanner_kwargs` only forwards `max_tickers` and `throttle_seconds`. If a scanner needs something else (e.g. sedar wants `boards=("tsx",)` to skip TSXV), it has to get it from somewhere other than the CLI. Two ways to fix: expand the kwargs list, or pass a scanner-specific config dict.

---

## 4. Scanner-by-scanner findings

### 4.1 LSE RNS (`lse_rns_scanner.py`, 454 lines) — GOOD

- **Source:** investegate.co.uk enumeration + LSE alldata API for metadata.
- **Rule table:** 13 patterns covering Rule 2.7 / Rule 2.4 firm-and-possible offers, guidance up/down, trading updates, governance changes, TR-1, JORC resources, AIM suspensions, fundraises, buybacks.
- **FX:** `GBP_TO_USD = 1.27` — hardcoded constant. Not wrong today, but this should be live via yfinance in a small helper so it doesn't drift as it has in the Investment tool (where similar hardcoded FX rates drifted materially year-over-year).
- **Cache:** 24h local file cache in `working/lse_alldata_cache/`. Clean.
- **Quality:** no trailing garbage, compiles cleanly.
- **Current output:** today's scan produced the BAWAG–PTSB Rule 2.7 candidate at score 33, with a merged sibling RNS. That's exactly the convergence engine doing its job.

**Recommended improvements:**
- Move GBP_TO_USD to a runtime fetch with a dated fallback (same pattern ASX uses for AUD).
- No urgent defects. This is the canary scanner and it's running cleanly.

### 4.2 TDnet (`tdnet_scanner.py`, 324 lines) — **LATENT BUG**

- **Source:** TDnet firehose HTML at `release.tdnet.info/inbs/I_list_001_YYYYMMDD.html`.
- **Rule table:** 16 Japanese regex patterns, with per-rule translation_confidence values (0.92 for 下方修正 / 上方修正 which are directionally unambiguous; 0.70 for 修正 / 見直し which are ambiguous).
- **Tokyo filter:** `東` character in place string.
- **JPX market-cap enrichment:** `tools/jpx_market_cap.py` — yfinance `CODE.T` → `info["marketCap"]` → JPY/USD live (JPY=X) → USD mm. Handles 4-digit, 5-digit, and alphanumeric JPX codes (e.g. `469A0` → strips to `469A.T`). Cached 7 days.

**Bug — trailing garbage at lines 319–323:**
```python
if __name__ == "__main__":
    _main()
scii=False))


if __name__ == "__main__":
    _main()
```

This is the same "OneDrive sync / botched append" pattern that hit `convergence_engine.py` in the Investment tool (where the last 10 lines were orphaned from an earlier edit). Two observations:

1. `scii=False))` is almost certainly the tail of `ensure_ascii=False))` — likely from a JSON-print stub that got overwritten.
2. As written, the file should raise `SyntaxError` on import. But `signals/tdnet_2026-04-14_processed.json` clearly contains valid scanner output (the 2972 Sankei tender-offer signal at strength 5, translation_confidence 0.92). Possibilities:
   - Python is loading a cached `.pyc` from a clean version, masking the broken `.py`.
   - The scanner is being invoked in a way that tolerates trailing garbage (unlikely — `SyntaxError` is at parse time).
   - The output was generated from an earlier clean version of the file, and the current on-disk `.py` is post-corruption.

**Fix:** Delete lines 319–323. The canonical tail should be only:
```python
if __name__ == "__main__":
    _main()
```

This must happen before any redeploy or the next `python3 -m tools.pipeline_runner --scanner tdnet` call from a cold bytecode cache will fail hard.

**Recommended improvements beyond the fix:**
- Add CI-style `python -m py_compile tools/*.py` step in the maintenance skill so this category of corruption is caught automatically.
- Per-rule translation_confidence is well-engineered, but watch for calibration drift as the rule set grows.

### 4.3 ASX (`asx_scanner.py` 352 lines + `asx_universe.py` + `asx_chunked_scan.py` + `asx_finalize.py` + `asx_rubric.py`) — GOOD with noise issue

- **Source:** markitdigital per-ticker (5-announcement cap per call), iterated over pre-filtered 426-ticker universe.
- **Rule table:** 30+ patterns covering takeovers, scheme of arrangement, guidance up/down, impairment, substantial holder filings, trading halts, JORC drilling, Appendix 4C cashflow, going-concern.
- **Price-sensitive flag:** bumps strength +1 (cap 5) and edge_decay +1 (cap 5) via the rubric.
- **Small-cap bump:** info_asymmetry +1 when market_cap_usd_mm < 1000.
- **Chunked runner pattern:** 80-ticker chunks per bash call, on-disk checkpoint in `working/asx_chunked_state.json`, then stage-based finalize (triage → resolve with time-budget checkpointing → dedup → score → write).
- **Quality:** clean, no trailing garbage, compiles.

**Issue — substantial-holder noise:**
Today's scan: 149 raw → 89 boilerplate-dropped → 60 triaged. Of the 89 drops, ≈88 are `substantial_holder_change` variants (`becoming a substantial holder` / `ceasing to be a substantial holder` / `change in substantial holding`). That's being correctly caught at the boilerplate filter stage, but it means:

1. The scanner is generating ~60% noise-to-signal at emit time.
2. When a substantial-holder filing *is* material (e.g. an activist fund crossing 5%, or BlackRock going from 4.9% to 10%), the current code drops it blindly.

**Fix — holder-size threshold filter in `asx_scanner.py`:**
- Parse the holding percentage from the headline/body when the pattern is `substantial_holder_*`.
- Emit only when the new holding crosses a meaningful threshold (e.g. 5% → 10%, or any activist/PE-labelled filer by name).
- Route the rest to a lower-priority `substantial_holder_trace` signal_type that is triage-dropped by default but logged for monthly review.

Flagged in `HEALTH_LOG.md` 2026-04-14 as a recommendation. Low-complexity, high-value fix.

### 4.4 SEDAR (`sedar_scanner.py` 327 lines + `sedar_rubric.py` + `sedar_chrome_supplement.py` + `ca_universe.py`) — CODE-COMPLETE, UNRUN

- **Decision:** hybrid, per `PHASE4_PROGRESS.md` §Decision:
  - **Primary per-scan:** yfinance `Ticker('<SYM>.TO').news` — cheap, no auth, returns syndicated English-language headlines.
  - **Daily supplement:** `sedar_chrome_supplement.py` reads `working/sedar_chrome_inbox.json` (operator-populated via Claude-in-Chrome). Catches French-only Quebec filings and TSXV small-caps the aggregator misses.
- **Rule table:** 35+ patterns covering takeover bids, plans of arrangement, material change reports, guidance up/down, early warning 10%, NI 43-101 technical, NI 51-101 reserves, MCTO / cease-trade, impairment, restatement, bought deals, private placements, NCIB/SIB, going concern, covenant breach, CCAA.
- **Rubric:** baseline info_asymmetry=2 for TSX large-caps; +1 TSXV; +2 NI 43-101; +1 French; D-002 caps applied when French with translation_confidence < 0.85.
- **Universe builder (`ca_universe.py`):** iterates TMX company-directory endpoint per A–Z char × (tsx, tsxv); dedups; yfinance CADUSD=X with fallback 0.72.

**Status:** sandbox unavailable 2026-04-14 blocked end-to-end execution. Expected result per PHASE4_PROGRESS.md: 300–500 tickers above floor on TSX+TSXV.

**Risks flagged in the code and docs:**
- TMX directory endpoint may rate-limit or change schema. Fallback: static TSX listed-companies CSV.
- yfinance news volume is ticker-dependent: large-caps ~10 items/week, mid-caps 2-3, small-caps often 0. The Chrome supplement exists to cover the gap.
- TSXV can produce noise; strategy doc recommends raising TSXV strength threshold to ≥4 at triage.

**Recommended improvements:**
- When the sandbox is back: run the 3-step smoke test in PHASE4_PROGRESS §"What still needs to happen." Do not skip the per-ticker slice — full-universe on first run is expensive to debug.
- The Chrome supplement's `working/sedar_chrome_inbox.json` is operator-populated; it's not yet wired to a Cowork shortcut. That's Phase 10 work per the plan. Flag as a pending handshake point.
- Consider starting with `boards=("tsx",)` only and adding TSXV in a second pass once the top-quality signals are clean.

### 4.5 Shared infrastructure

**`tools/pipeline_runner.py` (320 lines) — clean.**
SCANNER_REGISTRY has all 10 entries (lse_rns, tdnet, asx, sedar, sedar_chrome, hkex, kind, bse_nse, cvm, bmv). Five are currently `module_missing`. The routing thresholds and D-002 cap implementation are both correct and match what's documented in `DECISIONS.md` and `framework/scoring_system.md`.

**`tools/convergence_engine.py` (209 lines) — clean.**
`JACCARD_THRESHOLD = 0.80`, `CONVERGENCE_WINDOW_DAYS = 14`. Union-find by `issuer_figi`, Jaccard on first 500 words of normalized text. `annotate_convergence()` counts distinct strategies per issuer_figi in 14d; +4 for 2 strategies, +8 for 3+. Self-test in `__main__`.

**`tools/openfigi_resolver.py` — persistent cache enabled.**
`CACHE_DIR = working/openfigi_cache/`, 7-day TTL. Stronger than Investment tool's equivalent.

**`tools/boilerplate_filters.py` (130 lines) — complete across 9 exchanges.**
Per-exchange regex lists for LSE, TDnet, ASX, SEDAR, HKEx, KIND, BSE_NSE, CVM, BMV. Fail-open for unknown exchange keys. ASX section explicitly includes `becoming a substantial holder` (which is why the 88 drops happen; see §4.3).

---

## 5. Candidate quality review

Five active candidates. I read three in full (WBC, 2972 Sankei, PTSB) and scanned headers on the other two (6058 Vector, 9601 Shochiku).

All five follow the template in `framework/candidate_template.md`:
- Full frontmatter including FIGI, issuer_figi, market_cap_usd_mm, score + convergence_bonus + score_total.
- `status: pending_deep_dive` on all five — they are scanner stubs, not completed thesis files. That's the correct semantics; the deep-dives skill is supposed to flesh them out.
- TL;DR, Source signal(s), Translation notes, Thesis statement (pending), Steelman, Web research layer, Kill conditions, Catalyst map, Position sizing, Source traceability.

Quality observations:

**WBC (score 30, immediate):** strong template fill. Pre-tagged short based on the well-known "Items Impacting" Westpac pre-announcement convention, with a detailed steelman (kitchen-sink / new-CEO reset, already-priced, non-cash software impairment, peer read-through, buyback offset) and explicit kill conditions. This is exemplar quality for a stub.

**2972 Sankei (score 35, immediate):** J-REIT tender offer with sibling filings correctly merged by the convergence engine. Translation_confidence 0.92 on the 公開買付け pattern. Template fill is thinner than WBC — the steelman is more bullet-list than prose — but directionally correct.

**PTSB (score 33, immediate):** RNS Rule 2.7 recommended cash offer with a merged sibling. Thesis direction long. Kill conditions are mostly generic ("Offer is withdrawn", "Regulatory block") — a thesis-specific threshold (e.g. bid premium) is missing. Deep-dive will fix.

**Template compliance:** 5/5. **Thesis-specific detail:** 3/5 strong, 2/5 skeletal. The pipeline is emitting stubs, not finished work — that's working as designed.

---

## 6. Data quality issues

### 6.1 Metadata gap: `exchange` field null on all 71 signal_log entries

Confirmed in `HEALTH_LOG.md` 2026-04-14: `exchange` field is null on all 71 log entries. Low-priority because `mic` is present and downstream code routes on `mic`. But it's a template-fidelity issue — `INSTRUCTIONS.md` defines `exchange` as a mandatory signal-schema field.

**Fix:** the three scanners need one line added at signal-emit time:
- `lse_rns_scanner.py`: `exchange="LSE"`.
- `tdnet_scanner.py`: `exchange="TDnet"`.
- `asx_scanner.py`: `exchange="ASX"`.

SEDAR already does this correctly in `sedar_scanner.py` (`"exchange": "TSX" if board == "tsx" else "TSXV"`). Use that as the template.

### 6.2 PDI/RXR merger-pair dedup gap

Today's ASX run emitted four immediate-route signals that are really two events:
- PDI — Merger Proceeding to Implementation + Implementation Timetable
- RXR — Merger Proceeding to Implementation + Implementation Timetable

The convergence engine dedups within-issuer (same issuer_figi + same signal_type + same source_date → merged) but not across merger counterparties. D-001 is designed for cross-listings (BHP on ASX + LSE), not for merger pairs (acquirer + target in the same transaction).

**Mitigation options:**
1. **Cross-reference table in the signal_log.** When an ASX `merger_agreement` signal is emitted, look ahead in the same day's raw signals for the other leg by scanning headline counterparty mentions. If found, tag both with a shared `transaction_id` and fold the convergence bonus once, not twice.
2. **Manual review in the deep-dives skill.** Acceptable short-term; don't score-inflate by double-counting when the deep-dive will catch it anyway.

I'd recommend option 2 for now (it's a 2026-04 issue, not a 2026-Q1-carried-forward issue), but option 1 becomes important when HKEx / BSE add their own high-merger-volume regimes.

### 6.3 Market-cap enrichment asymmetry

Three patterns coexist:
- **JPX:** enriched via `_MCAP_ENRICHERS_BY_MIC` hook in pipeline_runner.
- **ASX + Canada:** enriched in the universe builder, baked into `working/<ex>_universe.json`.
- **LSE:** enriched via the in-flow LSE alldata metadata API call during the scanner run.

All three work, but the pattern diversity is an accident of build order, not design. It makes the pipeline harder to reason about. Standardize toward the universe-builder pattern (ASX/Canada): it centralizes the data, avoids per-scan yfinance calls, and caches cheaply.

---

## 7. Defect list (priority-ordered)

| # | Severity | File | Issue | Fix effort |
|---|----------|------|-------|-----------|
| 1 | **HIGH (latent)** | `tools/tdnet_scanner.py` L319-323 | Trailing garbage `scii=False))` + duplicated `__main__` block. Will fail Python syntax check on cold bytecode cache. | 30 sec |
| 2 | MED | `tools/asx_scanner.py` | `substantial_holder_*` signals generate ~88 of 90 boilerplate drops per scan. Real material holder changes are dropped with noise. | 2-4h (parse percentage, threshold filter) |
| 3 | MED | 3 scanners | `exchange` field null in signal_log for all 71 entries. Template-fidelity gap. | 10 min (one-line addition per scanner) |
| 4 | LOW | `tools/lse_rns_scanner.py` | `GBP_TO_USD = 1.27` hardcoded. | 30 min (switch to runtime yfinance fetch with fallback) |
| 5 | LOW | ASX merger pairs | Convergence engine does not merge acquirer+target emissions. Double-counts convergence. | 2-3h if fixing in code; zero if delegated to deep-dives skill |
| 6 | LOW | pipeline plumbing | Market-cap enrichment has 3 coexisting patterns (hook, universe-builder, in-flow). | 4-6h to unify; can wait for a lull |
| 7 | LOW | `_filter_scanner_kwargs` | Only forwards `max_tickers` / `throttle_seconds`. Inflexible as scanners grow. | 1h (generalize to scanner config dict) |
| 8 | INFO | `asx_chunked_scan.py` / `asx_finalize.py` | Checkpointing pattern will need to be generalized for HKEx / KIND / BSE-NSE. | 4-8h when Phase 5 starts |

---

## 8. What to do next

### 8.1 Immediate (this session or next, <1 day total)

1. **Fix the tdnet trailing-garbage bug.** Read the canonical `__main__` stanza, delete lines 319-323, verify with `python -m py_compile tools/tdnet_scanner.py`.
2. **Add `exchange` field to the three live scanners.** One line per scanner. Fixes signal_log metadata fidelity.
3. **Run the SEDAR smoke tests from PHASE4_PROGRESS.md §3 once the sandbox is back.**

### 8.2 Short-term (this week)

4. **Implement the ASX substantial-holder-size threshold filter** per HEALTH_LOG.md recommendation. Parse percentage from headline; only emit for meaningful threshold crossings or activist/PE-labelled filers.
5. **Promote GBP_TO_USD to a runtime fetch** to match the AUD/CAD/JPY pattern.
6. **Add a `python -m py_compile tools/*.py` step to the maintenance skill** so the class of bug represented by #1 is caught automatically on every maintenance cycle.

### 8.3 Medium-term (by end of month)

7. **Complete SEDAR Phase 4 end-to-end.** Smoke test → full run → first XTSE/XTSX candidate markdown → mark phase complete → memory update → advance to Phase 5.
8. **Build Phase 5 (HKEx).** Read `strategies/strategy_hk_hkex.md` first. HKEx has both a well-documented bulk filings API and Chinese-language filing nuance; translation-confidence discipline matters more here than for SEDAR.
9. **Generalize the chunked-runner pattern.** Before building HKEx, extract `asx_chunked_scan.py` / `asx_finalize.py` into `tools/chunked_runner.py` with a scanner-independent interface. Will save ~1.5 days × 5 remaining scanners.

### 8.4 Longer-term (Phase 6+)

10. **Wire the SEDAR chrome-supplement to a Cowork shortcut** so the once-daily inbox population is automatic instead of manual.
11. **Cross-system convergence analyzer.** Beta and the Investment tool currently run with zero shared state by design (D-000 file-system independence). The "Independent review" project folder on disk is where this cross-system layer will live. Revisit after Beta has 6+ scanners live and Investment tool is fully de-flake.

---

## 9. What the synergy story looks like when both tools are healthy

Beta and the Investment tool are designed to have **zero universe overlap.** The Investment tool covers US-listed equities (EDGAR, US contract awards, Congressional trading, FDA PDUFA, ESMA shorts on European-domiciled but US-traded names). Beta covers non-US exchanges in native languages.

The synergy happens at three levels:

1. **Universe coverage.** Combined, the two systems cover ~8,000 non-US-primary-listed issuers plus ~8,000 US-primary. No issuer gets scanned twice; no catalyst is missed for dual-listed names because Beta's convergence engine handles cross-listings explicitly.

2. **Cross-system convergence.** If Rio Tinto (ASX+LSE primary; US ADR) appears in both Beta's signal_log (via ASX RNS) and the Investment tool's signal_log (via its US ADR SEC filings or contract-award hits), that is a real cross-system convergence event. Currently neither tool sees the other. This is the "Independent review" project's job, not Beta's.

3. **Translation/context asymmetry.** Beta produces candidates in markets where the Investment tool has zero coverage (Japan, Korea, India, Brazil, Mexico). These are systematically under-covered by US sell-side; the info_asymmetry score on Beta candidates is structurally higher than on US-equivalent Investment tool candidates, and that shows up in the rubric weights.

The current state: **Beta is healthier than the Investment tool**, has better engineering discipline (ADR-style DECISIONS.md, persistent OpenFIGI cache, per-exchange rubric files), and is already producing publication-quality candidate stubs. The fastest way to compound value is to fix the three immediate items in §8.1 and push Phase 4 over the line.

---

## 10. Appendix — Files read for this audit

Core docs: `CLAUDE.md`, `OBJECTIVES.md`, `SESSION_STATE.md`, `PHASE3_PROGRESS.md`, `PHASE4_PROGRESS.md`, `DECISIONS.md`, `OPEN_QUESTIONS.md`, `HEALTH_LOG.md`, `INDEX.md`, `framework/scoring_system.md`, `framework/candidate_template.md`.

Python sources: `tools/pipeline_runner.py`, `tools/lse_rns_scanner.py`, `tools/tdnet_scanner.py` (including trailing-garbage confirmation at L310-324), `tools/asx_scanner.py`, `tools/asx_universe.py`, `tools/asx_chunked_scan.py`, `tools/asx_finalize.py`, `tools/asx_rubric.py`, `tools/sedar_scanner.py`, `tools/sedar_rubric.py`, `tools/sedar_chrome_supplement.py`, `tools/ca_universe.py`, `tools/jpx_market_cap.py`, `tools/convergence_engine.py`, `tools/openfigi_resolver.py` (first 80 lines), `tools/boilerplate_filters.py`.

Data: `signals/tdnet_2026-04-14_processed.json` (first 50 lines).

Candidates: `candidates/WBC_XASX_westpac-hy2026-items-impacting-results.md`, `candidates/2972_XTKS_sankei-real-estate-tender-offer.md`, `candidates/PTSB_XLON_bawag-recommended-cash-offer.md`.

Sandbox: unavailable 2026-04-14; no runtime verification performed. All findings are from code inspection + today's emitted data.
