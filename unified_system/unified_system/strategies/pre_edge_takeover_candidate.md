# Strategy: Pre-Edge Takeover Candidate Scanner

## Purpose
Identify small/mid-cap public companies **likely to receive an M&A offer within 3–12 months** — *before* the deal is announced. Once a merger agreement is signed, the opportunity moves to the post-edge archive (see D-013). The scanner exists because the current merger-arb / EDGAR lanes only trigger *after* announcements have spiked the stock — too late to capture the premium.

**Why we need this lane**: AVNS received a 72% premium take-private on 2026-04-14. Pre-announcement, the name showed every classic setup signal: streamlined medtech, ~$850M mcap, stagnant stock, divested non-core lines, clean FCF. The system saw none of it until the press release crossed the wire.

## Data Sources
- **SEC EDGAR** (already operational): 13G / 13D filings from PE-adjacent filers, DEF 14A compensation changes, 10-K/10-Q language for strategic-review disclosures, insider Form 4 accumulation.
- **Press release + 8-K NLP**: scan for phrases "exploring strategic alternatives," "reviewing options," "hired financial advisor," specific banker mandates ("Centerview," "Goldman Sachs financial advisor to the Board").
- **yfinance / openFIGI**: market cap, 52-week price vs. 2-yr average, volume, institutional ownership %.
- **openFDA / press release corpus**: divestiture announcements ("sold non-core division to X"), portfolio simplification signals.
- **Reuters / Bloomberg M&A RSS** (optional, rate-limited): rumor aggregation with source-confidence flagging.
- **Cost**: free within existing scanner stack; no new paid feeds required.

## Signal Logic
A takeover candidate is a public company where one or more of the following setup patterns hold:

1. **PE take-private setup**: market cap $215M–$5B, clean balance sheet (net debt / EBITDA < 3x), stagnant share price (trading at or below 2-yr average), EBITDA margin > 15%, predictable FCF. PE's ideal target.
2. **Streamlined-for-sale pattern**: company has divested 1+ non-core divisions in the last 18 months, named a new CFO or strategic-advisor in the last 12 months, portfolio simplification announced.
3. **Strategic-review disclosure**: 8-K, proxy, or earnings call explicitly mentions "strategic alternatives" or "financial advisor engaged to explore options."
4. **Insider + institutional accumulation**: Form 4 insider buying in the trailing 90 days, 13G filings from known PE-adjacent funds (Silver Lake, KKR, Apollo, Blackstone, Thoma Bravo, Advent, American Industrial Partners).
5. **Strategic buyer fit**: company operates in a sector undergoing consolidation, with a known strategic buyer (named Fortune 500 competitor in M&A history) likely to fit.

Minimum bar: a candidate must hit **at least 2 of the 5 patterns** to be surfaced.

## Triage Filters (Stage 1)
- Market cap ≥ $215M USD (system-wide floor)
- Public on major exchange (NYSE, NASDAQ, or major non-US equivalents if applicable)
- Not currently under a definitive merger agreement (disqualifies post-edge)
- Not in a sector that excludes typical M&A (regulated utilities handled separately)
- Fresh within 30-day scanning window (re-evaluated on re-scan)

## Scoring (uses existing `activist_governance` rubric OR new `takeover_candidate` rubric)
Proposed new rubric `takeover_candidate` (0–50):
- **Setup Strength** (0–15): how many of the 5 patterns are hit, with weight for strategic-review language.
- **Edge Freshness** (0–10): how recently the key signal appeared — new signals score higher.
- **Valuation Cushion** (0–10): % discount to historical median EV/EBITDA or EV/Revenue — larger cushion = more room for a buyer's premium.
- **Strategic Buyer Clarity** (0–10): can we name a likely buyer? Named buyer with M&A history = max.
- **Liquidity** (0–5): 30-day ADV, spread, borrow cost.

Auto-caps:
- Definitive merger agreement already announced → disqualify (post-edge).
- Company has rejected a prior offer in the trailing 6 months → cap at archive (signals low management receptiveness).

## Execution
- **Frequency**: weekly (Sunday midnight UTC).
- **Output**: signals to `signals/` with `scoring_profile: takeover_candidate`, `thesis_direction: long`.
- **Candidate promotion threshold**: score ≥ 30 promotes to active candidate with a rationale that MUST name the expected catalyst path (PE buyer, strategic competitor, spin-off) and the "edge disappears if X" condition.
- **Expected pipeline volume**: 3–6 takeover candidates in active pool at any time.

## Expected Outcome / Kill Watch
- **Hit**: merger agreement announced within 6 months → candidate moves to archive with `outcome: WIN` and the premium captured.
- **Miss (edge disappears)**: company announces a bad-news event (earnings miss, management turnover, buyer walks away) that removes the setup. Archive with `outcome: MISS`.
- **Decay**: no deal within 12 months AND setup weakens → archive with `outcome: NEUTRAL`.

## Operational Integration
- Lives in `unified_system/tools/scanners/takeover_candidate_scanner.py` (new).
- Re-uses existing `openfigi_resolver.py`, `convergence_engine.py`, scoring infrastructure.
- Feeds into the same `_collect_all_candidates()` reporting pipeline; promoted candidates get a `.md` in `candidates/` and an entry in `_curated_rationales.json`.
- Post-edge gate applies: once a deal is signed, auto-move to `_archived`.

## Dependencies
- openFIGI resolver (already operational).
- EDGAR filing monitor (already operational — extends to pull 13G filings from PE-flagged filers).
- `scoring_profile: takeover_candidate` rubric added to `framework/profile_takeover_candidate.md` (new — to be written).
- Known-PE-filer allowlist at `config/pe_filer_allowlist.json` (new — to be written).

## Status
**Planned.** Not yet implemented. Implementation estimate: 2–3 scheduled sessions. Required before the pre-edge mandate is fully enforced with pipeline coverage.
