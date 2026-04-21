# ESMA Short Scanner — Diagnostic
**Tool**: `tools/esma_short_scanner.py` v2.0
**Grade**: **B+** — all 4 regulators live, signal flow healthy, but regime change looming

---

## What it does (verified)
Downloads short-position disclosures from four EU/UK regulators (FCA, AMF, AFM, BaFin). Diffs against prior snapshot. Detects five signal types: new position, position increase, position decrease, crowded short (≥3 independent holders on same ISIN), large position (≥2%). Resolves ISIN → ticker via OpenFIGI with country suffix (.L, .DE, .PA, .AS, etc.). Emits standard pipeline signals.

## Current health (verified)
- **Today's live-run (S56, 2026-04-14)**: 15 signals, **5 high-strength crowded-new-position stacks** — SW.PA (Sodexo), VU.PA (Vusiongroup), RAND.AS (Randstad), MICC.AS (Magnum Ice Cream), SDF.DE (K+S). Another 10 medium strength.
- **Positions scanned**: 2,223 across all regulators; 177 ISINs crowded.
- **Wall-clock**: 17.0 s — fast.
- **Compilation**: `py_compile` clean.
- **APIs**: FCA XLSX 200, AMF CSV 200 (188 positions Apr 14), AFM CSV 200, BaFin CSV 200 (with session-cookie workaround).

## What's working well (verified)
- **Multi-regulator fan-out** — if one regulator goes dark, the others continue. Today's result: all 4 green.
- **AMF auto-discovery** — `_discover_amf_csv_url()` uses data.gouv.fr API to find the current day's CSV. Resilient to URL changes.
- **BaFin session-cookie handshake** — BaFin requires the user first visit the page before the CSV endpoint works. Handled via `requests.Session()`.
- **ISIN→ticker cache** — `esma_ticker_cache.json` persists across runs. Current FIGI rate limit = 25 calls/run.
- **Snapshot-based diff** — prior-snapshot comparison drives change-pct signals. Snapshots stored per-regulator per-day.
- **Country-suffix mapping** — 17 countries mapped; tickers emitted with correct Yahoo/native-exchange suffix.

## Known issues (verified)

### Q-008: FCA June 2026 regime change (CALENDAR RISK)
- UK short-selling regime transitions to **aggregate anonymized** (ANSP) disclosures in June 2026, replacing individual disclosures at 0.5% threshold.
- **Impact**: Scanner loses ability to count independent holders in UK names (the core of crowded-short detection). Position sizes become aggregate only.
- **Mitigation options**: (a) Adapt to ANSP aggregate format — still useful for shock detection, (b) supplement with German/French sources (they're not changing), (c) accept degraded UK signal quality.
- **Runway**: ~6 weeks. This is the single biggest known risk to the strategy.

### CONSOB (Italy) + CNMV (Spain) blocked
- CONSOB has Radware bot protection → 403.
- CNMV returns 403 from Python sandbox. CNMV is Pedro's home market (see Q-002).
- **Current posture**: Deferred. Coverage is UK + DE + FR + NL only.

### Snapshot purging / history (not-yet-fixed)
- Scanner saves one snapshot per regulator per day but never purges. Over many months this becomes hundreds of files.
- Also: `diff_snapshots()` uses *yesterday's* snapshot only. For "crowded short that wasn't crowded last week" detection, we need N-day lookback.

## Data-structure observations (verified from source)
- Internal position model: `{regulator, holder_name, target_company, isin, position_pct, position_date, previous_position_pct, change_pct, disclosure_date}`.
- Signal `raw_data` includes full crowded-short constituent list (holders + pct + date + regulator).
- ISIN suffix map (line 70–75) is hardcoded — adding new countries requires code edit.

## What to build next (ranked)

**P1** (next 1–2 sessions):
1. **Historical crowded-short tracking** — store a rolling 14-day crowded-short state file (`esma_crowded_history.json`). Distinguish "newly crowded" (high signal) from "persistently crowded" (low signal).
2. **Snapshot retention policy** — keep last 14 snapshots per regulator, archive older to `esma_snapshots/archive/`.

**P2** (2–4 weeks):
3. **FCA ANSP-format prep** — write an adapter for the forthcoming aggregate format. Land before June 2026.
4. **Daily position-size-delta sparklines** — a holder quietly adding 0.2 pp/week for 4 weeks is a different signal from a 0.8 pp one-shot. Track trajectory.
5. **Holder-reputation tracking** — maintain a list of historically-accurate shorts (e.g., D.E. Shaw, Marshall Wace) and boost their signals vs. unknown funds.

**P3** (speculative):
6. **CNMV via Claude in Chrome** — if Pedro wants Spain coverage. Adds browser-automation complexity. INFERRED value depends on Pedro's portfolio concentration.
7. **Nordics coverage** (Finansinspektionen, Finanstilsynet) — free CSV disclosures exist. Similar pattern to AFM.

## Signal quality note
Today's top-5 crowded-new-position stacks (SW.PA, VU.PA, RAND.AS, MICC.AS, SDF.DE) — SPECULATED: the consumer-staples/staffing cluster suggests a coordinated macro short. Worth a targeted investigation when Pedro has bandwidth.

## Synergy hooks
- **Crowded shorts + M&A EDGAR** — if a crowded-short name gets acquired, it's a squeeze. Convergence engine already supports this cross-strategy match.
- **Crowded shorts + FDA PDUFA** — high short interest into a PDUFA is pre-priced pessimism; approval outcome is maximally asymmetric.

## Verification notes
- All claims above verified against source code (`esma_short_scanner.py` full read).
- Today's run data verified against `reports/2026-04-14_daily_report.md` and SESSION_STATE.md §Tool Health.
- Q-008 regime change dates verified against OPEN_QUESTIONS.md (claims trace to FCA publications — not re-verified in this audit).
