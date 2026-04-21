# Unified Investment Discovery System — Implementation Plan

**Date**: 2026-04-16
**Author**: Pedro + Claude (planning session)
**Target executor**: Fresh Opus 4.7 session in this same project folder
**Status**: APPROVED FOR EXECUTION

---

## EXECUTIVE SUMMARY

This plan consolidates 6 scattered project folders (Investment tool, Investmet tool Beta, Investment tool Delta, Investment tool Gamma, Reporting Hub, Independent review project set up) into a single unified investment discovery system. The new system keeps all proven scanners, adds new ones, introduces per-signal-type scoring profiles, and implements a clean reporting layer that produces PDFs without interfering with operational data.

**What gets built**: One unified system with ~16 scanners, 5 scoring profiles, a cross-signal convergence engine, and a PDF reporting layer.
**What gets retired**: Contract Monitor, Silence Scanner (Tool 4 / Gamma), Reporting Hub (replaced by built-in reporting), Independent Review (absorbed into convergence engine). All old scattered folders are archived, not deleted.
**Scheduled tasks**: 3 tasks replace the current 10 (6 active + 4 retired).

---

## PART 1 — WHAT EXISTS TODAY (READ THESE FILES)

Before writing ANY code, the executor MUST read these source files to understand the current working state. Do not skip this — the existing tools have 67+ sessions of battle-tested logic.

### Tool 1 — US Investment Discovery (OPERATIONAL)
**Root**: `C:\Users\javie\OneDrive\Desktop\Claude Cowork\Conan\Investment tool\investment_discovery_system\`
**Bash**: `/sessions/.../mnt/Conan/Investment tool/investment_discovery_system/`

Read these:
- `INSTRUCTIONS.md` — the operational discipline backbone. The cold-start protocol, session rules, anti-early-stop rules, shutdown protocol, and work-until-limit mandate ALL carry forward to the unified system verbatim.
- `SESSION_STATE.md` — current state (Session 67+). Has 6 active candidates (AXSM, RPAY, RGR, VERA, AVNS, GSAT) + 1 watchlist (SEM).
- `framework/scoring_system.md` — the current 7-dimension rubric. This is being replaced by 5 profile-specific rubrics, but the structure and discipline carry forward.
- `framework/candidate_template.md` — the candidate dossier template. Largely preserved.
- `DECISIONS.md` — 52+ decisions. Key ones: D-014 (subprocess isolation), D-018 (EDGAR wall-clock budget), D-047 (maintenance task), D-052 (atomic writes for truncation bug).
- `OPEN_QUESTIONS.md` — Q-016 (terminal-marker validation) should be implemented in the unified maintenance task.
- `strategies/*.md` — 5 strategy specs (edgar, esma, congressional, fda_pdufa, sam_gov_contracts). The sam_gov one is retired but keep for reference.

Migrate these tools:
- `tools/edgar_filing_monitor.py` (v2.4) — KEEP, refocus on activist/governance/distress
- `tools/esma_short_scanner.py` (v2.0) — KEEP, add historical tracking
- `tools/fda_pdufa_pipeline.py` (v2.0) — KEEP as-is
- `tools/congressional_trading.py` (v2.0) — KEEP with Ro Khanna filter (Q-014)
- `tools/openfigi_resolver.py` (v3) — KEEP, shared across all scanners
- `tools/convergence_engine.py` (v1.4) — REDESIGN as multi-profile convergence
- `tools/mcap_cache.py` — KEEP, shared
- `tools/pipeline_runner.py` (v1.1) — REDESIGN as unified dispatcher with scanner registry
- `tools/run_post_scan.py` — REDESIGN as unified post-scan
- `tools/run_scanner.py` — KEEP pattern (one scanner per subprocess)

Do NOT migrate:
- `tools/contract_monitor.py` — RETIRED (zero output after 67 sessions)
- `tools/companies_house_monitor.py`, `tools/google_trends_scanner.py`, `tools/uk_gazette_insolvency_scanner.py` — non-operational utilities, do not migrate

### Tool 2 — Non-US Discovery (OPERATIONAL, mid-build)
**Root**: `C:\Users\javie\OneDrive\Desktop\Claude Cowork\Conan\Investmet tool Beta\non_us_discovery_system\`
**Bash**: `/sessions/.../mnt/Conan/Investmet tool Beta/non_us_discovery_system/`

Read these:
- `INSTRUCTIONS.md` — the non-US adaptation. Key differences: $300M market cap floor (Tool 2 used $300M; Tool 1 used $215M — the unified system adopts $300M as the universal floor), translation_confidence field, cross-listing dedup via issuer_figi, MIC-based entity resolution.
- `SESSION_STATE.md` — current state. 24 candidates, LSE/TDnet/ASX operational, SEDAR+ blocked.
- `CONTEXT.md` — validated API endpoints for all 9 exchanges.
- `strategies/*.md` — 9 strategy specs. ALL carry forward.

Migrate these tools:
- `tools/lse_rns_scanner.py` — KEEP (producing candidates, caught ITRK.XLON)
- `tools/tdnet_scanner.py` — KEEP but FIX the FIGI-resolve defect for Japanese tickers
- `tools/asx_scanner.py` — KEEP (stable chunked processing)
- `tools/sedar_plus_scanner.py` — KEEP, fix the `working/ca_universe.json` blocker
- `tools/openfigi_resolver.py` — already shared design, merge with Tool 1's version

Build these (not yet coded):
- `tools/hkex_scanner.py` — use strategy spec `strategy_hk_hkex.md`
- `tools/kind_scanner.py` — use strategy spec `strategy_kr_kind.md`
- `tools/bse_nse_scanner.py` — use strategy spec `strategy_in_bse_nse.md`
- `tools/cvm_scanner.py` — use strategy spec `strategy_br_cvm.md`
- `tools/bmv_scanner.py` — use strategy spec `strategy_mx_bmv.md`

### Tool 3 — Litigation (Phase 0-1, mostly scaffold)
**Root**: `C:\Users\javie\OneDrive\Desktop\Claude Cowork\Conan\Investment tool Delta\litigation_system\`
**Bash**: `/sessions/.../mnt/Conan/Investment tool Delta/litigation_system/`

Read these:
- `INSTRUCTIONS.md` — 6-layer architecture, party resolution protocol, signal pipeline. The full 6-channel design is overengineered for initial build. SIMPLIFY to 2 channels first.
- `CONTEXT.md` — domain background, entity resolution protocol (CIK → ticker → FIGI), validated endpoints.
- `DECISIONS.md` — D-003 (two-stage party resolution), D-004 (common signal schema), D-008 (PACER cost control), D-015 (per-host UA dispatch), D-016 (Delaware Chancery redesign).
- `strategies/strategy_federal_civil.md` — CourtListener RECAP API. BUILD FIRST.
- `strategies/strategy_sec_enforcement.md` — sec.gov litigation releases. BUILD SECOND.

Migrate these tools:
- `tools/party_resolver.py` — KEEP (coded but untested, needs live validation)
- `tools/http_client.py` — KEEP (rate-limited HTTP wrapper, useful for all scanners)
- `tools/build_exhibit21_map.py` — KEEP (needs to run once to populate baselines)

Build these (simplified from original 6-channel to 2-channel initial):
- `tools/courtlistener_scanner.py` — Federal civil cases via CourtListener RECAP API (free, verified)
- `tools/sec_enforcement_scanner.py` — SEC litigation releases + EDGAR enforcement

Defer these (build after 2-channel proves out):
- `tools/ptab_ipr_scanner.py` — PTAB v2 is decommissioned; v3 WAF-gated (Q-004)
- `tools/itc_337_scanner.py` — ITC EDIS REST spec unclear (Q-005)
- `tools/delaware_chancery_scanner.py` — redesigned per D-016 but not built
- `tools/doj_ftc_antitrust_scanner.py` — lower priority

### What gets DELETED (archive first, then remove from active)
- `Investment tool Gamma/` — Silence scanner concept. Archive entirely. Do not build.
- `Reporting Hub/` — Archive entirely. Replaced by built-in reporting layer.
- `Independent review project set up/` — Archive entirely. Absorbed into convergence engine.
- `Tool Audit 2026-04-14/` — Keep as historical reference, no migration needed.

---

## PART 2 — UNIFIED FOLDER STRUCTURE

Create this structure at: `C:\Users\javie\OneDrive\Desktop\Claude Cowork\Conan\unified_system\`
Bash equivalent: The bash mount path varies per session. Use the path mapping shown in your system prompt to translate Windows paths to sandbox paths. The Conan folder is always mounted under `/sessions/<session-name>/mnt/Conan/`.

```
unified_system/
├── INSTRUCTIONS.md              ← NEW (adapted from Tool 1, covers all scanners)
├── SESSION_STATE.md             ← NEW (seeded from Tool 1 + Tool 2 current state)
├── SESSION_LOCK.md              ← Operational lock (writer tasks only)
├── DECISIONS.md                 ← NEW (start at D-001, reference legacy decisions)
├── OPEN_QUESTIONS.md            ← NEW (migrate unresolved items from all tools)
├── PROGRESS_LOG.md              ← NEW (start fresh, reference legacy logs)
├── INDEX.md                     ← NEW
├── OBJECTIVES.md                ← NEW (updated mandate covering all geographies + litigation)
├── CONTEXT.md                   ← NEW (consolidated API reference table)
│
├── framework/
│   ├── profile_merger_arb.md         ← Scoring profile 1
│   ├── profile_activist_governance.md ← Scoring profile 2
│   ├── profile_binary_catalyst.md     ← Scoring profile 3
│   ├── profile_short_positioning.md   ← Scoring profile 4
│   ├── profile_litigation.md          ← Scoring profile 5
│   └── candidate_template.md          ← Adapted from Tool 1 (profile-aware)
│
├── strategies/                   ← Per-scanner specs (migrated + new)
│   ├── us_edgar_activist_governance.md
│   ├── us_edgar_distress.md
│   ├── eu_esma_short.md
│   ├── us_fda_pdufa.md
│   ├── us_congressional.md
│   ├── uk_lse_rns.md
│   ├── jp_tdnet.md
│   ├── au_asx.md
│   ├── ca_sedar_plus.md
│   ├── hk_hkex.md
│   ├── kr_kind.md
│   ├── in_bse_nse.md
│   ├── br_cvm.md
│   ├── mx_bmv.md
│   ├── lit_courtlistener_federal.md
│   └── lit_sec_enforcement.md
│
├── tools/                        ← ALL Python scripts
│   ├── pipeline_runner.py        ← Unified dispatcher with scanner registry
│   ├── openfigi_resolver.py      ← Merged from Tool 1 + Tool 2
│   ├── convergence_engine.py     ← Redesigned for multi-profile matching
│   ├── mcap_cache.py             ← From Tool 1
│   ├── http_client.py            ← From Tool 3 (rate-limited, backoff, per-host UA)
│   ├── party_resolver.py         ← From Tool 3 (litigation entity matching)
│   ├── run_post_scan.py          ← Unified post-scan (scoring + convergence + digest)
│   ├── report_generator.py       ← NEW: PDF generation for daily digest + dossiers + weekly
│   │
│   ├── edgar_filing_monitor.py   ← From Tool 1
│   ├── esma_short_scanner.py     ← From Tool 1, enhanced with historical tracking
│   ├── fda_pdufa_pipeline.py     ← From Tool 1
│   ├── congressional_trading.py  ← From Tool 1
│   ├── lse_rns_scanner.py        ← From Tool 2
│   ├── tdnet_scanner.py          ← From Tool 2, FIGI defect fixed
│   ├── asx_scanner.py            ← From Tool 2
│   ├── sedar_plus_scanner.py     ← From Tool 2, ca_universe blocker fixed
│   ├── hkex_scanner.py           ← NEW BUILD
│   ├── kind_scanner.py           ← NEW BUILD
│   ├── bse_nse_scanner.py        ← NEW BUILD
│   ├── cvm_scanner.py            ← NEW BUILD
│   ├── bmv_scanner.py            ← NEW BUILD
│   ├── courtlistener_scanner.py  ← NEW BUILD
│   ├── sec_enforcement_scanner.py ← NEW BUILD
│   └── build_exhibit21_map.py    ← From Tool 3 (run once for litigation baselines)
│
├── config/
│   ├── scanner_registry.json     ← Cadences, endpoints, last_run, scoring_profile
│   └── entity_cache.json         ← Persistent OpenFIGI + party resolution cache
│
├── signals/
│   └── signal_log.json           ← Unified signal log (all scanners)
│
├── candidates/                   ← Source markdown dossiers (written by operational task)
│   ├── delivered/                ← Resolved outcomes
│   └── archive/                  ← Superseded writeups
│
├── reports/                      ← ALL reporting output (written ONLY by reporting task)
│   ├── REPORTING_LOCK.md         ← Reporting lock (independent from SESSION_LOCK)
│   ├── candidates_index.json     ← Machine-readable candidate registry
│   ├── daily/                    ← Daily digest PDFs
│   ├── weekly/                   ← Weekly strategic report PDFs
│   └── dossiers/
│       └── pdf/                  ← Per-candidate dossier PDFs
│
├── working/                      ← Scratch, in-progress, logs
├── research/                     ← Persistent investigative notes
├── baselines/                    ← exhibit21_map, party_cache, scanner baselines
└── archive/                      ← Superseded files (never delete)
```

---

## PART 3 — SCORING PROFILES

Replace the single 7-dimension rubric with 5 profile-specific scorecards. All profiles produce a final score on a **0–50 normalized scale** so the convergence engine and candidate pipeline can compare across types.

### Profile 1: Merger Arbitrage / Announced Deals
**Applies to**: EDGAR M&A filings, TDnet tender offers, LSE firm offers (Rule 2.7), ASX schemes of arrangement, SEDAR+ plans of arrangement, any announced transaction.
**File**: `framework/profile_merger_arb.md`

| Dimension | Weight | What it measures |
|-----------|--------|------------------|
| Spread Size | ×3 | Gross spread to deal price. 5 = >10%, 4 = 5-10%, 3 = 3-5%, 2 = 1-3%, 1 = <1% |
| Deal Certainty | ×2.5 | Regulatory risk, financing conditions, shareholder vote, MAC risk. 5 = unconditional/minimal, 1 = multiple serious conditions |
| Annualized Return | ×2 | Spread ÷ time to close, annualized. 5 = >20% ann., 4 = 12-20%, 3 = 8-12%, 2 = 4-8%, 1 = <4% |
| Break Risk | ×1.5 | Downside to unaffected price if deal fails. 5 = <10% downside, 1 = >40% downside |
| Liquidity | ×1 | Same as current: avg daily volume and tradability |

**Max**: 15 + 12.5 + 10 + 7.5 + 5 = **50**
**Thresholds**: 35+ Immediate, 25-34 Watchlist, 15-24 Archive, <15 Discard
**Key rule**: If annualized return < risk-free rate + 3%, auto-cap at Watchlist regardless of other scores. (This would have correctly flagged SEM's 1.1% annualized as not actionable.)

### Profile 2: Activist / Governance Events
**Applies to**: EDGAR 13D/14A filings, activist campaigns, proxy fights, poison pill situations, board disputes.
**File**: `framework/profile_activist_governance.md`

| Dimension | Weight | What it measures |
|-----------|--------|------------------|
| Signal Strength | ×2 | Strength of causal link to price movement (same as current) |
| Information Asymmetry | ×2 | How few market participants are aware (same as current, but higher weight) |
| Activist Track Record | ×1.5 | Does this fund/activist have a history of successful campaigns? 5 = proven (Elliott, Icahn, Starboard), 1 = unknown first-timer |
| Risk/Reward | ×1.5 | Asymmetry of potential payoff (same as current) |
| Catalyst Clarity | ×1 | How bounded is the timeline |
| Edge Decay | ×1 | How long the informational advantage persists |
| Liquidity | ×1 | Tradability |

**Max**: 10 + 10 + 7.5 + 7.5 + 5 + 5 + 5 = **50**
**Thresholds**: 35+ Immediate, 25-34 Watchlist, 15-24 Archive, <15 Discard

### Profile 3: Binary Catalyst (FDA PDUFA, clinical readouts)
**Applies to**: FDA PDUFA dates, AdCom votes, Phase 3 readouts, regulatory decisions.
**File**: `framework/profile_binary_catalyst.md`

| Dimension | Weight | What it measures |
|-----------|--------|------------------|
| Approval Probability | ×2.5 | Based on clinical data, regulatory signals (priority review, breakthrough, AdCom). 5 = >80%, 4 = 60-80%, 3 = 40-60%, 2 = 20-40%, 1 = <20% |
| Market Mispricing | ×2.5 | Gap between estimated probability and market-implied probability. 5 = >20% gap, 4 = 10-20%, 3 = 5-10%, 2 = 2-5%, 1 = <2% |
| Magnitude of Move | ×1.5 | Expected price move on positive outcome. 5 = >50%, 4 = 30-50%, 3 = 15-30%, 2 = 5-15%, 1 = <5% |
| Competitive Landscape | ×1.5 | Is there a better drug pending? Competitive approval risk. 5 = first-in-class/no competition, 1 = me-too in crowded field |
| Catalyst Timeline | ×1 | Urgency (same as current) |
| Liquidity | ×1 | Tradability |

**Max**: 12.5 + 12.5 + 7.5 + 7.5 + 5 + 5 = **50**
**Thresholds**: 35+ Immediate, 25-34 Watchlist, 15-24 Archive, <15 Discard
**Key rule**: Expected value calculation is primary: EV = (P_approval × upside) - (P_rejection × downside). If EV < 5%, auto-cap at Watchlist.

### Profile 4: Short Positioning / Flow Signals
**Applies to**: ESMA short disclosures, Form 4 insider transactions (future), short interest data.
**File**: `framework/profile_short_positioning.md`

| Dimension | Weight | What it measures |
|-----------|--------|------------------|
| Crowding Intensity | ×2.5 | Number of independent short-sellers / insider sellers. 5 = 6+ holders or cluster of C-suite, 1 = single holder / single minor insider |
| Trend Direction | ×2 | Building vs. unwinding. 5 = rapid new buildup (new positions opening), 1 = steady unwinding |
| Catalyst Proximity | ×2 | Is there an upcoming event that could force covering / validate the positioning? 5 = catalyst within 2 weeks, 1 = no visible catalyst |
| Position Size Relative to Float | ×1.5 | Aggregate short/insider position as % of float. 5 = >10%, 4 = 5-10%, 3 = 2-5%, 2 = 1-2%, 1 = <1% |
| Historical Analog | ×1 | What happened to other names with similar positioning profiles? 5 = strong historical pattern, 1 = no relevant precedent |
| Liquidity | ×1 | Tradability |

**Max**: 12.5 + 10 + 10 + 7.5 + 5 + 5 = **50**
**Thresholds**: 35+ Immediate, 25-34 Watchlist, 15-24 Archive, <15 Discard
**Key distinction**: Distinguish between "crowded shorts approaching a catalyst" (potentially explosive, high score) and "steady-state shorts in a declining business" (not actionable, low score). The Catalyst Proximity dimension is what separates them.

### Profile 5: Litigation / Legal Events
**Applies to**: CourtListener federal civil cases, SEC enforcement actions, future litigation channels.
**File**: `framework/profile_litigation.md`

| Dimension | Weight | What it measures |
|-----------|--------|------------------|
| Financial Materiality | ×3 | Damages/exposure as % of enterprise value. 5 = >20% EV, 4 = 10-20%, 3 = 5-10%, 2 = 2-5%, 1 = <2% |
| Legal Outcome Probability | ×2 | Based on case type, procedural stage, precedent. 5 = near-certain (consent decree, settlement), 4 = strong (motion to dismiss denied + discovery), 3 = uncertain, 2 = weak (early stage), 1 = speculative |
| Market Pricing | ×2 | Has the stock already moved on this litigation? 5 = no move (market unaware), 1 = fully priced in |
| Resolution Timeline | ×1.5 | 5 = within 1 month, 4 = 1-3 months, 3 = 3-6 months, 2 = 6-12 months, 1 = 1+ year |
| Liquidity | ×1 | Tradability |
| Party Resolution Confidence | ×0.5 | How confident is the entity match? 5 = exact CIK match, 3 = fuzzy match >0.85, 1 = uncertain |

**Max**: 15 + 10 + 10 + 7.5 + 5 + 2.5 = **50**
**Thresholds**: 35+ Immediate, 25-34 Watchlist, 15-24 Archive, <15 Discard
**Key rule**: If Party Resolution Confidence < 3 (i.e., match confidence < 0.85), auto-cap at Archive regardless of other scores. Never promote a candidate when you're not sure you have the right company.

### Scanner-to-Profile Mapping

| Scanner | Default Profile | Override conditions |
|---------|----------------|---------------------|
| edgar_filing_monitor (M&A signals) | Merger Arb | If activist/governance filing → Activist profile |
| edgar_filing_monitor (distress signals) | Activist/Governance | — |
| esma_short_scanner | Short Positioning | — |
| fda_pdufa_pipeline | Binary Catalyst | — |
| congressional_trading | Activist/Governance | — |
| lse_rns_scanner (firm offers) | Merger Arb | If possible offer (Rule 2.4) → Activist profile |
| lse_rns_scanner (other) | Activist/Governance | — |
| tdnet_scanner (tender offers) | Merger Arb | If impairment/earnings → varies |
| asx_scanner | Merger Arb or Activist | Depends on signal_type |
| sedar_plus_scanner | Merger Arb or Activist | Depends on signal_type |
| hkex_scanner | Merger Arb or Activist | Depends on signal_type |
| kind_scanner | Merger Arb or Activist | Depends on signal_type |
| bse_nse_scanner | Merger Arb or Activist | Depends on signal_type |
| cvm_scanner | Merger Arb or Activist | Depends on signal_type |
| bmv_scanner | Merger Arb or Activist | Depends on signal_type |
| courtlistener_scanner | Litigation | — |
| sec_enforcement_scanner | Litigation | — |

The `signal_type` field in each signal's JSON determines which profile applies. The `run_post_scan.py` scoring step reads the signal_type, looks up the default profile in `scanner_registry.json`, applies any override conditions, and scores using the matched profile rubric.

---

## PART 4 — CONVERGENCE ENGINE REDESIGN

The convergence engine is the analytical core. It sits downstream of ALL scanners and detects when multiple independent signals point to the same entity.

### How it works

1. After all due scanners run and signals are scored, the convergence engine reads the rolling signal log (14-day window for most profiles, 30-day for litigation).
2. Groups signals by `issuer_figi` (the canonical entity key, resolved via OpenFIGI).
3. For any entity with signals from 2+ different scanners within the window:
   a. Checks `source_content_hash` pairwise — if two signals are echoes of the same underlying event across different exchanges/listings, they are deduplicated (not counted as convergence).
   b. Classifies the convergence type:
      - **Same-direction** (both long, or both short) → highest conviction
      - **Orthogonal** (one is event-driven, other is positioning) → high conviction
      - **Contradiction** (one long, one short) → flag for manual review, do NOT auto-score
   c. Applies convergence bonus to the entity's highest-scoring signal:
      - 2 independent signals: +5 bonus
      - 3+ independent signals: +10 bonus
      - Contradiction: no bonus, flag only

### Cross-profile convergence examples (the high-value cases)

- ESMA short buildup + FDA PDUFA approaching = "pessimistic positioning into binary catalyst" → explosive if approval
- EDGAR activist filing + ESMA short covering on same name = "smart money shifting" → strong directional signal
- Litigation ruling + EDGAR distress keywords = "legal + financial stress compounding" → high-conviction short
- LSE RNS possible offer + Congressional trading on same entity = "informed political buying into M&A" → rare but very high signal
- TDnet tender offer + ESMA short covering on cross-listed name = "forced covering on announced deal" → merger arb confirmation

### What changes from current convergence engine

- Current: only checks within Tool 1's 5 scanners. New: checks across ALL scanners.
- Current: 14-day fixed window. New: 14 days for most, 30 days for litigation (courts move slowly).
- Current: +4/+8 bonus. New: +5/+10 bonus (higher to reflect the stronger signal of cross-system convergence).
- Current: no direction classification. New: same-direction/orthogonal/contradiction.
- Current: dedup by signal_id. New: dedup by source_content_hash (catches cross-listing echoes).

---

## PART 5 — REPORTING LAYER

### Design principles

1. **Read-only access to operational data.** The reporting task NEVER writes to `candidates/`, `signals/`, `SESSION_STATE.md`, `tools/`, or any file outside `reports/`. This is absolute.
2. **Own lock, own scope.** The reporting task uses `reports/REPORTING_LOCK.md`, completely independent from `SESSION_LOCK.md`. It can run in parallel with operational/maintenance tasks.
3. **PDF output only.** All deliverables are PDFs. No DOCX.
4. **JSON read-retry for tolerance.** If a JSON file is malformed (likely mid-write by operational task), retry after 2 seconds, up to 3 attempts. On 3rd failure, skip that data source for this cycle and log.

### Output 1: Daily Signal Digest (PDF)
**Path**: `reports/daily/YYYY-MM-DD_digest.pdf`
**Produced**: Every reporting cycle (every 4 hours)
**Content** (target: under 2 pages):
- **Headline** — single sentence: highest-priority finding
- **New signals** — grouped by scoring profile, 3-line summaries
- **Candidate status changes** — promotions, demotions, kills since last digest
- **Convergence alerts** — any cross-scanner convergences detected
- **Catalyst calendar** — next 14 days of upcoming events for all active candidates
- **System health** — 3-line block: scanner status, entity resolution issues, any warnings

### Output 2: Candidate Dossier PDFs
**Path**: `reports/dossiers/pdf/TICKER[_MIC]_YYYY-MM-DD.pdf`
**Produced**: When a candidate is new or has material updates (detected by comparing `candidates/*.md` timestamps against `candidates_index.json`)
**Content**: PDF render of the candidate markdown dossier, using the appropriate scoring profile breakdown. Includes all sections from the candidate template.

### Output 3: Weekly Strategic Report (PDF)
**Path**: `reports/weekly/YYYY-WW_strategic.pdf`
**Produced**: On Sunday reporting cycles only
**Content**:
- Scanner health trends (signals produced per scanner, 7-day trend)
- Candidate pipeline metrics (active, watch, killed, delivered counts + trend)
- Hit rates by scoring profile (what % of signals per profile become candidates)
- Coverage gaps (markets/sectors with no signal flow)
- Convergence statistics (convergences detected, near-misses)
- Recommendations (what to tune, add, or remove)

### Output 4: candidates_index.json
**Path**: `reports/candidates_index.json`
**Produced**: Every reporting cycle (updated atomically via temp+rename)
**Content**: Machine-readable registry of all candidates. Schema:
```json
{
  "ticker": "AXSM",
  "mic": null,
  "source_tool": "fda_pdufa",
  "scoring_profile": "binary_catalyst",
  "score": 38.5,
  "status": "active",
  "conviction": "high",
  "thesis_direction": "long",
  "hypothesis": "AXS-05 for Alzheimer disease agitation has 60-70% approval probability...",
  "next_key_dates": ["2026-04-30: PDUFA decision"],
  "last_updated": "2026-04-16",
  "dossier_path": "reports/dossiers/pdf/AXSM_2026-04-16.pdf"
}
```

### PDF generation approach

Use Python `reportlab` for PDF generation. Install: `pip install reportlab --break-system-packages`. Do NOT use pypdf for creation (it's a reader/writer, not a creator). Do NOT use weasyprint or wkhtmltopdf (complex dependencies). reportlab is pure Python, installs cleanly in the sandbox, and produces clean PDFs.

The `tools/report_generator.py` script should have three functions:
- `generate_daily_digest(signals, candidates, convergences) → PDF path`
- `generate_candidate_dossier(candidate_md_path, scoring_profile) → PDF path`
- `generate_weekly_strategic(scanner_stats, pipeline_stats) → PDF path`

---

## PART 6 — SCHEDULED TASKS

### Task 1: `unified-operational`
**Cron**: `0 */3 * * *` (every 3 hours)
**Lock**: `SESSION_LOCK.md` (acquires at start, releases at end)
**Write scope**: Everything in `unified_system/` EXCEPT `reports/`

**Prompt flow**:
1. Cold-start: read SESSION_STATE → INSTRUCTIONS → OPEN_QUESTIONS (if flagged)
2. Install deps: `pip install requests beautifulsoup4 lxml yfinance openpyxl pandas python-dateutil feedparser pypdf rapidfuzz reportlab --break-system-packages`
3. Acquire SESSION_LOCK (check first, abort if locked <4h)
4. Tool Validation Protocol: py_compile all tools, probe endpoints, terminal-marker check (Q-016)
5. Read scanner_registry.json → determine which scanners are due based on cadence + last_run
6. Run each due scanner as subprocess (120s hard-kill, per-scanner soft budget)
7. Triage + entity resolution (OpenFIGI) on all new signals
8. Score each signal using its matched scoring profile
9. Run convergence engine across 14/30-day rolling window
10. Promote/demote candidates based on scores + convergence
11. Deep dive on any new 35+ scores or convergences
12. Monitor existing candidates against kill conditions
13. Update SESSION_STATE, PROGRESS_LOG, INDEX
14. Release SESSION_LOCK

**Anti-early-stop rules**: Carried forward verbatim from Tool 1. Work until usage limit. Running scanners is step 1 of many. There is ALWAYS more work.

### Task 2: `unified-maintenance`
**Cron**: `50 */3 * * *` (10 minutes before each operational cycle)
**Lock**: `SESSION_LOCK.md` (same lock as operational — mutual exclusion)
**Write scope**: `unified_system/` (health fixes only — never touches candidates, scoring, signals)

**Prompt flow**:
1. Cold-start: read SESSION_STATE → INSTRUCTIONS
2. Install deps
3. Acquire SESSION_LOCK (abort if locked)
4. py_compile all tools + terminal-marker check
5. Endpoint reachability probes for all active scanners
6. Signal log integrity check (valid JSON, no duplicates, no orphans)
7. Entity cache freshness audit
8. Scanner cadence audit (is any scanner overdue by >2× its cadence?)
9. Fix any compile errors or truncated files (atomic write method per D-052)
10. Update SESSION_STATE warnings, release lock

### Task 3: `unified-reporting`
**Cron**: `30 */4 * * *` (every 4 hours at :30)
**Lock**: `reports/REPORTING_LOCK.md` (independent from SESSION_LOCK)
**Write scope**: ONLY `reports/` — NEVER writes outside this folder

**Prompt flow**:
1. Acquire REPORTING_LOCK (abort if locked <4h)
2. Install deps: `pip install reportlab requests yfinance --break-system-packages`
3. READ (never write) from: `SESSION_STATE.md`, `signals/signal_log.json`, `candidates/*.md`, `config/scanner_registry.json`
4. For each read: if JSON malformed, retry 2s × 3 attempts, then skip + log
5. Generate daily digest PDF → `reports/daily/YYYY-MM-DD_HHMM_digest.pdf`
6. Check for new/updated candidates (compare `candidates/*.md` mtimes vs `candidates_index.json`)
7. For each new/updated candidate: generate dossier PDF → `reports/dossiers/pdf/`
8. Update `reports/candidates_index.json` (atomic write: tmp + rename)
9. If Sunday: also generate weekly strategic report PDF → `reports/weekly/`
10. Release REPORTING_LOCK

**CRITICAL**: This task's prompt must explicitly state: "You are a READ-ONLY consumer of operational data. You NEVER write to any file outside the `reports/` folder. You NEVER modify candidates, signals, SESSION_STATE, or any operational file. If you detect an issue in operational data, log it to `reports/working/issues_YYYY-MM-DD.log` — do not fix it. Fixing operational data is the maintenance task's job."

### Task timing — no collisions

```
HOUR:  :00              :30              :50
       ├─ OPERATIONAL ──┤                ├─ MAINTENANCE ──┤
       │  (SESSION_LOCK) │                │  (SESSION_LOCK) │
       │                 │                │                 │
       │                 ├─ REPORTING ───┤                 │
       │                 │  (REPORT_LOCK) │                 │
       │                 │  (reads only)  │                 │
```

- Operational runs at :00, typically finishes in 15-30 minutes
- Reporting runs at :30, after operational is likely done (but safe to overlap because read-only)
- Maintenance runs at :50, must finish before next operational at :00

Jitter (the system adds a few minutes of random offset) is handled naturally: SESSION_LOCK prevents operational + maintenance from overlapping even if jitter shifts them. Reporting is always safe because it never acquires SESSION_LOCK.

### Retiring old tasks

Disable all existing tasks before enabling new ones:
- `investment-tool-project` → disable
- `investment-tool-maintenance` → disable
- `non-us-operational` → disable
- `non-us-maintenance` → disable
- `reporting-hub-performance` → disable
- `reporting-hub-deep-dives` → disable
- `investment-tool-performance-report` (already disabled) → leave
- `investment-tool-deep-dives` (already disabled) → leave
- `non-us-performance-report` (already disabled) → leave
- `non-us-deep-dives` (already disabled) → leave

---

## PART 7 — COMMON SIGNAL JSON SCHEMA

All scanners emit signals in this unified format. This is the contract between scanners and the scoring/convergence layer.

```json
{
  "signal_id": "<stable unique hash>",
  "upstream_scanner": "edgar_filing_monitor",
  "scoring_profile": "activist_governance",
  
  "ticker": "RPAY",
  "ticker_local": null,
  "mic": "XNAS",
  "isin": null,
  "figi": "BBG00BN7PVD8",
  "issuer_figi": "BBG00BN7PVD8",
  "company_name": "Repay Holdings Corporation",
  "company_name_local": null,
  "market_cap_usd_mm": 450,
  "country": "US",
  
  "signal_type": "activist_13d",
  "signal_category": "edgar",
  "thesis_direction": "long",
  "strength_estimate": 4,
  
  "source_url": "https://...",
  "source_date": "2026-04-10",
  "scan_date": "2026-04-10T15:30:00Z",
  "source_content_hash": "<sha256>",
  "translation_confidence": null,
  
  "raw_data": {
    "filing_type": "SC 13D",
    "filer": "Forager Fund",
    "ownership_pct": 12.9
  }
}
```

Key fields for cross-system operation:
- `issuer_figi` — the convergence key. Resolves cross-listings to the same ultimate issuer.
- `scoring_profile` — which rubric to apply. Set by scanner based on signal_type.
- `source_content_hash` — SHA256 of the filing body. Used for cross-listing dedup.
- `translation_confidence` — only for non-English sources. Scores < 0.70 are dropped.
- `thesis_direction` — long/short/neutral/unknown. Required for convergence classification.

---

## PART 8 — MIGRATION SEQUENCE (PHASED)

Execute these phases in order. Each phase has a clear "done" gate before the next begins.

### Phase 0: Scaffold + Archive (Session 1)
1. Create the `unified_system/` folder structure per Part 2.
2. Write INSTRUCTIONS.md (adapted from Tool 1, covering all scanners and all profiles).
3. Write OBJECTIVES.md (updated mandate: all geographies + litigation).
4. Write CONTEXT.md (consolidated API reference from all three tools).
5. Write all 5 scoring profile files in `framework/`.
6. Write the adapted `candidate_template.md`.
7. Write `config/scanner_registry.json` with all 16+ scanners, their cadences, endpoints, and profile mappings.
8. Archive old folders: rename `Investment tool` → `_ARCHIVED_Investment_tool_2026-04-XX`, same for Beta, Delta, Gamma, Reporting Hub, Independent review. DO NOT DELETE — the archive preserves all history, decisions, and code.
9. Write initial SESSION_STATE.md, DECISIONS.md (D-001: "Unified system created per IMPLEMENTATION_PLAN.md"), PROGRESS_LOG.md, INDEX.md.

**Done gate**: Folder structure exists, all framework files written, old folders archived.

### Phase 1: Migrate US Scanners (Sessions 2-3)
1. Copy Tool 1 scanner scripts into `unified_system/tools/`.
2. Copy `openfigi_resolver.py`, `mcap_cache.py` into `tools/`.
3. Copy `http_client.py` from Tool 3 into `tools/`.
4. Adapt `pipeline_runner.py` to read `scanner_registry.json` and dispatch only due scanners.
5. Adapt `run_post_scan.py` to score using the appropriate profile (reading `scoring_profile` from each signal).
6. Redesign `convergence_engine.py` for multi-profile matching per Part 4.
7. Refocus EDGAR: apply Q-009 proxy-season whitelist, Q-010 SPAC filter, Q-014 Ro Khanna filter. Adjust signal_type classification to separate M&A from activist/governance/distress.
8. Run full Tool 1 pipeline end-to-end in unified system. Verify all 6 active candidates + 1 watchlist produce consistent scores under new profiles.
9. Migrate existing candidates from `Investment tool/investment_discovery_system/candidates/` into `unified_system/candidates/` — update scoring breakdown to use new profile format.

**Done gate**: All 4 US scanners run successfully in unified system. Existing candidates migrated. Scores under new profiles are reasonable (may differ from old scores — that's expected).

### Phase 2: Migrate Non-US Scanners (Sessions 3-4)
1. Copy LSE, TDnet, ASX, SEDAR+ scanners into `tools/`.
2. Merge Tool 2's OpenFIGI resolver logic (MIC-based resolution, cross-listing awareness) into the unified `openfigi_resolver.py`.
3. Fix TDnet FIGI-resolve defect: 5-char JPX tickers (e.g., `469A0`) must be stripped to `469A.T` format.
4. Fix SEDAR+ blocker: build `working/ca_universe.json` via `ca_universe` builder.
5. Run LSE + TDnet + ASX scanners in unified pipeline. Verify signals flow through scoring + convergence.
6. Migrate existing 24 non-US candidates into `unified_system/candidates/`.

**Done gate**: All 4 existing non-US scanners operational. SEDAR+ unblocked. Candidates migrated.

### Phase 3: Build Reporting Layer (Sessions 4-5)
1. Build `tools/report_generator.py` using reportlab.
2. Implement daily digest PDF generation.
3. Implement candidate dossier PDF generation.
4. Implement weekly strategic report PDF generation.
5. Test PDF output quality — readable, properly formatted, all data present.
6. Write the `unified-reporting` task prompt.

**Done gate**: All 3 PDF types generate correctly from current data.

### Phase 4: Register Tasks + Burn-in (Session 5-6)
1. Disable all 6 active old scheduled tasks.
2. Register `unified-operational`, `unified-maintenance`, `unified-reporting`.
3. Run first operational cycle manually ("Run now"). Verify full pipeline.
4. Run first reporting cycle manually. Verify PDFs.
5. Let system run autonomously for 48 hours. Check outputs.

**Done gate**: System runs 48 hours autonomously. Daily digests produced. Candidate dossiers produced. No lock collisions.

### Phase 5: Build New Non-US Scanners (Sessions 7-12, phased)
Build in this order (prioritized by market liquidity + data accessibility):
1. HKEx (Hong Kong) — use `strategy_hk_hkex.md` from Tool 2
2. KIND (Korea) — use `strategy_kr_kind.md` from Tool 2
3. BSE/NSE (India) — use `strategy_in_bse_nse.md` from Tool 2
4. CVM (Brazil) — use `strategy_br_cvm.md` from Tool 2
5. BMV (Mexico) — use `strategy_mx_bmv.md` from Tool 2

Each scanner follows the same pattern: read strategy spec → code scanner → test against live endpoint → add to scanner_registry.json → verify in pipeline.

**Done gate per scanner**: Scanner compiles, endpoint reachable, produces ≥1 signal from live data, signal flows through scoring pipeline correctly.

### Phase 6: Build Litigation Scanners (Sessions 12-15)
1. Validate `party_resolver.py` against live EDGAR (was never tested).
2. Run `build_exhibit21_map.py` to populate `baselines/exhibit21_subsidiary_table.json`.
3. Build `courtlistener_scanner.py` using CourtListener RECAP API v4.
4. Build `sec_enforcement_scanner.py` using sec.gov litigation releases + EDGAR.
5. Test party resolution → entity resolution → scoring pipeline end-to-end.
6. Add both scanners to `scanner_registry.json`.

**Done gate**: Both litigation scanners produce signals that flow through the full pipeline with correct entity resolution and litigation-profile scoring.

### Phase 7: Enhance ESMA Historical Tracking (Session 15-16)
1. Implement `esma_snapshots/` persistence (longer than 1 day).
2. Add "newly crowded" detection: today 6 holders, yesterday 2 holders = real signal vs. steady-state.
3. Add multi-regulator boost per B3 from synergy analysis.

**Done gate**: ESMA scanner produces differentiated scores based on historical position changes, not just point-in-time snapshots.

---

## PART 9 — OPERATIONAL DISCIPLINE (from Tool 1, applies to ALL)

These rules are non-negotiable. They are the backbone that kept Tool 1 running for 67+ sessions.

### Prime Directive
Every claim in every deliverable must be labeled:
- **VERIFIED** — directly traceable to source code, data, or primary document
- **INFERRED** — reasonable conclusion from combining verified facts
- **SPECULATED** — forward-looking or hypothetical

### Data Discipline
- Every signal must be traceable to a source URL
- Market cap floor: $300M USD (unified across all geographies)
- Entity resolution via OpenFIGI is mandatory before scoring
- Translation confidence < 0.70 = signal dropped (non-English sources)
- Party resolution confidence < 0.85 = signal capped at Archive (litigation)

### Session Discipline
- Cold-start protocol: SESSION_STATE → INSTRUCTIONS → task-specific file
- Work until usage limit — NO EXCEPTIONS
- Save after every discrete unit of work
- Never delete files — archive with dated suffix
- One concept per file
- Shutdown protocol: flush state → SESSION_STATE → PROGRESS_LOG → INDEX → release lock

### Quality Over Quantity
- Target: 2-5 high-conviction candidates per week across all scanners
- Every candidate must survive the full pipeline: triage → score → deep dive → web research → kill conditions
- The web research layer is MANDATORY for all candidates
- No candidate leaves the system without explicit kill conditions

---

## PART 10 — WHAT SUCCESS LOOKS LIKE

After full implementation:
- One system folder with ~16 scanners feeding a unified pipeline
- 3 scheduled tasks (down from 10) running smoothly with no lock collisions
- Daily digest PDFs produced every 4 hours
- Candidate dossier PDFs auto-generated for new/updated candidates
- Weekly strategic report showing scanner performance and system health
- Cross-scanner convergence detection working across all signal types
- 5 scoring profiles producing accurate, profile-appropriate scores
- Clean separation: operational tasks write operational data, reporting task reads it and writes PDFs

---

## APPENDIX A — EXISTING FILES TO PRESERVE / REFERENCE

### Candidates to migrate (current active + watchlist)
**From Tool 1**: AXSM, RPAY, RGR, VERA, AVNS, GSAT (active) + SEM (watchlist) + VRDN (watchlist)
**From Tool 2**: ITRK_XLON, 1878_XTKS, PDI_XASX, WBC_XASX, PTSB_XLON, 9601_XTKS, 2972_XTKS, 6058_XTKS, CCL_XASX, and ~15 more XTKS candidates

### Strategy specs to carry forward
**From Tool 1**: `strategies/edgar_keyword_scanning.md`, `strategies/esma_short_aggregation.md`, `strategies/fda_pdufa_calendar.md`, `strategies/congressional_trading.md`
**From Tool 2**: All 9 strategy files in `strategies/`
**From Tool 3**: `strategies/strategy_federal_civil.md`, `strategies/strategy_sec_enforcement.md` (build first); remaining 4 for later phases

### Decisions to reference
Tool 1 DECISIONS.md has 52+ decisions. Key ones to carry forward into unified DECISIONS.md as references:
- D-014 (subprocess isolation pattern)
- D-018 (EDGAR wall-clock budget 35s)
- D-047 (operational + maintenance task split)
- D-052 (atomic writes for truncation bug — use this pattern for ALL file writes)

### Open questions to migrate
- Q-002 (CNMV Spain access — Pedro's home market)
- Q-016 (terminal-marker validation — implement in maintenance task)

---

## APPENDIX B — DEPENDENCIES

```bash
pip install requests beautifulsoup4 lxml yfinance openpyxl pandas python-dateutil feedparser pypdf rapidfuzz reportlab --break-system-packages
```

Run at the start of EVERY session (sandbox resets between sessions).

---

*End of implementation plan. This document is the complete specification. A fresh session should be able to execute from Phase 0 through Phase 7 using only this plan + the existing files referenced above.*
