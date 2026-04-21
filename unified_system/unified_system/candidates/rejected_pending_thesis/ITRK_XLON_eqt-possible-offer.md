---
ticker_local: ITRK
mic: XLON
ticker_plus_mic: ITRK.XLON
isin: GB0031638363
sedol: 3163836
figi: null
issuer_figi: BBG000D9L3W0
company_name_local: Intertek Group
company_name_en: INTERTEK GROUP PLC
market_cap_usd_mm: 8421.85
market_cap_gbp_mm: 6631.38
exchange: LSE
country: GB
sector_code: "502050"
market_tier: MAINMARKET
score: 28.0
convergence_bonus: 0
score_total: 28.0
convergence_strategy_count: 1
status: pending_deep_dive
thesis_direction: long
translation_confidence: n/a
first_signal_date: 2026-04-16
last_updated: 2026-04-16
primary_catalyst_date: indefinite
cross_listed_on: []
related_signal_ids: ["d0175731827b3fbeca24c49039a1425d", "52c36d90dd51433b1d2caffde1fa6564"]
signal_type: takeover_possible_offer
signal_category: takeover
scanner: lse_rns
---

# Intertek Group (ITRK.XLON) — Response to Possible Offer by EQT

> **STATUS: pending_deep_dive.** This is an operational-skill stub. The deep-dives skill should flesh out every section below on its next run. Do not act on this stub alone.

## TL;DR (3 sentences max)

On 16 Apr 2026 Intertek issued a Rule 2.4-style "Statement re possible offer" at 11:37 UTC, followed by "Response to possible offer announcement by EQT" at 13:39 UTC — confirming EQT (the Swedish PE firm) is the named potential bidder for the UK-listed testing/inspection/certification group. Scanner classifies as `takeover_possible_offer`, score 28.0 (signal_strength 4, catalyst_clarity 4, info_asymmetry 2, risk_reward 3, edge_decay 3, liquidity 4, catalyst_timeline 3) — the info_asymmetry cap reflects that LSE takeover announcements are widely watched. This candidate is a stub pending human-in-the-loop deep-dive — thesis, steelman, kill conditions, and catalyst dates have not yet been researched.

## Source signal(s)

- **Signal 1 (11:37 UTC)** — https://www.investegate.co.uk/announcement/rns/intertek-group--itrk/statement-re-possible-offer/9524033 — 2026-04-16 11:37 UTC — RNS — [verified] Intertek's initial statement confirming a possible offer; [inferred] follows a prior undisclosed approach, triggering Rule 2.4 obligation once media or leak forced disclosure.
- **Signal 2 (13:39 UTC)** — https://www.investegate.co.uk/announcement/rns/intertek-group--itrk/response-to-possible-offer-announcement-by-eqt/9524268 — 2026-04-16 13:39 UTC — RNS — [verified] Follow-up naming EQT as the potential offeror; [inferred] EQT made a Rule 2.4 announcement from its side between 11:37 and 13:39, and Intertek is responding on the record.

Both signals merged into single candidate per dedup rule (same issuer_figi + same signal_type + same source_date). `related_signal_ids` captures the two source_content_hashes. No cross-listing signals observed; ITRK trades primarily on LSE Main Market.

## Translation notes

n/a — English-language source.

## Company context (to be completed by deep-dive)

- Market cap (USD): $8,421.85mm; GBP: £6,631.38mm — **FTSE 100 large-cap**, not a small/mid opportunity.
- ISIN: GB0031638363 — UK-domiciled, GBP-denominated.
- Sector code: 502050 (per LSE classification) — Professional Services (testing, inspection, certification).
- Market tier: MAINMARKET.
- Trading status: N (normal) as of scanner run. TODO: confirm whether trading halted or paused intraday post-announcement.
- Business: Intertek is a global quality-assurance provider (testing/inspection/certification; TIC industry). Primary peers: SGS (Swiss-listed), Bureau Veritas (French-listed), Eurofins.
- Recent price action: TODO — deep-dive fetch 30/90-day, plus intraday 16 Apr 2026 reaction.
- Cross-listings: TODO — verify whether Intertek has any ADR or foreign-listing equivalents.
- Institutional ownership: TODO — FTSE 100 so expect 70%+ institutional; check recent 13G-style TR-1 filings for build-up.

## Thesis statement (to be completed by deep-dive)

**Pending.** Scanner rubric pre-assigns `thesis_direction: long` for takeover_possible_offer classification on the target side. But note: a Rule 2.4 "possible offer" is NOT yet a firm offer and carries higher completion risk than a Rule 2.7 firm-offer candidate. Deep-dive must verify:

- Has EQT issued a written approach letter, or is this a preliminary discussion?
- What is the PUSU deadline (put-up-or-shut-up, typically 28 days from the Rule 2.4 date under the UK Takeover Code)?
- Price indications in either announcement — typical range as a premium to undisturbed price.
- Competing bidder potential — SGS, Apollo, KKR, Bridgepoint have all historically circled the TIC space.
- Strategic rationale — EQT's existing portfolio overlap; possible IDEX/Certara-style buy-and-build play.
- Takeover Panel treatment — whether the Rule 2.6(a) 28-day clock has started.

## Steelman of the opposite view

**Pending deep-dive.** Possible angles to research:

- **Rule 2.4 walk-away:** EQT announces it will not proceed. Historical base rate for 2.4 → 2.7 conversion in UK large-cap is ~55%. 45% fail to convert, so ITRK drifts back to undisturbed price; recent premium gives up.
- **Valuation ceiling:** ITRK trades at a mature multiple (~20x FCF historically); PE may be priced out once due-diligence confirms TIC margin compression from competition with SGS and Bureau Veritas.
- **Financing / syndication risk:** At ~£6.6bn enterprise value this is a very large PE deal; financing markets may not support a 30%+ premium. Some PE ramps go hostile and then collapse on debt-market pushback.
- **Board rejection:** Intertek board may determine EQT's proposal undervalues the company and reject further engagement. Possible but Rule 2.4 naming by the target usually indicates prior preliminary discussions were not hostile.
- **Regulatory complexity:** UK CMA, EU competition, potentially US HSR — TIC industry is concentrated and EQT likely does not have a competing TIC business, so regulatory is not a primary risk but must be mapped.

## Web research layer (mandatory — pending)

**Required before promoting from pending_deep_dive → active:**

- Financial press reporting on EQT approach — Bloomberg/FT/Reuters/Sky News. When did leak hit? Was 11:37 Intertek filing forced by media report?
- Analyst immediate reaction — Peel Hunt, Jefferies, UBS, Morgan Stanley views on standalone vs offer value.
- EQT historical large-cap UK take-privates (Dechra, Waystar) — conversion rate, timeline, typical premium.
- Shareholder register: top-10 holders, index-fund stakes (FTSE 100 passive holders), any activist-style accumulation in prior 90 days.
- CEO/Chair recent commentary — has Intertek signalled strategic dissatisfaction publicly?
- TIC industry consolidation context — SGS has been discussed as a merger counterparty historically.

## Kill conditions (explicit — to be refined)

- **Kill 1:** EQT Rule 2.8 lapse announcement ("EQT confirms it will not proceed with an offer"). Observable on RNS within 28 days of 16 Apr 2026 (PUSU deadline approx 2026-05-14 unless extended).
- **Kill 2:** Intertek Rule 2.4 update stating board has terminated discussions and no agreed terms can be reached. Observable on RNS.
- **Kill 3:** Price indication disclosed in a subsequent announcement falls below the undisturbed price plus ≤15% premium, below typical hostile-protection thresholds — market re-rates the "deal" downward.
- **Kill 4:** Competing bidder emerges at higher terms → switch-bid risk. Whether this is a kill or an upside depends on position sizing and entry price (arbitrage vs directional).
- **(refine with specific price thresholds after deep-dive retrieves undisturbed price and any indicative offer value)**

## Catalyst map (skeleton — deep-dive to fill dates)

| Event | Date/Window | Entry trigger | Exit trigger |
|-------|-------------|---------------|--------------|
| EQT Rule 2.4 announcement | 2026-04-16 (completed) | N/A | N/A |
| Intertek Rule 2.4 response | 2026-04-16 13:39 UTC (completed) | Entry on reopen | — |
| PUSU deadline (default 28d) | ~2026-05-14 | — | Kill 1 trigger if lapse |
| Potential PUSU extension (Takeover Panel permission) | Extends to a further 28d window | — | — |
| Rule 2.7 firm offer (if it converts) | T+28 to T+56 | Re-underwrite on firm terms | — |
| Scheme / takeover offer document | T+28 post-firm | — | — |
| Shareholder meeting / court sanction | T+90 to T+120 post-firm | — | — |

## Position sizing note

Satellite (1–3%). Possible-offer (Rule 2.4) candidates have a materially lower conversion rate than firm-offer (Rule 2.7) — ~55% historical base rate for UK large-cap 2.4 → 2.7. Size **smaller than a Rule 2.7 arbitrage trade would warrant**, and manage explicit PUSU-deadline risk (Kill 1). ITRK liquidity is excellent (FTSE 100, £6.6bn mcap), so position sizing is not liquidity-constrained.

## Source traceability

- https://www.investegate.co.uk/announcement/rns/intertek-group--itrk/statement-re-possible-offer/9524033 — retrieved 2026-04-16 via lse_rns scanner (pipeline_runner run, window=1) — logged as signal_id d0175731827b3fbeca24c49039a1425d, hash 09dd19669bc360c67281adb3
- https://www.investegate.co.uk/announcement/rns/intertek-group--itrk/response-to-possible-offer-announcement-by-eqt/9524268 — retrieved 2026-04-16 via lse_rns scanner — logged as signal_id 52c36d90dd51433b1d2caffde1fa6564, hash df26158b4c542c0a9bae4555
- OpenFIGI resolution: ticker=ITRK mic=XLON → issuer_figi=BBG000D9L3W0 (cached 2026-04-16)
- LSE alldata: ISIN GB0031638363, SEDOL 3163836, market_cap £6,631.38mm / $8,421.85mm USD (retrieved 2026-04-16)
