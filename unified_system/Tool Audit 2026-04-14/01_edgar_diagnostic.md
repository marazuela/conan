# EDGAR Filing Monitor — Diagnostic
**Tool**: `tools/edgar_filing_monitor.py` v2.4
**Grade**: **A** — best-performing scanner in the pipeline

---

## What it does (verified)
Scans SEC EDGAR full-text-search (EFTS) for material keywords across four rotating categories (activist → M&A → distress → governance). Applies form whitelists/blacklists per category. Resolves ticker + mcap. Emits strength-scored signals. Rotation state persists in `signals/edgar_rotation_state.json`.

## Current health (verified)
- **Today's live-run**: S56 (2026-04-14 16:07 UTC) rotated to `mna`, caught **2 strength-5 live deals**: AVNS ($25 AIP buyout, 72% premium) + GSAT ($90 Amazon acquisition). Both became candidate writeups same-day.
- **Compilation**: `py_compile` clean on S56 (no file-truncation regression).
- **API**: EFTS 200, data.sec.gov 200.
- **Wall-clock**: 36.0 s (within 45 s bash budget).
- **Output format**: 6 signals today (3 high-strength); dedup via MD5 hash in `signals/edgar_dedup.json`.

## What's working well (verified)
- **Category rotation** (D-030, D-031, D-037) — prevents any one keyword set from dominating signal flow. Today: `mna`; next: `bankruptcy` per `edgar_rotation_state.json`.
- **KEYWORD_SKIP_FORMS** and **CATEGORY_FORM_WHITELIST** — filters boilerplate from ARS/DEF 14A/S-1 proxy/registration-wave noise.
- **SPAC_IPO_FORM_BLACKLIST** — excludes S-4, 425, SC TO-C, 424B, DRS (the de-SPAC contamination source).
- **WALL_CLOCK_BUDGET_S = 35** — scanner exits gracefully before bash 45 s kill, protecting sandbox state.
- **MD5 dedup** — prevents re-emitting same filing across runs.

## Known issues (verified, from OPEN_QUESTIONS + DECISIONS)

### Q-009: Activist + governance blind during March–May proxy season
- **Root cause**: DEF 14A / ARS filings dominate EFTS search results and contain CIC/golden-parachute boilerplate that triggers "change in control" and "strategic alternatives" keywords.
- **Current workaround**: Rotation skips activist during proxy season.
- **Fix needed**: Form whitelist `{8-K, SC 13D, SC 13D/A, SC 14D9, PRER14A, DFAN14A}` for activist + raise mcap floor to $500M for this category.
- **Effort**: ~1 hour — both are config edits to `_scan_category()`.

### Q-010: Distress + M&A SPAC noise
- **Root cause**: S-4/A + DEFM14A + 425 legitimately contain "merger agreement", "going concern", etc. Partial fix (`SPAC_IPO_FORM_BLACKLIST`) landed; issuer-name blacklist pending.
- **Fix needed**: (a) extend blacklist to issuer-name patterns ("Acquisition Corp", "Capital Corp", "Blank Check"), (b) lift distress mcap floor from $215M → $500M.
- **Effort**: ~1 hour.

### Active warning #1 (SESSION_STATE): File-truncation bug
- S55 reported 3 scripts broken on its start; S56 reported 0. Intermittent regression between sessions.
- **Fix needed**: Pre-shutdown checksum validation added to session protocol. Not tool-specific.

## Data-structure observations (verified from source)
- Signal output conforms to common pipeline schema (ticker, isin, company_name, market_cap_mm, signal_type, signal_category, strength_estimate, source_url, source_date, scan_date, raw_data).
- `raw_data` contains `form`, `category`, `keyword`, `filing_date`, `cik`, `adsh`, `passage` (truncated 300 chars).
- Mcap triage uses `mcap_cache.get_market_cap_cached()` — shared with other scanners.

## What to build next (ranked)

**P1** (next session):
1. Apply Q-009 form whitelist + $500M floor for activist.
2. Apply Q-010 issuer-name blacklist + $500M floor for distress.

**P2** (1–2 weeks):
3. **Pre-passage scoring**: currently all M&A signals emit at strength 5 if any keyword hits. An 8-K with a headline "Entered merger agreement" (strength 5) and an S-1 containing "merger agreement" in risk factors (false positive) are indistinguishable. Add passage-level scoring: boilerplate-context detection downgrades to strength 2.
4. **Filing-size heuristic**: 8-Ks announcing real deals are typically <50 KB. Multi-MB files are usually 10-Q/10-K embedded mentions. Add size-based weighting.

**P3** (speculative, INFERRED benefit):
5. **Ticker-at-source**: EDGAR filings include CIK, not ticker. Current pipeline resolves ticker via OpenFIGI after the fact. Maintaining a CIK→ticker mapping file (refreshed monthly from SEC) would cut OpenFIGI calls ~70%.

## Synergy hooks
- Today's AVNS and GSAT hits could have cross-validated against ESMA shorts (bullish setup if shorts are crowded and being squeezed by the deal) — but neither ticker had ESMA positions.
- EDGAR distress + FDA PDUFA convergence = highest-value stack: "8-K mentioning going concern while a PDUFA approaches" = binary event with survival risk. Add to convergence directionality classifier.

## Verification notes
- All claims above verified against source code (`edgar_filing_monitor.py` read through) and `SESSION_STATE.md` (dated 2026-04-14 16:55 UTC).
- Today's S56 run results verified against `reports/2026-04-14_daily_report.md`.
- Rotation state behavior verified against in-code reference to `signals/edgar_rotation_state.json`.
