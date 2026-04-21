---
ticker_local: PTSB
mic: XLON
ticker_plus_mic: PTSB.XLON
isin: IE00BWB8X525
sedol: BVGGZK3
figi: BBG000DJ30N4
issuer_figi: BBG000DJ1YL4
company_name_local: Permanent TSB Group Holdings (CDI)
company_name_en: PERMANENT TSB GROUP HOLDINGS PLC
market_cap_usd_mm: 1764.2
market_cap_gbp_mm: 1389.13
exchange: LSE
country: GB
sector_code: "301010"
market_tier: MAINMARKET
score: 33.0
convergence_bonus: 0
score_total: 33.0
convergence_strategy_count: 1
status: pending_deep_dive
thesis_direction: long
translation_confidence: n/a
first_signal_date: 2026-04-14
last_updated: 2026-04-14
primary_catalyst_date: indefinite
cross_listed_on: []
related_signal_ids: ["ddc0441969c02c688b9366ef0acaeb7c"]
signal_type: takeover_firm_offer
signal_category: takeover
scanner: lse_rns
---

# Permanent TSB Group Holdings (PTSB.XLON) — BAWAG Recommended Cash Offer

> **STATUS: pending_deep_dive.** This is an operational-skill stub. The deep-dives skill should flesh out every section below on its next run. Do not act on this stub alone.

## TL;DR (3 sentences max)

On 14 Apr 2026 BAWAG announced a recommended cash offer for Permanent TSB Group Holdings, triggering a Rule 2.7 firm-offer RNS. The scanner classifies this as a takeover_firm_offer with score 33 (dimensions: signal_strength 5, catalyst_clarity 5, catalyst_timeline 5; capped by info_asymmetry 2 because LSE takeovers are widely watched). This candidate is a stub pending human-in-the-loop deep-dive work — thesis, steelman, and kill conditions have not yet been researched.

## Source signal(s)

- **Signal 1** — https://www.investegate.co.uk/announcement/rns/permanent-tsb-group-holdings-cdi---ptsb/recommended-cash-offer-for-permanent-tsb/9519415 — 2026-04-14 11:05 UTC — RNS Rule 2.7 — [verified] BAWAG is making a recommended cash offer for PTSB; [inferred] headline is authoritative regulatory filing.
- **Signal 2** (merged sibling) — https://www.investegate.co.uk/announcement/rns/permanent-tsb-group-holdings-cdi---ptsb/bawag-recommended-offer-for-ptsb-/9519514 — 2026-04-14 11:50 UTC — RNS — same event, offeror-side filing.

Both signals merged into single candidate per dedup rule (same issuer_figi + same signal_type + same source_date).

## Translation notes

n/a — English-language source.

## Company context (to be completed by deep-dive)

- Market cap (USD): $1,764.2mm; GBP: £1,389.13mm
- ISIN: IE00BWB8X525 — Irish ISIN; issuer is Irish-domiciled trading via CDI on LSE.
- Sector code: 301010 (per LSE classification)
- Market tier: MAINMARKET
- Trading status: N (normal) as of scanner run.
- Recent price action: TODO — deep-dive fetch 30/90-day.
- Cross-listings: TODO — verify Dublin/Euronext listing; CDI status suggests underlying shares trade on Euronext Dublin.
- Institutional ownership: TODO.

## Thesis statement (to be completed by deep-dive)

**Pending.** Scanner rubric pre-assigns `thesis_direction: long` for Rule-2.7-firm-offer classification (target equity tends to converge toward offer price once scheme of arrangement / court sanction path is set). Deep-dive must verify:
- Offer terms (price per share, cash vs mixed consideration)
- Premium to undisturbed price
- Acceptance condition threshold and irrevocables percentage
- Regulatory approvals required (CBI / ECB for a bank target, Irish Takeover Panel, EU merger control)
- Break fee and material-adverse-change carve-outs

## Steelman of the opposite view

**Pending deep-dive.** Possible angles to research:
- Competing bidder emerges and the BAWAG offer is jumped (positive for PTSB, but risky if acceptance threshold fails)
- Regulatory block — Irish state's residual stake in PTSB may give political veto
- Deal breaks on financing / conditions — PTSB falls back to undisturbed range

## Web research layer (mandatory — pending)

**Required before promoting from pending_deep_dive → active:**
- Irish press coverage of BAWAG / PTSB deal history (was there prior approach?)
- Analyst price targets pre- vs post-announcement
- Irish state (via Minister for Finance) ownership / voting stake and stated position
- BAWAG strategic rationale: is this a one-off or part of Irish market consolidation?
- CBI / ECB preliminary indications on a non-Irish EU bank buying an Irish bank

## Kill conditions (explicit — to be refined)

- **Kill 1:** Offer is withdrawn. Observable in LSE RNS as Rule 2.8 lapse announcement.
- **Kill 2:** Irish state publicly vetoes or sets conditions BAWAG cannot meet. Observable in Department of Finance press release.
- **Kill 3:** Competing higher offer emerges, making BAWAG's terms uncompetitive. Observable in RNS.
- **Kill 4:** Regulatory block by CBI/ECB/EU. Observable in formal refusal publication.
- **(refine with specific price thresholds during deep-dive)**

## Catalyst map (skeleton — deep-dive to fill dates)

| Event | Date/Window | Entry trigger | Exit trigger |
|-------|-------------|---------------|--------------|
| Scheme document publication | T+28 days typical under Irish Takeover Rules | N/A | N/A |
| Shareholder meeting / scheme approval | T+60 to T+90 | - | - |
| Court sanction hearing | T+90 to T+120 | - | - |
| Effective date | T+100 to T+130 | - | - |

## Position sizing note

Satellite (2–5%). Arbitrage-style spread plays are liquidity-sensitive; check average daily volume in CDI form before sizing. Bank-sector deals have elongated regulatory tails — position to hold to close, not to pre-close exit.

## Source traceability

- https://www.investegate.co.uk/announcement/rns/permanent-tsb-group-holdings-cdi---ptsb/recommended-cash-offer-for-permanent-tsb/9519415 — retrieved 2026-04-14 14:52 UTC via lse_rns scanner (pipeline_runner run)
- https://www.investegate.co.uk/announcement/rns/permanent-tsb-group-holdings-cdi---ptsb/bawag-recommended-offer-for-ptsb-/9519514 — retrieved 2026-04-14 14:52 UTC (merged sibling)
- OpenFIGI resolution: ticker=PTSB mic=XLON → figi=BBG000DJ30N4 issuer_figi=BBG000DJ1YL4 (cached 2026-04-14)
- LSE alldata: ISIN IE00BWB8X525, SEDOL BVGGZK3, market_cap £1,389.13mm (retrieved 2026-04-14 14:34 UTC)
