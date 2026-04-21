# CONAN — Complete Scoring Methodology Reference

**Purpose.** This document exhaustively describes how the Conan unified investment-research system scores, enriches, and promotes signals into candidates. It is written to be complete enough that Claude Code (or any engineer) can replicate the structure end-to-end without needing to read the source tree first.

**Scope.** Every scoring profile (6), every scanner (17), every enricher (3), the convergence bonus math, the candidate gate, the kill-watch / monitor logic, the signal-log validator, the health-check families, and every numeric threshold and keyword list that drives a classification decision.

**Source of truth.** All numbers and keyword lists below were extracted directly from `C:\Users\javie\OneDrive\Desktop\Claude Cowork\Conan\engine\` on 2026-04-21. Where a behaviour is driven by a specific file and line, the file name is cited.

---

## 0. Executive Summary — How Conan Scores

Conan is a multi-source investment research pipeline that ingests regulatory disclosures, court dockets, short-position registries, and clinical-trial data from 17 sources; scores each signal against one of 6 scoring profiles on a 0–50 rubric; enriches a subset (litigation, biotech, merger-arb) with orthogonal risk/financial metrics; optionally applies a convergence bonus when independent scanners agree on the same issuer; and promotes signals that clear a written-thesis quality gate into a candidate dossier.

Every scoring decision funnels through three layers:

1. **Triage gates** — universal pre-scoring filters (market cap ≥ $215M USD, public exchange, novelty, translation confidence).
2. **Profile rubric** — 5–7 weighted dimensions, each on a 1–5 scale, producing a 0–50 total. Band thresholds: Immediate ≥ 35 (draft threshold is 30 post-D-034), Watchlist 25–34, Archive 15–24, Discard < 15. Profile-specific auto-caps can force a ceiling.
3. **Post-scoring adjustments** — convergence bonus (+5 for 2 independent signals, +10 for 3+), enrichment tiers (non-numeric), kill-watch deductions.

The promotion rule is strict: **no signal becomes a candidate without a written thesis** that names situation, why-underpriced, next catalyst, catalyst date, and kill conditions (enforced by `candidate_gate.promote_candidate`).

---

## 1. System Architecture — Data Flow

```
 Scanners (17) → signal records (JSON) → signal_log.json
        │
        ▼
 Post-scan hook chain (6 hooks in tools/run_post_scan.py)
        │  1. catalyst_calendar.run()
        │  2. candidate_monitor.run()
        │  3. legal_enricher.enrich_signal_log()
        │  4. validate_signal_log.run()
        │  5. biotech_enricher.enrich_signal_log()
        │  6. health_check.run()
        ▼
 Convergence engine (tools/convergence_engine.py) — groups by issuer,
   applies +5/+10 bonus, rewrites signal_log with scoring.score_with_bonus
        │
        ▼
 Thesis draft / analyst (thesis_draft.py + thesis_analyst.py)
   — for signals ≥ SCORE_FLOOR (30) with rich record + ticker + mcap OK
   — merger_arb / activist calls deal_context_enricher inline
        │
        ▼
 candidate_gate.promote_candidate — rejects if thesis fails quality gate
        │
        ▼
 report_generator — executive summary + detail book + per-ticker dossiers
   + Legal Desk + Biotech Desk sections
        │
        ▼
 build_dashboard.py → Conan/DASHBOARD.html (live)
```

Universal properties of the pipeline:

- **Market-cap floor** — $215M USD everywhere (`MCAP_FLOOR_USD = 215_000_000` in `thesis_draft.py`). Applied at triage and re-validated at thesis-draft time.
- **Signal-log retention** — 14 days rolling, except litigation signals kept for 90 days.
- **Convergence window** — 14 days normally, 30 days when any member of the group has `scoring_profile == "litigation"`.
- **Dedup** — cross-listed echoes are collapsed via `source_content_hash`; logical duplicates (same issuer + source_date + scanner + signal_type + hash) are flagged by the validator.
- **Idempotency** — every enricher can be re-run; it stamps a non-invasive `<kind>_enrichment` patch with an `enriched_at` field.

---

## 2. Shared Primitives

### 2.1 Universal Triage Gate (applied before any profile rubric runs)

| Gate | Rule |
|---|---|
| Market cap | ≥ $215M USD (≈ €200M) at signal date |
| Listing | Public exchange — NYSE, NASDAQ, LSE, Euronext, XETRA, TSE, ASX, TSX, HKEX, KRX, BSE/NSE, B3, BMV |
| Novelty | First occurrence in N-day dedup window (N varies per scanner; 7–45 days) OR material escalation (stage change, threshold crossing) |
| Freshness | Source date within scan window (scanner-specific — typically 3–14 days) |
| Translation | For non-English sources, confidence ≥ 0.70; direction-disambiguating matches score 0.92 |

Profile-specific gates add more constraints (see §3).

### 2.2 Score Bands (system-wide)

| Band | Raw score (0–50) | Action |
|---|---|---|
| Immediate | ≥ 35 | Full dossier within 24h; daily monitoring |
| Watchlist | 25–34 | Track, re-score on update |
| Archive | 15–24 | Log only |
| Discard | < 15 | Drop with reason |

Two wrinkles:

- **D-034 (2026-04-21)** — `SCORE_FLOOR` for thesis drafting was lowered from 35 → 30 to surface more names for review. The convergence engine's `band_with_bonus` thresholds were shifted in lockstep: `immediate` ≥ 30, `watchlist` ≥ 20, `archive` ≥ 10, `discard` < 10. The *raw* band thresholds in the profile rubrics (§3) still read 35 / 25 / 15 because they represent the pre-bonus rubric.
- **Auto-caps** — several profiles have auto-caps that force a ceiling (e.g., merger_arb caps at watchlist if annualized return < risk-free + 3%).

### 2.3 Signal Record Schema (minimum fields, produced by every scanner)

```
signal_id                 str (UUID or scanner-local id)
source_url                str (primary source)
source_content_hash       str (SHA256 of body — for dedup)
source_date               ISO-8601 UTC
scan_date                 ISO-8601 UTC
scanner / upstream_scanner str ("edgar_filing_monitor", etc.)
signal_type               str (scanner-specific, e.g. "merger_announced")
scoring_profile           str (one of 6)
thesis_direction          "long" | "short" | "neutral" | "unknown"
translation_confidence    float 0..1 (non-English only)
issuer_figi               str (when resolved via OpenFIGI)
ticker / ticker_local     str
mic                       str (4-letter, e.g. XNYS, XLON, XTKS)
cik                       str (US only)
company_name_en           str
market_cap_usd            number (when available)
raw_data                  dict (scanner-native payload)
scoring                   dict { dimensions:{...}, score:float, band:str,
                                 convergence_bonus:int, score_with_bonus:float }
```

### 2.4 Signal-type → Profile Routing

Central map lives in `tools/profile_map.py::profile_for(signal_type, scanner)`. Scanners may tag their own `scoring_profile` explicitly; when they don't, `profile_map` falls back to signal-type membership in these sets:

- `merger_arb` — takeover_bid_circular, plan_of_arrangement, acquisition_proposal, merger_agreement, directors_circular, tender_offer, mbo_announcement, scheme_of_arrangement, going_private, offer_document, possible_offer, firm_offer.
- `binary_catalyst` — trial_readout, phase1/2/3_readout, pdufa, bla_submission, bla_approval, marketing_authorization, ema_chmp_positive_opinion, ema_chmp_negative_opinion, clinical_hold, cmc_rtf, advisory_committee, pre_phase3_readout.
- `litigation` — securities_class_action, securities_litigation, litigation_regulatory, regulatory_action, sec_enforcement, doj_enforcement, ftc_action, cease_trade_order, mcto_management_cease_trade, investigation_announcement, wells_notice, subpoena, antitrust_complaint, consumer_class_action.
- `short_positioning` — short_crowded, heavy_short, short_report_published, borrow_tight, ftd_spike, profit_warning, guidance_downgrade, earnings_miss, impairment_loss, write_down, financial_restatement, restatement, internal_control_weakness, going_concern_warning, covenant_breach, administration_or_receivership, ccaa_filing.
- `activist_governance` — activist_proxy, 13d_filing, early_warning_10pct, proxy_circular, shareholder_meeting, equity_fundraise, bought_deal, private_placement, equity_financing, buyback_initiation, share_buyback, dividend_increase, dividend_cut, special_dividend, guidance_revision, guidance_upgrade, forecast_variance, profit_upgrade, tanshin_results, material_change_report, capital_reorganization, spin_off, rights_issue, ni43101_technical_report, ni51101_reserves.
- `takeover_candidate` — takeover_candidate.

Scanner-level fallbacks (when `signal_type` absent): asx/sedar_plus/hkex/kind/bse_nse/cvm/bmv/lse_rns/tdnet/edgar/congressional → `activist_governance`; esma_short → `short_positioning`; fda_pdufa / pre_phase3_readout → `binary_catalyst`; courtlistener / sec_enforcement → `litigation`; takeover_candidate_scanner → `takeover_candidate`.

---

## 3. The Six Scoring Profiles

Each profile is a weighted-dimension rubric on a 0–50 max. Dimensions use a 1–5 raw scale and a per-dimension weight; score = Σ(raw × weight). Rubric files: `engine/framework/profile_<name>.md`.

### 3.1 Profile — `merger_arb` (announced deals)

**Applies to.** EDGAR M&A filings (DEFM14A, PREM14A, SC 13E3, S-4), TDnet tender offers, LSE Rule 2.7 firm offers, ASX schemes, SEDAR+ plans of arrangement, HKEx takeovers, any deal with a fixed or exchange-ratio price.

**Philosophy.** Spread × certainty × time. Edge is quantifiable; risk is binary.

**Dimensions (max = 50):**

| # | Dimension | Weight | 5 | 4 | 3 | 2 | 1 |
|---|---|---|---|---|---|---|---|
| 1 | Spread Size | ×3 | > 10% | 5–10% | 3–5% | 1–3% | < 1% |
| 2 | Deal Certainty | ×2.5 | Unconditional / cash / routine antitrust | Minor conditions | Moderate antitrust + financing | Hostile / competing bidder | CFIUS / MAC dispute / holdout |
| 3 | Annualized Return | ×2 | > 20% | 12–20% | 8–12% | 4–8% | < 4% |
| 4 | Break Risk | ×1.5 | < 10% | 10–20% | 20–30% | 30–40% | > 40% |
| 5 | Liquidity (ADV USD) | ×1 | > $50M | $20–50M | $10–20M | $3–10M | < $3M |

**Auto-caps:**
- *Sub-scale return.* If annualized return < (risk-free rate + 3%) cap at Watchlist. (Current RF ≈ 4.3%, so trigger below ~7.3%.)
- *Break-risk dominance.* If Break Risk = 1 **and** Deal Certainty ≤ 2, cap at Watchlist.

**Formulas:**
- `spread = (deal_price − current_price) / current_price` — for stock deals use `buyer_price × exchange_ratio`.
- `annualized = spread × (365 / estimated_days_to_close)`.
- Unaffected price = 30-trading-day VWAP prior to first leak.

**Judgment notes:**
- Cross-border: add CFIUS / FDI / antitrust-by-jurisdiction to Deal Certainty.
- Controller take-privates: +0.5 to Certainty if controller historically bumps price; −0.5 if historically cuts.

### 3.2 Profile — `activist_governance`

**Applies to.** SC 13D, contested proxies (PREN14A / DEFC14A), poison pills, board disputes, cooperation agreements, Rule 2.4 "possible offer" (LSE), Article 324 (TDnet), SEDAR+ early warning.

**Additional gate.** Filer must be identifiable. "Unknown fund" → drop.

**Dimensions (max = 50):**

| # | Dimension | Weight |
|---|---|---|
| 1 | Signal Strength | ×2 |
| 2 | Information Asymmetry | ×2 |
| 3 | Activist Track Record | ×1.5 |
| 4 | Risk/Reward | ×1.5 |
| 5 | Catalyst Clarity | ×1 |
| 6 | Edge Decay | ×1 |
| 7 | Liquidity | ×1 |

**Scales (1–5, abridged):**
- *Signal Strength.* 5 = direct mechanistic (13D with stated board-replacement intent; poison pill adopted in response to known bidder). 1 = speculative (passive-to-active by non-campaigner).
- *Information Asymmetry.* 5 = just filed, no news coverage yet. 1 = stale, campaign public for months.
- *Activist Track Record.* 5 = Tier-1 (Elliott, Icahn, Starboard, ValueAct, Trian). 4 = Jana, Engaged Capital, Blue Harbour. 1 = unknown first-time filer.
- *Risk/Reward.* 5 = ≥ 3:1 asymmetry. 1 = downside > upside.
- *Catalyst Clarity.* 5 = specific date (annual meeting, poison-pill expiry). 1 = no catalyst.
- *Edge Decay.* 5 = weeks+. 1 = hours or less.
- *Liquidity.* Same bands as merger_arb.

**Judgment notes:**
- Multi-holder simultaneous filings (two 13Ds same week) → Signal Strength +1, and flag for convergence with `short_positioning`.
- Cooperation agreements: board seats > strategic review > standstill alone.

### 3.3 Profile — `binary_catalyst` (FDA / clinical / regulatory)

**Applies to.** PDUFA dates, AdCom votes, Phase 3 pivotal readouts, PMA/510(k), EMA CHMP, MHRA, non-pharma binary regulatory outcomes with defined date.

**Additional gate.** Decision date ≤ 60 days OR narrow window. Drug/device must have published trial data or prior FDA interaction.

**Dimensions (max = 50):**

| # | Dimension | Weight | 5 | 4 | 3 | 2 | 1 |
|---|---|---|---|---|---|---|---|
| 1 | Approval Probability | ×2.5 | > 80% | 60–80% | 40–60% | 20–40% | < 20% |
| 2 | Market Mispricing | ×2.5 | > ±20pp | ±10–20pp | ±5–10pp | ±2–5pp | < ±2pp |
| 3 | Magnitude of Move | ×1.5 | > 50% | 30–50% | 15–30% | 5–15% | < 5% |
| 4 | Competitive Landscape | ×1.5 | first-in-class | best-in-class, 1 prior | 2–3 approved | crowded me-too | heavy price pressure |
| 5 | Catalyst Timeline | ×1 | ≤ 14d | 15–30d | 31–60d | 61–90d | > 90d |
| 6 | Liquidity | ×1 | same as §3.1 |

**Auto-cap (Expected Value):** `EV = P_approve × upside% − P_reject × downside%`. If `EV < 5%` cap at Watchlist.

**Inputs to Approval Probability:** primary-endpoint hit/miss, safety profile, AdCom vote, precedent label in class, RTOR designation, CMC issues. AdCom heuristic: 6-6 tie ≈ 40% approval; 10-2 positive ≈ 90%.

**Red flag:** insider sales near decision date without 10b5-1 affirmation → subtract 1 from Approval Probability. Verify affirmation via the `<aff10b5One>` XML tag on Form 4.

### 3.4 Profile — `short_positioning`

**Applies to.** ESMA short disclosures (FCA, AMF, AFM, BaFin, CNMV, CONSOB); SEC Form 4 insider clusters (when scanner built); institutional short interest; crowded-short registrations.

**Dimensions (max = 50):**

| # | Dimension | Weight |
|---|---|---|
| 1 | Crowding Intensity | ×2.5 |
| 2 | Trend Direction | ×2 |
| 3 | Catalyst Proximity | ×2 |
| 4 | Position Size vs. Float | ×1.5 |
| 5 | Historical Analog | ×1 |
| 6 | Liquidity | ×1 |

**Crowding Intensity (ESMA):** 5 = ≥ 6 unique holders OR ≥ 4 in same 30-day window; 4 = 4–5; 3 = 3; 2 = 2; 1 = 1. Affiliated funds (e.g., Citadel variants) count as ONE holder.

**Crowding Intensity (Form 4 clusters):** 5 = 3+ C-suite sellers in 30d; 4 = 2 C-suite + 1 VP; 3 = VP/director cluster; 2 = 1–2 minor insiders; 1 = single minor insider.

**Trend Direction:** 5 = rapid buildup (≥3 new in 7d, aggregate +50% in 30d); 1 = rapid unwind (≥3 closed in 7d, aggregate −50% in 30d). Requires daily snapshots in `esma_snapshots/`.

**Catalyst Proximity:** 5 = ≤ 14d, 4 = 15–30d, 3 = 31–90d, 2 = > 90d, 1 = none.

**Position vs. Float:** 5 = > 10%; 4 = 5–10%; 3 = 2–5%; 2 = 1–2%; 1 = < 1%.

**Multi-regulator boost:** Same name disclosed across 2+ regulators → Crowding Intensity +1. Aggregate via `issuer_figi`.

**Inversion rule.** Declining aggregate short into a positive catalyst → flip to LONG thesis (shorts covering).

### 3.5 Profile — `litigation`

**Applies to.** CourtListener federal dockets (securities=850, antitrust=410, patent=830/835, contract M&A=190), SEC enforcement (litigation releases + administrative proceedings), DOJ/FTC, ITC 337, Delaware Chancery, PTAB IPR.

**Additional gate.** Party resolution confidence ≥ 0.85 (exact CIK or fuzzy with corroborating fields). If < 0.85, signal dropped (prevents signal-log contamination).

**Dimensions (max = 50):**

| # | Dimension | Weight | 5 | 4 | 3 | 2 | 1 |
|---|---|---|---|---|---|---|---|
| 1 | Financial Materiality (damages / EV) | ×3 | > 20% | 10–20% | 5–10% | 2–5% | < 2% |
| 2 | Legal Outcome Probability | ×2 | Near-certain adverse | Strong adverse indicator | Uncertain | Weak case | Speculative |
| 3 | Market Pricing (how much already moved) | ×2 | No move | < 5% | 5–15% | 15–30% | > 30% |
| 4 | Resolution Timeline | ×1.5 | ≤ 1mo | 1–3mo | 3–6mo | 6–12mo | > 12mo |
| 5 | Liquidity | ×1 | same scale |
| 6 | Party Resolution Confidence | ×0.5 | exact CIK + corroborated | exact name + state | fuzzy ≥ 0.92 | fuzzy 0.85–0.92 | < 0.85 (DROP) |

**Auto-cap — Party confidence.** If Party Resolution Confidence < 3 (fuzzy < 0.92), cap at Archive.

**Enrichment layer (D-028).** Every litigation signal gets two extra tiers via `legal_enricher.py` — see §5.1.

**Judgment notes:**
- Subsidiary liability: check `baselines/exhibit21_subsidiary_table.json`; materiality scored against parent EV.
- Securities class actions: the filing itself is NOT the signal — real signals are (a) motion-to-dismiss denied + damaging discovery, or (b) lead-plaintiff appointment with Tier-1 firm (Robbins Geller, Labaton, Bernstein).

### 3.6 Profile — `takeover_candidate` (pre-edge)

**Applies to.** Signals from `takeover_candidate_scanner` identifying small/mid-cap public companies exhibiting the setup of a likely M&A target 3–12 months *before* any deal is announced.

**Philosophy.** Pre-edge pattern recognition. Candidate is a *hypothesis*, not a confirmed deal.

**Additional gates:**
- No definitive merger agreement currently in effect (post-edge disqualifier, D-013).
- Company has not rejected a prior bid within trailing 6 months (else cap at Archive).
- ≥ 2 of 5 setup patterns hit (see below).

**Dimensions (max = 50):**

| # | Dimension | Weight |
|---|---|---|
| 1 | Setup Strength | ×3 |
| 2 | Edge Freshness | ×2 |
| 3 | Valuation Cushion | ×2 |
| 4 | Strategic Buyer Clarity | ×2 |
| 5 | Liquidity | ×1 |

**Setup Strength scale** (counts of the 5 setup patterns hit):
- 5 = 4–5 patterns including explicit "strategic alternatives" / "financial advisor engaged".
- 4 = 3 patterns including strategic review OR banker mandate named.
- 3 = 3 patterns, no strategic-review language.
- 2 = 2 patterns.
- 1 = 1 pattern but unusually strong (e.g., activist 13D with M&A-demand language).

**The 5 setup patterns:** (1) PE take-private setup, (2) streamlined-for-sale pattern, (3) strategic-review disclosure, (4) insider + institutional accumulation, (5) strategic buyer fit.

**Edge Freshness:** 5 = key signal within last 30d; 4 = 30–90d; 3 = 3–6mo; 2 = 6–12mo; 1 = > 12mo stale.

**Valuation Cushion** (discount to 5-yr median EV/EBITDA or EV/Revenue): 5 = > 35%; 4 = 20–35%; 3 = 5–20%; 2 = near median; 1 = above median.

**Strategic Buyer Clarity:** 5 = named strategic with prior M&A in sub-sector. 4 = named PE with sector history. 3 = generic PE take-private. 2 = unclear. 1 = no credible buyer.

**Liquidity (ADV USD):** 5 = > $50M; 4 = $15–50M; 3 = $5–15M; 2 = $1–5M; 1 = < $1M.

**Auto-caps:**
- Definitive merger agreement already announced → disqualify.
- Company rejected prior offer in trailing 6mo → cap Archive.
- Going-concern warning in last 10-Q → cap Watchlist.
- Target in active-consolidation sector but already acquired a peer → cap Watchlist.

---

## 4. Scanner-Level Scoring (the 17 scanners)

Every scanner is a Python script in `engine/tools/` that (a) queries its source, (b) classifies each announcement into a `signal_type`, (c) assigns a raw `signal_strength` 1–5, (d) maps to a `scoring_profile`, (e) seeds a 7-dim rubric (for some scanners) or emits minimal metadata for the finalizer to score, and (f) writes to the signal log.

Cadence and endpoint data live in `engine/config/scanner_registry.json`.

### 4.1 edgar_filing_monitor.py (US — `3h` cadence)

**Keyword → signal_type (rotation order: activist → mna → distress → governance):**

*Activist:* "strategic alternatives", "board representation", "maximize shareholder value", "undervalued", "change in control", "special committee", "proxy contest", "consent solicitation" → `activist_keyword`. SC 13D / 13D/A → `activist_ownership` (strength=4).

*Distress:* "going concern", "covenant breach", "waiver", "forbearance agreement", "material weakness", "restatement", "liquidity shortfall", "substantial doubt", "debtor-in-possession" → `distress_keyword`. NT 10-K/Q variants → `late_filings` (strength=3).

*M&A:* "merger agreement", "tender offer", "fairness opinion", "change of control", "break-up fee", "definitive agreement", "received indication of interest" → `mna_keyword`.

*Governance:* "poison pill", "rights plan", "bylaw amendment", "declassify board", "auditor resignation", "whistleblower", "internal investigation" → `governance_keyword`.

**Form whitelists (per category):** distress → {10-K, 10-Q, 8-K, their /A forms}; activist → {8-K, 13D, 14D9, PRER14A, DFAN14A}; mna → {8-K, 13D, SC TO-T, SC 13E3, PREM14A}; governance → {8-K, 10-K, 10-Q}.

**SPAC/IPO blacklist (drop):** S-1, S-4, F-1, F-4, DRS, SB-2, 425, SC TO-C, 424B3-5 (all /A variants).

**Skipped noise:** ARS, DEF 14A (routine proxy), DEFA14A, DEFM14A (routed to merger_arb via signal_type_map), PRE 14A, N-CSR, 497, NPORT-P.

**Strength formula.** Base = 2; form bumps to 4–5 (13D=4, new SC TO-T=4, merger agreement=5); keyword-specificity bumps (going concern / substantial doubt = 4). Wall-clock budget 35s; dedup window 45 days; mcap floor $215M.

**Endpoints:** `https://efts.sec.gov/LATEST/search-index` (primary), `https://data.sec.gov/submissions/` (secondary).

### 4.2 esma_short_scanner.py (EU — `daily`)

**Country → MIC suffix:** GB (.L), FR (.PA), NL (.AS), DE (.DE), ES (.MC), IT (.MI), CH (.SW), BE (.BR), AT (.VI), IE (.IR), PT (.LS), SE (.ST), NO (.OL), DK (.CO), FI (.HE).

**Signal types (from daily snapshot diff vs. prior):**
- `short_new_position` — holder not in prior snapshot ≥ 0.5% → strength 3.
- `short_position_increase` — Δ ≥ 0.2pp (strength 3); ≥ 0.5pp (strength 4).
- `short_position_decrease` — Δ ≤ −0.2pp (strength 2).
- `short_crowded_short` — ≥ 3 holders on same ISIN (strength 3); ≥ 5 (strength 4).
- `short_large_position` — ≥ 2.0% (strength 4).
- Combined: "new_position+crowded_short" (sorted by max strength).

**Triage:** mcap ≥ $215M; dedup 7d; FIGI rate-limit 25 calls/run with caching.

**Primary endpoint:** `https://www.fca.org.uk/publication/data/short-positions-daily-update.xlsx`.

### 4.3 fda_pdufa_pipeline.py (US — `3h`)

**Watchlist schema:** ticker, company_name, drug_name, indication, pdufa_date, nda_type (NDA | BLA), application_number, phase3_nctid, adcom_date, adcom_vote, is_resubmission, crl_date, status (active | approved | rejected | withdrawn).

**Signal-type classification (days_until PDUFA):**
- ≤ 7d → `pdufa_imminent` (strength base+1, cap 5)
- ≤ 30d → `pdufa_approaching`
- ≤ 90d → `pdufa_watchlist`

**Strength scoring (additive, base = 2):** +1 Phase 3 data available; +1 trial complete/results; +1 resubmission; +1 adcom vote favorable (yes/no > 2.0); +1 imminent.

**AdCom parsing:** "12-1" / "10-2" format; yes/no ratio > 2.0 → +1.

**Explicit disqualifications (D-039):** ZLAB (Augtyro approved Jun-24), CORT (Lifyorli approved Mar-25-26), ORCA (private).

**Auto-discovery:** 90-day lookback of EDGAR 8-K for "PDUFA" + "action date".

**Early-approval cross-check (D-046):** flags approvals within 180d pre-PDUFA.

**Endpoints:** `https://api.fda.gov/drug/label.json` (primary); EDGAR EFTS fallback.

### 4.4 congressional_trading.py (US — `daily`)

**Committee → sector map** (static, ~20 committees): Armed Services → defense/aerospace/military; HELP → pharma/biotech/healthcare/medical/education; Banking → banking/financial/fintech/insurance; Commerce → tech/telecom/communications/internet/ai; Energy → oil/gas/energy/utilities/renewable/solar/nuclear; etc.

**Sector → ticker map** (~70 entries): defense → [LMT, RTX, GD, NOC, BA, LHX, HII, LDOS, SAIC, BAH, CACI, KTOS, PLTR, BWXT]; pharma → [PFE, JNJ, MRK, ABBV, LLY, BMY, AZN, NVS, GSK, AMGN, GILD, REGN, VRTX, BIIB]; …

**Size ranges:** "1K–15K" → (1000, 15000); "15K–50K"; "50K–100K"; "100K–250K"; "250K–500K"; "500K–1M"; "1M–5M"; "5M–25M"; "25M–50M"; "50M+".

**Strength heuristic:** base = 2; `committee_aligned` → max 4; `unusual_size` (≥ $25K) → max 3; `timing_cluster` (≥ 3 members same week) → max 4; `options_activity` → max 4.

**Q-014 de-noise:** spouse/child small-dollar trades in Commerce-committee mega-caps forced to strength 2.

**Filter:** `filter_excluded_filers = ["Ro Khanna"]` (passive investor / noise).

**Triage:** min_amount = $5K; mcap floor $215M; dedup 14d; Stock Act reporting window 45d back.

**Endpoint:** `https://www.capitoltrades.com/trades`.

### 4.5 lse_rns_scanner.py (UK — `3h`)

**Headline regex (first match wins):**

| Pattern | signal_type | strength | direction |
|---|---|---|---|
| Rule 2.7 / recommended offer / firm intention | `takeover_firm_offer` | 5 | long |
| Rule 2.4 / possible offer | `takeover_possible_offer` | 4 | long |
| Scheme of arrangement (sanction / court / effective) | `scheme_sanction` | 5 | long |
| Strategic review | `strategic_review` | 3 | unknown |
| Profit warning / materially below / trading alert | `profit_warning` | 5 | short |
| Profit upgrade / ahead of expectations | `profit_upgrade` | 4 | long |
| Trading update / pre-close update | `trading_update` | 3 | unknown |
| Final / preliminary / annual results | `final_results` | 3 | — |
| Interim / half-year / Q1-4 results | `interim_results` | 3 | — |
| CEO resignation / appointment | `senior_governance_change` | 4 | — |
| TR-1 / major holding notification | `major_shareholder_change` | 3 | — |
| JORC / mineral resource / resource upgrade | `jorc_resource_update` | 4 | — |
| Trading suspension / cancellation | `aim_suspension` | 4 | short |
| Placing / retail offer / rights issue | `equity_fundraise` | 3 | short |
| Share buyback commencement | `buyback_initiation` | 3 | long |

**7-dim seed (LSE baseline: clarity=3, asymm=2 (widely watched), risk_reward=3, edge=3, liquidity=4, timeline=3):**
- takeover_firm_offer / scheme_sanction → clarity=5, timeline=5.
- profit_warning → clarity=4, timeline=5, edge=5.
- Price-sensitive announcement → strength +1.
- Small-cap (< $1B) → asymmetry +1.

**FX:** 1 GBP ≈ 1.27 USD → £170M floor equivalent to $215M.

**Cache.** Maintenance task pre-warms `lse_alldata_cache/` so window=3 and window=7 queries run in cold sandbox.

### 4.6 tdnet_scanner.py (JP — `3h`)

**Japanese title patterns (regex list, ordered):**

| Pattern | signal_type | strength | direction | confidence |
|---|---|---|---|---|
| 業績予想.*(下方修正\|下振れ) | `profit_warning` | 5 | short | 0.92 |
| 業績予想.*(上方修正\|上振れ) | `profit_upgrade` | 4 | long | 0.92 |
| 業績予想.*(修正\|見直し\|変更) | `guidance_revision` | 4 | unknown | 0.70 |
| 予想と実績.*差異 | `forecast_variance` | 4 | unknown | 0.70 |
| 配当予想.*(増配\|上方修正) | `dividend_increase` | 3 | long | 0.90 |
| 配当予想.*(減配\|無配\|下方修正) | `dividend_cut` | 4 | short | 0.90 |
| 公開買付 | `tender_offer` | 5 | long | 0.92 |
| ＭＢＯ \| マネジメント・バイアウト | `mbo_announcement` | 5 | long | 0.92 |
| 株式交換契約 \| 合併契約 \| 経営統合 | `merger_agreement` | 5 | unknown | 0.75 |
| 特別損失.*計上 \| 減損損失 | `impairment_loss` | 4 | short | 0.88 |
| 決算訂正 \| 過年度.*訂正 | `restatement` | 5 | short | 0.90 |
| 内部統制.*開示すべき重要な不備 | `internal_control_weakness` | 5 | short | 0.90 |
| 自己株式.*取得.*(決議\|取締役会) | `buyback_initiation` | 3 | long | 0.85 |
| 新株発行 \| 公募増資 \| 第三者割当 | `equity_fundraise` | 3 | short | 0.85 |
| 決算短信 | `tanshin_results` | 3 | unknown | 0.70 |
| 訴訟 \| 課徴金 \| 行政処分 | `litigation_regulatory` | 4 | short | 0.85 |

**7-dim seed (baseline: strength=X, clarity=3, asymm=4 (slower English re-dissem), risk_reward=3, edge=3, liquidity=3, timeline=3):**
- tender_offer / mbo → clarity=5, timeline=5.
- profit_warning → clarity=4, timeline=5, edge=5.
- impairment_loss → clarity=4, timeline=4.
- restatement → clarity=5, timeline=3.

**Listing filter:** 東 (Tokyo) only; skip Nagoya/Fukuoka single-lists.

**FIGI quirk (critical):** `openfigi_resolver.normalize_ticker` — if `len(ticker)==5 and ticker[3].isalpha() and ticker[4]=="0"` and MIC is JP → drop the trailing 0 (e.g., 469A0 → 469A).

### 4.7 asx_scanner.py (AU — `3h`)

**Headline rules (1–5 strength):**

- Takeover offer / scheme / acquisition proposal / merger → 4–5, long.
- Profit/earnings upgrade (above/beat/exceed) → 4, long.
- Profit/earnings downgrade/warning / below expectations → 4, short.
- Revised guidance/outlook → 3.
- Items impacting / impairment / write-down → 4, short.
- Financial restatement → 5, short.
- Preliminary final (App 4E) → 3; half-year (App 4D) → 3.
- Placement / institutional placement → 3, short.
- Entitlement / rights → 3, short; SPP → 2; general capital raise → 3, short.
- Buyback → 3, long.
- Substantial holder Form 603 → 3, long; ceasing (605) → 3, short; change (604) → 2.
- Trading halt → 3; suspension → 4, short.
- JORC / drilling → 2; resource upgrade → 3, long.
- App 4C cashflow → 2.
- Special dividend → 3, long; dividend cut → 4, short.
- Going concern → 5, short; covenant breach → 5, short; administration → 5, short.

**Concurrency:** `ThreadPoolExecutor(max_workers=10)`; 200 tickers in ~11s (rewritten 2026-04-20). Wall-clock budget 95s.

**Rubric (`asx_rubric.py`) — baseline strength=X, clarity=3, asymm=2 (English), risk_reward=3, edge=3, liquidity=4, timeline=3:**
- Takeovers/mergers → clarity=5, timeline=5, edge=4.
- Guidance up/down → clarity=5, timeline=5, edge=5.
- Going concern / covenant → clarity=4, timeline=4, risk_reward=4.
- Capital raise → clarity=4, timeline=3.
- Small-cap (< $1B) → asymmetry +1.
- Price-sensitive → edge +1.

### 4.8 sedar_plus_scanner.py (CA — `daily`)

**Headline patterns (primarily via yfinance news feed + SEDAR+ HTML):**

- Takeover bid circular / plan of arrangement → 5, long.
- Acquisition proposal → 4, long.
- Merger / combining / definitive / directors circular → 4.
- Material change report → 4 or 3.
- Profit/earnings warning / guidance lowered → 5 or 4, short; guidance raised → 4, long.
- Early warning report / 10% ownership → 3, long.
- NI 43-101 technical report / maiden/increased/updated mineral resource → 3–4, long.
- NI 51-101 reserves report → 3.
- MCTO / cease trade order → 5, short.
- Impairment / write-down/off → 4, short.
- Restatement → 5, short.
- Bought deal / private placement / equity financing → 3, short.
- Buyback → 3, long; special dividend → 3, long; dividend cut/suspension → 4, short.
- Going concern → 5, short; covenant breach / CCAA / receivership → 5, short.
- Interim MD&A → 2.

**Rubric (`sedar_rubric.py`) — baseline clarity=3, asymm=2, risk_reward=3, edge=3, liquidity=4, timeline=3:**
- Takeovers / mergers → clarity=5, timeline=5, edge=4.
- Guidance → clarity=5, timeline=5, edge=5.
- Early warning → clarity=3, timeline=2, asymm=3.
- NI 43-101/51-101 → clarity=3, asymm=4, timeline=2.
- MCTO → clarity=5, timeline=5, risk_reward=4, edge=5.
- Distress → clarity=4, timeline=4, risk_reward=4.
- TSX-V board → asymm+1, liquidity-1.
- French language < 0.85 confidence → D-002 caps (strength→2, risk_reward→3).
- Small-cap → asymm+1.

**CA universe.** `working/ca_universe.json` seeded with ~25 tickers above $300M USD (50-sample probe on 2026-04-16; full refresh pending per Q-006).

### 4.9 hkex_scanner.py (HK — `daily`)

**High-signal patterns (HKEX title-search servlet):**
- Takeover/offer announcement / Rule 3.5 / mandatory/voluntary offer / privatisation/delisting → `tender_offer`, merger_arb, long.
- Scheme of arrangement → `scheme_of_arrangement`, merger_arb, long.
- Disclosure of interest / Part XV / substantial shareholder → `major_shareholder_change`, activist_governance.
- Profit warning/alert / loss alert → `profit_warning`, activist_governance, short.
- Trading suspension/halt / resumption → `trading_suspension`, activist_governance, short.
- Going concern / material uncertainty / qualified opinion / auditor resignation → `going_concern`, activist_governance, short.
- Connected transaction / very substantial disposal/acquisition → `material_transaction`, activist_governance.

**Boilerplate blacklist (drop):** annual/interim/ESG report, AGM notice, dividend announcement, proxy forms, director list, mandate shares, monthly returns, next-day disclosure.

**Parsing quirks:** Title-search servlet returns JSON-in-JSON (outer `result` is a JSON-encoded string). 3-day lookback, `rowRange=200`. HKT (UTC+8) → UTC on source_date.

### 4.10 kind_scanner.py (KR — `daily`, via OpenDART)

**Korean title regex classifier:**

| Pattern | signal_type | profile | dir |
|---|---|---|---|
| 공개매수 | `tender_offer` | merger_arb | long |
| 합병 (not merger contract) | `merger_announcement` | merger_arb | unknown |
| 분할합병 \| 합병계약 | `merger_contract` | merger_arb | unknown |
| 경영권 \| 지배주주.*변경 | `control_change` | merger_arb | long |
| 주식등의대량보유 \| 5%룰 | `large_holding` | activist_governance | — |
| 지분공시 \| 임원.*주식소유 | `ownership_disclosure` | activist_governance | — |
| 횡령 \| 배임 | `fraud_allegation` | activist_governance | short |
| 감사의견.*(거절\|한정\|부적정) | `adverse_audit_opinion` | activist_governance | short |
| 상장폐지 \| 매매거래정지 | `delisting_or_halt` | activist_governance | short |
| 영업정지 \| 영업중단 | `operations_suspended` | activist_governance | short |
| 유상증자 | `rights_issue` | activist_governance | short |
| 전환사채.*발행 \| 신주인수권부사채 | `convertible_issuance` | activist_governance | short |
| 소송.*제기 \| 판결 | `litigation_filed` | litigation | short |
| 매출액.*감소 \| 영업.*감소 | `profit_warning` | activist_governance | short |

**Boilerplate drops:** 사업보고서 (annual), 반기보고서 (half-year), 분기보고서 (quarterly), 감사보고서 (audit, unless rejected), 주주총회소집 (AGM), 배당 (dividend), 공시서류 (filing deadline), 증권발행실적 (issuance report), 자기주식.*취득결과 (buyback completion).

**Auth.** `OPENDART_KEY` env var (free, 20k req/day). OpenDART status codes: 000 = success, 013 = no data, others = error. Scanner returns `status=auth_required` cleanly if key missing (Q-019 closed D-025).

**Lookback:** 3 days; paginated to 1000 records/scan.

### 4.11 bse_nse_scanner.py (IN — `daily`, NSE only)

**English/Hindi description patterns (NSE corporate-announcements API):**
- Acquisition → `acquisition`, merger_arb, long.
- Amalgamation / merger → `amalgamation_merger`, merger_arb, unknown.
- Scheme of arrangement → `scheme_of_arrangement`, merger_arb, long.
- Open offer / delisting → `open_offer`, merger_arb, long.
- Buyback → `buyback`, activist_governance, long.
- SEBI takeover regulations → `takeover_disclosure`, activist_governance.
- Substantial acquisition / promoter change → `major_shareholder_change`, activist_governance.
- Auditor change / resignation → `auditor_change`, activist_governance, short.
- Independent director resignation → `independent_director_resignation`, activist_governance, short.
- Material issue disclosure → `material_issue`, activist_governance, short.
- Trading suspension → `trading_suspension`, activist_governance, short.
- Pending litigation → `pending_litigation`, litigation, short.
- Profit warning / impairment → `profit_warning`, activist_governance, short.
- Operations halted / shutdown → `operational_shock`, activist_governance, short.

**Boilerplate drops:** investor presentations, press releases, analyst meetings, shareholder meetings, dividend declarations, capacity updates, AGM/EGM notices.

**Warmup.** NSE requires home-page cookie warmup before API call (otherwise WAF block).

**Lookback:** 3 days; ~1600 records/window; typical yield ~100 tradeable signals (80 takeover disclosures + ~20 M&A/governance/litigation). IST (UTC+5:30) → UTC on source_date.

**BSE status:** blocked by WAF interstitial; NSE only.

### 4.12 cvm_scanner.py (BR — `daily`)

**Portuguese patterns (against Fato Relevante + Comunicado ao Mercado + RPT + Judicial Recovery):**
- OPA / Oferta Pública de Aquisição → `tender_offer`, merger_arb, long.
- Fusão / incorporação → `merger_announcement`, merger_arb.
- Cisão → `spinoff`, merger_arb.
- Aquisição/alienação de participação / Art. 12 CVM → `major_shareholder_change`, activist_governance.
- Acordo de acionistas → `shareholder_agreement`, activist_governance.
- Mudança de auditor → `auditor_change`, activist_governance, short.
- Renúncia de conselho / diretor → `board_resignation`, activist_governance, short.
- Destituição de administrador → `board_shakeup`, activist_governance.
- Investigação → `regulatory_investigation`, activist_governance, short.
- Recuperação judicial / falência → `judicial_recovery`, activist_governance, short.
- Adiamento de divulgação → `earnings_delay`, activist_governance, short.
- Conversão / cancelamento de registro → `delisting`, activist_governance, short.
- Memorando de entendimento / MOU → `mou_signed`, activist_governance, long.
- Leilão → `auction_result`, activist_governance.
- Partes relacionadas → `related_party_transaction`, activist_governance.
- Ação judicial / sentença / condenação → `litigation_event`, litigation, short.

**Data pipeline.** Downloads annual IPE zip from `dados.cvm.gov.br`, parses latin-1 CSV with `;` delimiter, filters to the 4 target categories. Ticker is null (CVM IPE doesn't include B3 ticker) — matching relies on `company_name_en` + `codigo_cvm` + CNPJ.

**Lookback:** 7 days (weekends); BRT (UTC-3) → UTC.

### 4.13 bmv_scanner.py (MX — `daily`, via BIVA JSON)

**Spanish patterns (BIVA `/emisoras/eventos-relevantes` JSON):**
- Fusión → `merger_announcement`, merger_arb.
- Adquisición → `acquisition`, merger_arb, long.
- OPA / OPC → `tender_offer`, merger_arb, long.
- Escisión → `spinoff`, merger_arb.
- Desliste / cancelación de inscripción → `delisting`, merger_arb, long.
- Cambio de control → `change_of_control`, activist_governance.
- Accionista mayoritario → `major_shareholder_change`, activist_governance.
- Auditor change → `auditor_change`, activist_governance, short.
- Director / consejero resignation → `board_resignation`, activist_governance, short.
- Board appointment → `board_appointment`, activist_governance.
- Concurso mercantil / insolvencia → `insolvency`, activist_governance, short.
- Going concern / duda sobre continuidad → `going_concern`, activist_governance, short.
- Suspensión de cotización → `trading_suspension`, activist_governance, short.
- Investigación / sanción / multa / CNBV → `regulatory_investigation`, activist_governance, short.
- Demanda / litigio / amparo → `litigation_event`, litigation, short.
- Advertencia / alerta de resultados → `profit_warning`, activist_governance, short.
- Impairment → `impairment`, activist_governance, short.
- Rating downgrade (HR Ratings / Fitch MX / Moody's MX) → `rating_downgrade`, activist_governance, short.
- Rating watch negative → `rating_watch_negative`, activist_governance, short.

**Boilerplate drops:** "Afirma" (rating affirmation), AGM notices, dividend declarations, financial reports.

**Schema (BIVA):** `clave` (ticker), `tipoDocumento` (subject Spanish), `fechaPublicacion` (epoch ms), `seccion` (emisora vs calificadora). Ticker-only — no company long-name.

**Lookback:** 14 days.

### 4.14 courtlistener_scanner.py (US — `daily`)

**NOS code filter:** 850 (securities), 190 (contract M&A), 830 + 835 (patent), 410 (antitrust).

**Signal-type classification (case_name heuristics):**
- "class certif" → `class_certified`, short.
- "settlement" → `settlement`.
- "summary judgment" → `summary_judgment`.
- "motion to dismiss" + "denied" → `mtd_denied`, short.
- Default → `federal_civil_filed`, short.

**Ticker hint extraction:** regex `\(\s*"?([A-Z]{2,5})"?\s*\)` in case name.

**Lookback:** 7 days; search endpoint (Solr-backed), not dockets endpoint.

**Auth.** `COURTLISTENER_TOKEN` env var (Q-017 closed D-024).

### 4.15 sec_enforcement_scanner.py (US — `daily`)

**RSS feeds:**
- `https://www.sec.gov/enforcement-litigation/litigation-releases/rss` → `sec_litigation` / `litigation_release`, short.
- `https://www.sec.gov/enforcement-litigation/administrative-proceedings/rss` → `sec_admin_proceeding` / `cease_and_desist`, short.

**Corp-entity filter (tradeability):** keeps if ticker-hint regex matches OR corporate keywords (Inc, Corp, LLC, Holdings, etc.). Drops solo-individual cases.

### 4.16 takeover_candidate_scanner.py (US — `weekly`)

**5 setup patterns (must ≥ 2):**
1. **13G/13D filing from PE allowlist** — 45-day lookback. Allowlist in `engine/config/pe_filer_allowlist.json` (merged `filers` + `activist_crossover`).
2. **Strategic-review language** — 8-K/10-K/10-Q, 60-day lookback. Keywords: "strategic alternatives", "exploring strategic alternatives", "review of strategic alternatives", "financial advisor" + "Board", "retained/engaged as financial advisor".
3. **Streamlined-for-sale signals** — 8-K only. Keywords: "divested" + "non-core", "portfolio simplification", "appointed Chief Financial Officer" (signals housecleaning).
4. **Insider + institutional accumulation** — Form 4 buying + 13F shift (separate pattern).
5. **Strategic buyer fit** — heuristic sector match (separate module).

**Post-edge disqualifiers (hard kill):** DEFM14A, SC TO-T, SC 13E3, 425 filed in last 30 days.

**Strength scoring:** base = 2; +1 per pattern matched (max 5); +1 if 13D/G from PE allowlist.

**Wall-clock budget:** 90s (weekly cadence).

### 4.17 pre_phase3_readout_scanner.py (US — `weekly`, ClinicalTrials.gov)

**Indication → base-rate key** (regex order of specificity):
- "alzheimer" → `neurology_alzheimers`.
- "ALS" → `neurology_als`.
- "parkinson" → `neurology_parkinsons`.
- "depression|MDD" → `psychiatry_depression`.
- "hepatitis" → `hepatology_hepb`.
- "NASH|steatohepatit" → `gastro_nash`.
- "Crohn|ulcerative colitis|IBD" → `gastro_ibd`.
- "RA|rheumatoid arthritis" → `rheumatology_ra`.
- "wet AMD|macular degeneration" → `ophthalmology_wet_amd`.
- "sickle cell" → `hematology_sickle_cell`.
- "obesity|weight management" → `metabolic_obesity`.
- "diabetes|T2DM" → `metabolic_diabetes`.
- "heart failure|atrial fib|MACE|cardiovascular" → `cardiovascular`.
- "COPD" → `respiratory_copd`.
- "lymphoma|leukemia|myeloma" → `oncology_hematologic`.
- "carcinoma|tumor|melanoma|cancer" → `oncology_solid_tumor`.
- (~37 total keys.)

**Readout window:** −14d to +90d from PrimaryCompletionDate.

**approval_probability → score (weight ×2.5 in binary_catalyst rubric):** ≥ 0.75 → 5; 0.65–0.75 → 4; 0.55–0.65 → 3; 0.45–0.55 → 2; < 0.45 → 1. Default 0.58 if indication key missing from `config/phase3_approval_base_rates.json`.

**Filter:** Phase 3 AND (ACTIVE_NOT_RECRUITING OR COMPLETED) AND industry sponsor AND ≥ 3 of 5 patterns.

---

## 5. Enrichers (post-scan scoring layers)

Enrichers run **after** the scanner has scored and written the signal. They add orthogonal dimensions without changing the base rubric score. Three live enrichers plus one inline one.

### 5.1 legal_enricher.py (hook #3, ROADMAP step 3 / D-028)

**Applies to:** litigation-profile signals only.

**Severity tiers (1–5):** Negligible → Low → Moderate → High → Critical.
**Likelihood tiers (1–5):** Remote → Unlikely → Possible → Likely → Almost Certain.

**Baselines (`BASELINE_BY_TYPE`, per `(scanner, signal_type)`):**
- `(sec_enforcement_scanner, litigation_release)` → (severity=4, likelihood=5).
- `(courtlistener_scanner, securities_class_action)` → (severity=4, likelihood=3).
- Generic patent_infringement → (severity=3, likelihood=3).

**Severity boost (keyword match):** "fraud|criminal|indict|ponzi|insider trading" → +2 (Critical); "class action|securities|injunction|consent decree|disgorge|settlement" → +1. Only one boost is applied (break after first match).

**Likelihood adjust:** "settled|verdict|conviction" → +2 (already-decided); "motion to dismiss denied|trial set" → +1 (late-stage); "complaint filed|preliminary" → −1 (early-stage).

**Risk score.** `risk_score = severity_tier × likelihood_tier`, range 1–25.

**Risk color.**
- GREEN: 1–4
- YELLOW: 5–9
- ORANGE: 10–15
- RED: 16–25

**Output stamped on signal (`legal_enrichment` key):** severity_tier, severity_label, likelihood_tier, likelihood_label, risk_score, risk_color, regulations, explanation, enriched_at.

**Regulations map:** SEC scanner → Securities Act 1933, SEA 1934, SOX 2002. Patent → Sherman + Clayton. Biotech-keyword → FFDCA (21 USC §301).

**Report sink.** Writes `working/legal_enrichment_report_YYYY-MM-DD.json`. `report_generator._append_legal_desk` renders top-12 enriched signals in last 3 days, sorted by risk_score desc.

### 5.2 biotech_enricher.py (hook #5, ROADMAP step 5 / D-030)

**Applies to:** binary_catalyst signals (biotech — ~115 live as of 2026-04-21).

**Endpoint strength tier (1–5)** — baseline 3:
- Safety-only outcomes → 2.
- No primary endpoints → 2.
- ≥ 4 co-primaries → −1 (over-powered, endpoint dilution).
- Hard endpoint (OS / mortality / MACE) + single primary → 5.
- Hard endpoint alone → ≥ 4.
- Single primary + n ≥ 300 → ≥ 4.
- Floor: n < 50 caps at tier 2.

Regexes: `HARD_ENDPOINT_RE` (OS, mortality, MACE), `SURROGATE_ENDPOINT_RE` (PFS, ORR), `SOFT_ENDPOINT_RE` (safety-only demote).

**Sponsor tier (1–5):**
- Default tier 2 (NIH/FED) or 3 (INDUSTRY).
- Tier 5 (S — Big Pharma): `BIG_PHARMA_RE` matches ~102 companies ("Pfizer|Novartis|Roche|Johnson & Johnson|Merck|…").
- Tier 4 (A): `MID_CAP_BIOTECH_RE` ("Incyte|Seagen|Jazz|…") OR industry sponsor with enrollment ≥ 300.
- Rule: enrollment < 300 caps tier at 3 regardless.

**Indication resolver:** 37 base-rate keys via ordered regex rules (examples under §4.17). Returns `(base_rate_key, resolved_by_enricher_flag)`; only overrides scanner "default" value.

**Mechanism class** (from headline + summary via `MECHANISM_RULES`): "mAb / ADC", "gene therapy / editing", "RNA therapeutic", "mRNA", "cell therapy / bispecific", "kinase inhibitor", "immune checkpoint", "GLP-1 / incretin", "vaccine", "small molecule", "unclassified".

**enrichment_score 0–100 formula:**

```
ep  = (endpoint_tier − 1) / 4.0          # 0..1, weight 40%
sp  = (sponsor_tier  − 1) / 4.0          # 0..1, weight 25%
ind = 1.0 if indication_resolved else 0.0   # weight 10%
lit = bucket(pubmed_articles, 5, 100)    # weight 15% (null → 0.5)
pre = bucket(biorxiv_preprints, 1, 20)   # weight 10% (null → 0.5)
weighted = ep*0.40 + sp*0.25 + ind*0.10 + lit*0.15 + pre*0.10
score = round(weighted * 100)            # integer 0..100
```

**Color bands:** GREEN ≥ 75; YELLOW 55–74; ORANGE 35–54; RED < 35.

**Cache TTL:** 7 days (`CACHE_TTL_DAYS = 7`), stored in `working/biotech_cache.json`.

**Report sink.** `working/biotech_enrichment_report_YYYY-MM-DD.json`; `report_generator._append_biotech_desk` renders Biotech Desk section.

### 5.3 deal_context_enricher.py (inline from `thesis_draft.draft_markdown`, ROADMAP step 6 / D-031)

**Applies to:** `merger_arb` and `activist_governance` signals.
**US-issuer gate:** only MIC in `US_MICS = {"XNYS", "XNAS", "XASE", "BATS", "ARCX", "IEXG"}`. Non-US returns `{status: "unavailable", reason: "non_us_issuer"}`. Offline mode returns `{status: "pending_online"}` with CLI hint.

**Not a post-scan hook** by design — avoids making scheduled cadence depend on SEC network.

**SEC XBRL companyfacts concept preferences:**
- Revenue → Revenues, RevenueFromContractWithCustomer*, SalesRevenueNet.
- Operating income → OperatingIncomeLoss.
- SG&A → SellingGeneralAndAdministrativeExpense, GeneralAndAdministrativeExpense.
- D&A → DepreciationDepletionAndAmortization, DepreciationAndAmortization.
- Cash flow ops → NetCashProvidedByUsedInOperatingActivities.
- Debt → LongTermDebt, ShortTermBorrowings.
- Cash → CashAndCashEquivalentsAtCarryingValue, CashCashEquivalentsAndShortTermInvestments.
- Shares → CommonStockSharesOutstanding, EntityCommonStockSharesOutstanding.

**3-year trajectory:** Pulls last 3 fiscal years from 10-K where both revenue and operating_income exist. Per year computes: revenue, operating_income, sga_pct, op_margin_pct, ebitda_margin_pct, fcf. YoY variances: `revenue_yoy_pct`, `op_margin_delta_bps`, `sga_pct_delta_bps`, `fcf_yoy_pct`.

Derived: `op_margin = operating_income / revenue * 100`; `ebitda = operating_income + D&A`; `fcf = cash_from_ops − capex`.

**EV/EBITDA proxy:**

```
ebitda   = operating_income + D&A (TTM)
net_debt = long_term_debt + short_term_debt − cash
market_cap = ref_price × shares_outstanding   # optional from yfinance
EV = market_cap + net_debt
ev_ebitda = EV / ebitda
```

**financial_deterioration_score (activist lens):**

```
op_margin_delta_bps  = int((op_margin_FY1 − op_margin_FY0) * 100)
sga_pct_delta_bps    = int((sga_pct_FY1 − sga_pct_FY0) * 100)
op_leg  = max(0, -op_margin_delta_bps)    # margin compression
sga_leg = max(0,  sga_pct_delta_bps)      # SG&A bloat
raw     = op_leg + sga_leg
score   = min(100, raw)                   # 1 bp deterioration = 1 point
```

**Deterioration color bands:** GREEN < 10; YELLOW 10–29; ORANGE 30–59; RED ≥ 60.

**Cache TTL:** 30 days (`working/deal_context_cache.json`).
**Report sink:** `working/deal_context_report_YYYY-MM-DD.json`.

---

## 6. Convergence Engine

`tools/convergence_engine.py` groups log signals by issuer and applies a cross-scanner bonus.

**Grouping key priority:**
1. `issuer_figi`
2. `ticker + mic`
3. `ticker` alone
4. Venue-specific: `codigo_cvm` (BR), `id_empresa_biva` (MX), `stock_code` (HK/KR)
5. Normalized `company_name_en` (strip corp suffixes: inc, corp, llc, ltd, sa, plc, nv, ag, gmbh, kk, co, company)
6. Fallback: `unidentified:<signal_id>` (never collides)

**Time windows:** 14 days normally; 30 days if any signal in group has `scoring_profile == "litigation"`.

**Dedup within group:** two signals sharing `source_content_hash` count as ONE (cross-listing echo protection).

**Classification (`_classify`):**
- Both `long` and `short` present → `contradiction`, bonus = 0.
- All same direction → check count.
- ≥ 3 independent signals → bonus = +10.
- 2 signals → bonus = +5.
- < 2 → `single`, bonus = 0.
- If multiple profiles in same-direction group → type = `orthogonal` (event-driven + positioning).

**Bonus application:** bonus stamped on the highest-scoring signal in the group:
```
scoring.convergence_bonus = bonus
scoring.score_with_bonus  = round(score + bonus, 2)
```

**band_with_bonus classification (D-034, 2026-04-21 shifts 35/25/15 → 30/20/10):**
- ≥ 30 → `immediate`
- ≥ 20 → `watchlist`
- ≥ 10 → `archive`
- < 10 → `discard`

**Output:** `working/convergence_report_YYYY-MM-DD.json` + mutated signal_log entries.

---

## 7. Candidate Gate (the thesis quality gate)

`tools/candidate_gate.py::promote_candidate(signal, thesis, band=..., scoring_profile=...)` is the **only** sanctioned write path to `candidates/`. Direct writes are a bug.

**Required thesis fields (all non-empty, non-boilerplate):**
- `situation` — ≥ 80 non-whitespace chars.
- `why_underpriced` — ≥ 100 chars.
- `next_catalyst` — ≥ 40 chars.
- `next_catalyst_date` — ISO or recognizable range.
- `kill_conditions` — ≥ 60 chars.

**Boilerplate rejection patterns** (fail thesis if any match):
- `scanner\s+classified\s+signal_type`
- `tdnet\s+filed\s+\w+\s+for`
- `auto[-\s]generated\s+by`
- `placeholder\s+thesis`
- `no\s+thesis\s+yet`
- `to\s+be\s+researched`

**Date sanity** — accept ISO (`^\d{4}-\d{2}-\d{2}`), band (`^(Q[1-4]|H[12]|early|mid|late)\s+\d{4}`), or month (`January|February|…|Dec\s+\d{4}`).

**Band values accepted:** `immediate` or `watchlist`. (No direct writes to archive/discard — those just don't promote.)

**Rejections** appended to `working/rejected_promotions_<today>.json` with rejection reasons, intended band, scoring profile, thesis provided — visible for follow-up research.

**Audit CLI:** `python candidate_gate.py --audit` scans `candidates/` + `candidates/watchlist/` for files missing a thesis and writes `working/thesis_gate_audit_<date>.json`. `--demote-stubs` moves orphan JSON stubs to `candidates/rejected_pending_thesis/`.

**Draft containment (D-027):** `build_dashboard.py` + `report_generator.py` filter any candidate with `_draft: true` or `[DRAFT]`/`[TODO]` placeholders out of the Candidate queue + executive summary. Drafts surface in a dedicated "Drafts pending curation" panel. `_extract_ticker` uses `**Ticker**: X` lookup + `_PLACEHOLDER_TICKERS` reject list to block stale `DRAFT.pdf` / `TODO.pdf` dossiers.

---

## 8. Thesis Drafting (automated draft path)

`tools/thesis_draft.py::draft_markdown(signal)` auto-generates candidate `.md` drafts for qualifying signals.

**Qualification gate (all required):**
1. `score_total ≥ SCORE_FLOOR` — 30.0 (D-034, lowered from 35).
2. Rich record lookup succeeds in scanner output JSON.
3. Ticker plausible: alphanumeric, `len ≤ 10`, not in `{HR, MOODYS, FITCH, SP, DBRS}` (rating-agency aliases).
4. Not already in `curated/`, `archived/`, or `_discarded_at_curation/`.
5. yfinance resolves `market_cap_usd ≥ MCAP_FLOOR_USD` ($215M).

**Market-cap conversion** (yfinance may return local currency) — FX map:
- USD 1.0, EUR 1.08, GBP 1.26, HKD 0.128, JPY 0.0063, KRW 0.00074, BRL 0.19, INR 0.012.

**yfinance ticker suffix by MIC:** XNAS/XNYS → ""; XLON → ".L"; XTKS → ".T"; XHKG → ".HK"; XSHG → ".SS"; XKRX → ".KS"; XBOM → ".BO"; XNSE → ".NS"; XTSE → ".TO"; XASX → ".AX"; XBMV → ".MX"; XBSP → ".SA".

**Profile inference:** if `scoring_profile` not set, fallback via `SCANNER_DEFAULT_PROFILE` map.

**Inline deal_context call:** for `merger_arb` or `activist_governance` profiles, `draft_markdown` calls `deal_context_enricher.enrich(signal, online=True)` and renders the result.

### 8.1 thesis_analyst.py (evidence synthesis)

**Confidence rating:**
- HIGH — source doc parsed + (≥ 2 prior signals on issuer OR successful enrichment).
- MEDIUM — source doc parsed, sparse history, no enrichment.
- LOW — source doc unparseable OR only one signal with no supporting evidence.

**Per-profile analytical lens (direction + default kill triggers):**

| Profile | Default direction | Kill triggers |
|---|---|---|
| `merger_arb` | long | Regulator Phase II, acquirer stock collapse, MAC clause invoked, failed shareholder vote, amended downward |
| `activist_governance` | long | 13D/A stake cut < 5%, settlement collapse, poison pill adopted, ISS backs management, white-knight weak premium |
| `binary_catalyst` | long | Primary endpoint miss, clinical hold, readout slip > 6 months, competitor wins same indication, deep-discount equity raise |
| `litigation` | short | Motion to dismiss granted, settlement < insurance retention, regulator drops, adverse precedent, reserve > exposure |
| `takeover_candidate` | long | Shareholder renounces sale, review terminated, sector multiples compress, hostile issuance, bidder walks |
| `short_positioning` | short | Short interest declines > 30%, hard-to-borrow clears, emergency capital raise, adverse regulatory, squeeze dynamics |

**Jurisdiction lens.** MIC → {country, regulator, filings_hub} map: XTKS (Japan, JPX/FSA, TDNet), XLON (UK, FCA, RNS), XNAS/XNYS (USA, SEC, EDGAR), XASX (Australia, ASIC, ASX), XTSE (Canada, CSA, SEDAR+), XHKG (HK, SFC, HKEX), XKRX (Korea, FSS, DART), XBOM/XNSE (India, SEBI, NSE), XBSP (Brazil, CVM, CVM IPE), XBMV (Mexico, CNBV, BIVA).

---

## 9. Kill Watch (structured invalidation rules)

`tools/kill_watch.py` evaluates a DSL of "kill rules" attached to active candidates.

**Rule schema:**
```json
{
  "id": "axsm_major_amendment_8k",
  "description": "FDA Major Amendment delay",
  "kind": "edgar_8k_items | edgar_form | price_move | news_keyword | competitor_move",
  "params": { "cik": "...", "items": [...], "keywords": [...], "max_age_days": 30 },
  "action": "review | archive | cap_watchlist",
  "severity": "high | medium | low"
}
```

**Probe types and parameters:**
- `price_move` — threshold_pct, direction (up/down/abs), window_days.
- `edgar_form` — forms list, max_age_days.
- `edgar_8k_items` — items list, keywords REQUIRED in body, max_age_days.
- `news_keyword` — regex patterns, max_age_days.
- `competitor_move` — ticker, threshold_pct, direction, window_days.

**Action semantics:**
- `archive` → escalates to `AUTO_ARCHIVE` in `candidate_monitor` (next run).
- `review` → logged but no state mutation.
- `cap_watchlist` → reserved for future scoring band integration.

---

## 10. Candidate Monitor (post-edge detection + auto-archive)

`tools/candidate_monitor.py` runs every operational cycle (hook #2) and applies post-edge detection to active candidates.

**Probe thresholds:**
- Price move ±15% → strong; ±20% → autoarch candidate within 7 days.
- EDGAR resolution-form lookback: 14 days for DEFM14A, S-4, 425, etc.
- Activist settlement forms: SC 13D/A, DFAN14A, DEFA14A.

**Classification:**
- `AUTO_ARCHIVE` — any `edgar_resolution_filing` AND no future catalyst (> 7 days out); OR price strong (≥ 20%) AND (news_match OR fda_submission); OR price strong AND past catalyst.
- `REVIEW` — any trigger without AUTO_ARCHIVE criteria.
- `NOOP` — no triggers.

**Keyword sets (per profile):**
- `pdufa`: "approv|crl|fda grants|priority review|label expand|major amendment".
- `merger_arb`: "definitive agreement|to be acquired|take-private|antitrust approval|deal closes".
- `activist`: "settlement|board seats|poison pill|withdraws 13D|accepts offer".

**Reversibility:** audit JSONL records every decision with `pre_state`. `--undo <TICKER>` restores latest audit entry.

---

## 11. Signal-Log Validator (5 integrity checks)

`tools/validate_signal_log.py` (hook #4, D-029). Writes `signals/_validation_report.json`; `health_check` has a `validation` family reading it.

| # | Check | YELLOW | RED |
|---|---|---|---|
| 1 | Duplicate fingerprints | Logical dup < 20 | Hard dup any; logical ≥ 20 |
| 2 | Null rates per field | > 5% | > 20% |
| 3 | Ticker/MIC consistency | ticker ≠ prefix of ticker_plus_mic | — |
| 4 | Stale convergence groups | Report > 3 days; < 25% membership survived | < 3 groups w/ < 25% |
| 5 | Orphan signals (scanner not in registry + no output file) | any | — |

**ticker_plus_mic shape:** `^[A-Z0-9.\-]{1,32}\.[A-Z]{4}$` (SYMBOL.MIC4).

**Ticker-less-by-design scanners** excluded from ticker null check: `pre_phase3_readout`, `cvm`, `courtlistener`, `sec_enforcement`. But any issuer id (ticker | figi | cik | cvm | name) MUST be present.

---

## 12. Health Check (6 check families)

`tools/health_check.py` (hook #6, D-021). Writes `working/health_report_<date>.json`.

**Family 1 — Registry coherence:** every operational scanner must have produced output within 2× its cadence (1440m daily, 10080m weekly, 60m hourly). RED if stale > 2×, YELLOW if > 1.5×.

**Family 2 — Scanner output schema:** required fields (signal_id, source_date); required at least one of (ticker_plus_mic, ticker, ticker_local, figi, cik, cnpj, codigo_cvm, company_name_en/local). RED if required missing; YELLOW if any issuer id missing.

**Family 3 — Drift detection:** compare signal count / unique ticker count / mean score per scanner against `health_history.jsonl` baselines. Thresholds: > 50% signal count drop = RED, > 30% mean score shift = YELLOW.

**Family 4 — Signal log integrity:** reads `signals/_validation_report.json`.

**Family 5 — Curated file integrity:** JSON parseability, ticker key uniformity, draft entries > 7 days old (`DRAFT_STALE_DAYS`), archived entries missing outcome field.

**Family 6 — Working dir hygiene:** files older than 30 days (`DIR_STALE_DAYS`) not rotated: convergence_report_*, maintenance_log_*.

---

## 13. End-to-End Scoring Example (how one signal becomes a candidate)

```
1. edgar_filing_monitor.py picks up a new SC 13D filing
   → signal_type="activist_ownership", profile="activist_governance"
   → base strength 4 (13D filing bump)

2. Scanner seeds 7-dim rubric:
     signal_strength=4, clarity=3, asymm=4 (just filed), risk_reward=3,
     edge=3, liquidity=4, timeline=3
   Weighted total = 4*2 + 4*2 + 3*1.5 + 3*1.5 + 3*1 + 3*1 + 4*1
                  = 8 + 8 + 4.5 + 4.5 + 3 + 3 + 4  = 35.0  → band="immediate"

3. Scanner appends to signal_log.json.

4. Post-scan chain runs:
   - convergence_engine finds another filing (Form 4 insider buy) on same
     CIK within 14d → 2 signals, orthogonal (activist + insider buy) →
     bonus=+5. Stamps scoring.convergence_bonus=5, score_with_bonus=40,
     band_with_bonus="immediate".
   - legal_enricher skips (profile != litigation).
   - validate_signal_log OK.
   - biotech_enricher skips.
   - health_check passes.

5. thesis_draft picks up the signal (score 35 ≥ SCORE_FLOOR 30):
   - ticker plausible, not already curated, yfinance mcap $3.2B ≥ $215M.
   - profile=activist_governance → calls deal_context_enricher inline.
     Enricher returns 3-year trajectory + op_margin delta +80bp (margin
     expanding), financial_deterioration_score=0 (GREEN) — suggests
     "activist demanding margin recapture" lens.
   - Writes candidate .md draft marked _draft:true.

6. Curator (human, or a future agent) fills in the required thesis fields:
   situation, why_underpriced, next_catalyst, next_catalyst_date,
   kill_conditions. Removes _draft flag.

7. candidate_gate.promote_candidate() validates thesis (all required
   fields present, non-boilerplate, min char counts met). Writes final
   candidate to candidates/<TICKER>_<MIC>_<slug>.md.

8. report_generator.publish_reporting() next run:
   - Renders executive summary (reports/executive_summary.pdf)
   - Renders detail book (reports/detail_book.pdf)
   - Renders dossier (reports/dossiers/<TICKER>.pdf)
   - Copies exec summary to TODAY.pdf
   - build_dashboard.py updates DASHBOARD.html

9. Monitoring: candidate_monitor + kill_watch run every cycle, checking
   for invalidation triggers. If triggered -> auto_archive or review flag.
```

---

## 14. Reference Paths (file layout to replicate)

```
Conan/
├── DASHBOARD.html                     # Live snapshot (build_dashboard.py)
├── TODAY.pdf                          # Latest exec summary copy
├── README.md
├── engine/
│   ├── config/
│   │   ├── scanner_registry.json      # 17 scanners, cadence, endpoints
│   │   ├── pe_filer_allowlist.json    # For takeover_candidate_scanner
│   │   ├── phase3_approval_base_rates.json  # For pre_phase3_readout
│   │   └── secrets.env                # COURTLISTENER_TOKEN, OPENDART_KEY
│   ├── framework/
│   │   ├── profile_merger_arb.md
│   │   ├── profile_activist_governance.md
│   │   ├── profile_binary_catalyst.md
│   │   ├── profile_short_positioning.md
│   │   ├── profile_litigation.md
│   │   ├── profile_takeover_candidate.md
│   │   └── candidate_template.md
│   ├── signals/
│   │   ├── signal_log.json            # Rolling 14-day (90d for litigation)
│   │   └── _validation_report.json    # From validate_signal_log.py
│   ├── tools/                         # All 17 scanners + enrichers + gate
│   ├── working/
│   │   ├── openfigi_cache/
│   │   ├── biotech_cache.json          # 7-day TTL
│   │   ├── deal_context_cache.json     # 30-day TTL
│   │   ├── convergence_report_*.json
│   │   ├── catalyst_calendar.json
│   │   ├── candidate_monitor_report_*.json
│   │   ├── health_report_*.json
│   │   ├── legal_enrichment_report_*.json
│   │   ├── biotech_enrichment_report_*.json
│   │   ├── deal_context_report_*.json
│   │   ├── rejected_promotions_*.json
│   │   └── thesis_gate_audit_*.json
│   ├── candidates/
│   │   ├── <TICKER>_<MIC>_<slug>.md   # Promoted candidates
│   │   ├── watchlist/
│   │   └── rejected_pending_thesis/
│   ├── reports/                       # Engine-internal pre-publish
│   └── docs/
│       ├── INSTRUCTIONS.md
│       ├── OBJECTIVES.md
│       ├── CONTEXT.md
│       ├── DECISIONS.md               # D-001..D-031
│       ├── PROGRESS_LOG.md
│       ├── OPEN_QUESTIONS.md
│       ├── ROADMAP.md
│       └── SESSION_STATE.md
└── reports/                           # User-facing published PDFs
    ├── executive_summary.pdf
    ├── detail_book.pdf
    └── dossiers/<TICKER>.pdf
```

---

## 15. Replication Checklist (what Claude Code needs to build)

To recreate Conan's scoring methodology from scratch, implement in this order:

1. **Schema layer** — define the signal record shape (§2.3) and the 6 scoring profiles (§3) as a data contract. Write the rubric files under `framework/profile_*.md`.

2. **Universal primitives** — `config/scanner_registry.json`, `tools/profile_map.py`, `tools/openfigi_resolver.py` (with the TDnet `469A0 → 469A` normalization), `tools/env_loader.py` (auto-loads `config/secrets.env` on import of `tools/http_client.py`), `tools/http_client.py`.

3. **One scanner end-to-end** — start with `edgar_filing_monitor.py` (US, rich enough to exercise all patterns: keyword classification, form whitelists, rotation, blacklist drops, strength formula). Verify it writes a schema-valid signal and updates `scanner_registry.json`.

4. **Validator and gate** — `tools/validate_signal_log.py` (5 checks), `tools/candidate_gate.py` (thesis DSL + boilerplate reject). These are guardrails that must exist before multi-scanner noise can drown the log.

5. **Remaining 16 scanners** — any order; reuse the rubric seed pattern. Wire each into `scanner_registry.json` with correct cadence.

6. **Convergence engine** — `tools/convergence_engine.py`. Implement grouping priority, dedup by content hash, classify + bonus math. Verify band_with_bonus thresholds (30/20/10/D-034).

7. **Enrichers** — `legal_enricher.py` (severity × likelihood, keyword boosts, risk_color bands), `biotech_enricher.py` (endpoint/sponsor/indication/mechanism + 0–100 score), `deal_context_enricher.py` (SEC XBRL 3-year trajectory + financial_deterioration_score). Cache TTLs: 7d biotech, 30d deal_context.

8. **Post-scan chain** — `run_post_scan.py` with 6 isolated try/except hooks in order: catalyst_calendar → candidate_monitor → legal_enricher → validate_signal_log → biotech_enricher → health_check.

9. **Thesis drafting** — `thesis_draft.py` (score floor + mcap floor + yfinance FX + MIC suffix map) calling `deal_context_enricher` inline for merger_arb/activist_governance. `thesis_analyst.py` (confidence + per-profile lens + jurisdiction lens).

10. **Kill watch + candidate monitor** — DSL with 5 probe types; auto-archive rules with 15%/20% price thresholds.

11. **Reporting** — `report_generator.py` producing executive summary + detail book + per-ticker dossiers; Legal Desk section (top-12 litigation by risk_score); Biotech Desk section (top-12 by enrichment_score); `build_dashboard.py` producing `Conan/DASHBOARD.html`.

12. **Scheduling** — three scheduled tasks (operational 0 */3 * * *, maintenance 50 */3 * * *, reporting 30 */4 * * *). Start disabled; enable only after full pipeline passes a dry run.

The single most important invariant across all of this: **no candidate exists without a complete, non-boilerplate written thesis.** Every other piece of infrastructure exists to feed or protect that invariant.

---

**END OF DOCUMENT.** Written 2026-04-21 against Conan @ `engine/` post-D-031.
