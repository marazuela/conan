# Project orientation — read first

This folder contains the **Non-US Discovery System**: an automated investment-candidate discovery pipeline covering 9 non-US exchanges. It scans exchange filings, applies triage + scoring + convergence dedup, and emits candidate markdown files with a full deep-dive scaffold for human review.

Everything lives under `non_us_discovery_system/`.

---

## How to resume work

Read these files in order to orient yourself:

1. **`non_us_discovery_system/PHASE3_PROGRESS.md`** — current work log. Tells you exactly what's done, what's in flight, and what the next command is. If this file exists, it is authoritative about current state.
2. **`non_us_discovery_system/README.md`** — architecture overview (if present).
3. **`non_us_discovery_system/framework/`** — core design docs: scoring rubric, convergence rules (D-001, D-004), translation honesty (D-002), entity resolution (D-003).
4. **`non_us_discovery_system/strategies/strategy_*.md`** — one per exchange. Each has endpoint details, filing categories, signal filters, deep-dive checklist. Status line at the top tells you whether that exchange is STUB or ACTIVE.

Whenever you finish a meaningful chunk of work, **update or replace `PHASE3_PROGRESS.md`** (rename it to `PHASE{N}_PROGRESS.md` when you advance to the next phase) so the next session can pick up cleanly.

---

## Build phase order

Build one exchange at a time, in this order. Don't start a later phase until the current one produces at least one real candidate markdown file end-to-end.

| Phase | Exchange | MIC | Scanner module | Status |
|------:|----------|-----|----------------|--------|
| 1 | UK LSE RNS | XLON | `tools/lse_rns_scanner.py` | complete |
| 2 | Japan TDnet | XTKS | `tools/tdnet_scanner.py` | complete |
| 2.1 | Japan market-cap enrichment | — | `tools/jpx_market_cap.py` | complete |
| 3 | Australia ASX | XASX | `tools/asx_scanner.py` | **in progress** |
| 4 | Canada SEDAR+ | XTSE / XTSX | `tools/sedar_scanner.py` | pending |
| 5 | Hong Kong HKEx | XHKG | `tools/hkex_scanner.py` | pending |
| 6 | Korea KIND | XKRX | `tools/kind_scanner.py` | pending |
| 7 | India BSE/NSE | XBOM / XNSE | `tools/bse_nse_scanner.py` | pending |
| 8 | Brazil CVM | BVMF | `tools/cvm_scanner.py` | pending |
| 9 | Mexico BMV | XMEX | `tools/bmv_scanner.py` | pending |
| 10 | Register scheduled Cowork tasks | — | — | pending |
| 11 | 7-day autonomous run | — | — | pending |

Scanners all conform to the same contract: expose `fetch_raw_signals(window_days: int) -> list[dict]` in the common signal schema. They're registered in `SCANNER_REGISTRY` inside `tools/pipeline_runner.py`.

---

## Running the pipeline

From `non_us_discovery_system/`:

```
python3 -m tools.pipeline_runner --scanner <key> --window 7
```

Where `<key>` is one of `lse_rns`, `tdnet`, `asx`, `sedar`, `hkex`, `kind`, `bse_nse`, `cvm`, `bmv`.

This runs the full pipeline: scanner → triage (market-cap floor, novelty, boilerplate, translation floor) → entity resolve (OpenFIGI) → dedup + convergence → score → route. Signals scoring ≥28 go immediate (candidate markdown), 22-27 watchlist, 14-21 archive, <14 discard.

---

## Non-negotiable rules

- **Market-cap floor $300M USD.** Below that, discard.
- **Translation honesty (D-002).** For non-English sources, `translation_confidence < 0.85` caps `signal_strength=2` and `risk_reward=3`, and forces `thesis_direction="unknown"`. `< 0.70` drops the signal at triage.
- **Convergence hard-merge.** Same `issuer_figi` + same `signal_type` + same `source_date` unconditionally merges into one candidate with `related_signal_ids`.
- **Cross-listing aware dedup (D-001).** One issuer with filings on multiple exchanges is one candidate, not N.
- **Never mock data.** If an endpoint is unreachable, log it and move on — don't fabricate signals.
- **Candidate markdowns include a `steelman of the opposite view` section and explicit `kill conditions`.** No exceptions.

---

## Paths and conventions

- Scanners: `tools/<exchange>_scanner.py`
- Shared utilities: `tools/openfigi_resolver.py`, `tools/convergence_engine.py`, `tools/boilerplate_filters.py`, `tools/pipeline_runner.py`
- Working cache (universes, mcap lookups, FX rates): `working/`
- Raw signals per scan: `signals/raw/<scanner>_<date>.json`
- Processed signals per scan: `signals/<scanner>_<date>_processed.json`
- Signal log (append-only, for novelty dedup): `signals/signal_log.json`
- Candidates (deep-dive stubs): `candidates/<ticker>_<mic>_<slug>.md`
- Watchlist: `candidates/watchlist/`

---

## Known gotchas

- **yfinance auth:** raw HTTP to Yahoo Finance (`/v7/finance/quote`, `/v10/finance/quoteSummary`) returns 401. Use the `yfinance` Python library's `Ticker.info` instead.
- **Alphanumeric JPX tickers (e.g. `469A0`):** yfinance rejects the 5-char form; strip the trailing char to `469A.T`.
- **ASX:** no firehose endpoint exists. Only viable source is `asx.api.markitdigital.com/asx-research/1.0/companies/{T}/announcements` (5-item cap per ticker). Scanner operates against a pre-filtered universe in `working/asx_universe.json` (≥$300M USD, 7-day TTL).
- **Sandbox filesystem sync:** the Edit tool and the bash sandbox can see different versions of the same file for a few seconds after a write. If bash shows a truncated file right after a large Write, re-read via the Read tool to confirm the on-disk content before debugging.
- **Background nohup jobs lock the bash sandbox.** If you kick off a long-running process in the background, follow-up bash calls may block until it finishes. Prefer foreground for sub-10-minute jobs.

---

## When in doubt

- The Japan candidates in `candidates/9601_XTKS_shochiku-osaka-building-impairment.md`, `candidates/2972_XTKS_sankei-real-estate-tender-offer.md`, and `candidates/6058_XTKS_vector-special-losses.md` are canonical examples of the candidate-markdown format. Copy that shape for new candidates.
- The UK candidate (PTSB / Phase 1) is another reference.

---

## Reporting (external) — added 2026-04-15

Tool 2 is producer-only. Performance reports and deep-dive candidate briefs are generated by the project-root `Reporting Hub/` (tasks `reporting-hub-performance` and `reporting-hub-deep-dives`), which reads this system's state files (`SESSION_STATE.md`, `PROGRESS_LOG.md`, `candidates/`, `signals/`, `reports/`) and writes exclusively inside `Reporting Hub/`.

- Do NOT write to `Reporting Hub/` from this system.
- Do NOT recreate a `reporting_layer/` subfolder — it was removed on 2026-04-15.
- Hub read contract: `Reporting Hub/SOURCES.md`.

Scheduled tasks `non-us-performance-report` and `non-us-deep-dives` have been retired. The producer tasks `non-us-operational` and `non-us-maintenance` remain unchanged.
