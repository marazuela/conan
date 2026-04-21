---
ticker_local: "PDI"
mic: XASX
ticker_plus_mic: PDI.XASX
figi: BBG0017X3YN4
issuer_figi: BBG0017X3YM5
company_name_local: "PREDICTIVE DISCOVERY LIMITED"
company_name_en: "Predictive Discovery Limited"
market_cap_usd_mm: 1781.7
exchange: ASX
country: AU
score: 28.0
convergence_bonus: 0
score_total: 28.0
convergence_strategy_count: 1
status: pending_deep_dive
thesis_direction: unknown
translation_confidence: "n/a"
first_signal_date: 2026-04-15
last_updated: 2026-04-16
primary_catalyst_date: 2026-04-15
cross_listed_on: []
related_signal_ids: []
signal_type: merger_agreement
signal_category: takeover
scanner: asx
---

# Predictive Discovery Limited (PDI.XASX) — "PDI & Robex Merger Update"

> **STATUS: pending_deep_dive.** Automated-scanner stub. Do not act on this alone.

## TL;DR

On 2026-04-15 (23:58 UTC) Predictive Discovery released an ASX announcement titled "PDI & Robex Merger Update" under the Takeover Announcements / Scheme Announcements category (documentKey `2924-03079443-6A1320718`). Scanner classifies as `merger_agreement`, signal_category `takeover`, strength=4, catalyst_clarity=5, thesis_direction=`unknown` (pending PDF read). Score 28 puts this in the immediate-route bucket. PDI is a West-African gold developer (Guinea, Bankan project); Robex Resources is a TSX-V/TSX-listed gold producer operating in Mali and Guinea. A merger between the two would create a ~2-3 Moz gold platform in the Siguiri-Mandiana belt. Deep-dive must determine: (a) whether this is an announcement, reaffirmation, or material amendment to a previously-disclosed scheme of arrangement; (b) the implied exchange ratio and target premium; (c) the regulatory/shareholder timeline; (d) whether the deal is contingent on financing.

## Source signal

- **Signal** — ASX announcement `2924-03079443-6A1320718` — 2026-04-15 23:58:29 UTC — ASX/markitdigital — headline "PDI & Robex Merger Update"; isPriceSensitive=false (operator should verify — takeover-scheme updates typically ARE price-sensitive; the false flag warrants PDF inspection); PDF 209KB; matches scanner pattern `\b(merger|merging)\b`.
- The markitdigital API does not expose the PDF URL; operator must pull the PDF directly from the ASX Market Announcements page at https://www.asx.com.au/markets/company/PDI once the deep-dive starts.

## Translation notes

English source, translation_confidence = n/a. No interpretive risk on the scanner side.

## Company context (to be completed by deep-dive)

- Market cap: $1.78B USD (AUD ~$2.7B at ~0.66 AUDUSD). Mid-cap gold developer.
- Sector: Materials (GICS) — gold exploration/development.
- Primary asset: Bankan Gold Project, Guinea (Siguiri basin). Last publicly disclosed resource base ≈ 5 Moz indicated+inferred (confirm from latest DFS/MRE announcements).
- Counterparty: Robex Resources Inc. — TSX/TSX-V listed; produces from Nampala mine (Mali) and has development-stage Kiniero project (Guinea). Market cap ≈ C$500M (confirm).
- Listing: ASX primary; no ADR; no dual listing currently confirmed — merger could create a TSX-listed combined entity.
- Trading status: N (normal) at scan time — no trading halt flagged.
- 30/90-day price: TODO. Pull from yfinance `PDI.AX` close history.
- Recent news: TODO — check AFR, Mining Journal, Reuters for prior merger milestones in 2025-2026. Scheme of arrangement documents will likely be on the PDI investor-relations page.
- Holders: TODO. PDI typically has a mix of Australian retail, mining-specialist funds (Franklin Gold & Precious Metals, Van Eck Gold Miners, Sprott) and possibly strategic industry holders. Any register change around the scheme is itself a signal.

## Thesis statement (to be completed by deep-dive)

**Pending.** Direction `unknown` until PDF is read.

Deep-dive must:
- Read the PDF to extract:
  - Status of merger (binding scheme agreed / amended / conditions precedent / approval obtained / terms re-cut)
  - Exchange ratio (PDI shares per Robex share, or vice versa) and implied PDI premium at the announcement spot
  - Whether the deal is now recommended unanimously by both boards
  - Break-fee / reverse break-fee quantum
  - Regulatory clearances still outstanding (FIRB in Australia, Canadian Competition Bureau, Guinea Mining Ministry)
  - Shareholder meeting date and court-sanction date
  - Any capital raise or bridge financing attached to the merger
- Determine the arb spread:
  - Spot PDI vs. (exchange ratio × spot Robex) in a common currency
  - Annualized based on time-to-close
- Cross-check:
  - PDI's standalone DFS NPV and the implied consideration as % of NPV
  - Robex's latest 43-101 resource/reserve statement and production profile
  - Gold price backdrop — merger arbs in gold-miner deals are sensitive to absolute gold (breakup risk rises if gold rallies hard, stand-alone value rises)
- Decide on instrument:
  - **Long PDI + short Robex (or vice versa)** — classic merger-arb — if the deal is binding scheme with firm exchange ratio
  - **Outright long PDI** — if the update is an *improved* bid or increased ratio
  - **Outright short PDI** — if the update signals deal-break risk (condition not met, regulator pushback, financing gap)
  - **Do nothing** — if this is routine procedural update with no new economic information

## Steelman of the opposite view

- **Update = no change**: "Merger Update" in ASX filings often just means a procedural tick (e.g. "court date confirmed" or "ineligible foreign shareholders process"). In that case scanner false-positive on the `\bmerger\b` pattern; no tradeable alpha.
- **Deal break priced in**: If PDI has been trading at a wide discount to Robex-implied consideration for weeks, the market has already signaled break risk and the "update" may just confirm it — no edge from entering after confirmation.
- **Gold tape driven**: Gold equities move in a 0.6+ beta to spot gold. Short-term P&L on the arb may be dominated by gold moves unless both legs are hedged — true arb spread may be much smaller than nominal PDI/Robex price moves imply.
- **Merger creates forced-seller flow**: Some Australian index-linked holders must sell on conversion to a TSX-primary listed entity (if that's the structure). Post-completion drift could be negative on PDI equivalent — so "long PDI into close" is not automatically right.
- **Guinea country risk**: Guinea imposed mining-code reforms and state-participation demands in 2022-2024. Any adverse ministry commentary during this merger window caps upside even if the deal closes.

## Web research layer (mandatory — pending)

- Original scheme-implementation agreement date and terms (PDI ASX releases H2 2025 / Q1 2026)
- Robex Resources SEDAR+ filings (management information circular for the Robex shareholder meeting)
- Independent Expert's Report (IER) on the PDI scheme — ASX release if prepared
- Mining Journal, Reuters, Bloomberg coverage of the 2026-04-15 update
- ASIC / ASX Corporate Governance approvals
- Canadian securities approvals (Quebec AMF if Robex is Quebec-domiciled)
- FIRB approval status (Australia) for any Guinean/Malian asset change of control
- Guinean Ministry of Mines public statements for 2025-2026 on Bankan / Kiniero
- Gold price: spot + 3-month forward, COMEX skew
- Options: PDI ASX options liquidity (typically thin for mid-cap miners)

## Kill conditions

- **Kill 1:** PDF reveals this is a pure procedural update (e.g. second-court-date confirmation) with no change to economic terms — no alpha; archive.
- **Kill 2:** Exchange ratio unchanged AND arb spread < 3% annualized — reward doesn't clear execution friction for a cross-listed AUD/CAD arb.
- **Kill 3:** Regulatory knockback (Guinea state participation demand, FIRB block) that materially changes the deal — outright long PDI thesis dead; reconsider as short.
- **Kill 4:** Gold spot falls >8% within 2 weeks of entry — gold-equity arb trades are swamped by underlying; unwind and re-enter in a calmer tape.
- **Kill 5:** Robex board withdraws recommendation. Merger-arb P&L flips to deal-break loss; cover immediately.
- **Kill 6:** Signal_type on re-scan reclassifies to `merger_termination` (already in the PDI-historical log as a null) — deal dead; close any long-PDI arb leg.

## Catalyst map

| Event | Date/Window | Entry trigger | Exit trigger |
|-------|-------------|---------------|--------------|
| "Merger Update" announcement | 2026-04-15 (done) | — | — |
| Price reaction next ASX session | 2026-04-16 AEST | PDI flat/down on positive update → add long | Up-gap >5% → trim |
| Independent Expert's Report (if pending) | TODO from scheme booklet | Fair & reasonable → confirm arb | Not fair → abort |
| Shareholder meeting (both sides) | TODO | Approval threshold met → collapse spread | Fail → short |
| Court sanction (Australian Federal Court) | TODO | Sanctioned → implementation | Not sanctioned → exit |
| Implementation / record date | TODO | — | Close arb at record |
| Regulatory long-stop date | TODO | — | Long-stop breach → deal terminates |

## Position sizing

Satellite (1–3%) if entering as a classic merger-arb. PDI ADV is modest (~$5-10M AUD/day) and Robex ADV on TSX is thinner; both legs need careful VWAP execution. Cross-currency (AUD/CAD) adds a small FX overlay — either hedge via AUDCAD forward or accept the residual. Options expression is impractical — both sides too illiquid.

## Source traceability

- ASX announcement `2924-03079443-6A1320718` retrieved 2026-04-16 via `tools/asx_scanner.py` from `asx.api.markitdigital.com/asx-research/1.0/companies/PDI/announcements`
- OpenFIGI: ticker=PDI mic=XASX → figi=BBG0017X3YN4, issuer_figi=BBG0017X3YM5 (cached 2026-04-16)
- Market cap: $1.78B USD — from `working/asx_universe.json` (as_of 2026-04-14T16:08:27Z; yfinance PDI.AX Ticker.info × AUDUSD)
- Scanner pattern matched: `\b(merger|merging)\b` (in `ASX_TITLE_RULES` → signal_type=`merger_agreement`)
- ASX headline: "PDI & Robex Merger Update"
- Announcement type (ASX): "Takeover Announcements/Scheme Announcements"
- Announcement flagged isPriceSensitive=false — VERIFY; scheme updates should normally be flagged true
- File: 209 KB PDF (not fetched — URL not exposed by markitdigital API; operator must pull from ASX Market Announcements page at time of deep-dive)
- Scan date: 2026-04-16; signal_id `0439e1303980b984c488d19b31a5229a`; score 28.0 (routed `immediate`)
