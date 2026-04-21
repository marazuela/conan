---
ticker_local: "1878"
mic: XTKS
ticker_plus_mic: 1878.XTKS
isin: null
figi: BBG000BNKQW1
issuer_figi: BBG000BNKQL3
company_name_local: 大東建
company_name_en: "Daito Trust Construction Co., Ltd."
market_cap_usd_mm: 7158
exchange: TDnet
country: JP
score: 35.0
convergence_bonus: 0
score_total: 35.0
convergence_strategy_count: 1
status: pending_deep_dive
thesis_direction: long
translation_confidence: 0.92
first_signal_date: 2026-04-16
last_updated: 2026-04-16
primary_catalyst_date: indefinite
cross_listed_on: []
related_signal_ids: []
signal_type: tender_offer
signal_category: takeover
scanner: tdnet
---

# Daito Trust Construction Co., Ltd. (1878.XTKS) — Correction to Tender Offer Filing for Global Co (3271)

> **STATUS: pending_deep_dive.** Automated-scanner stub. Do not act on this alone.

## TL;DR

On 2026-04-16 at 15:00 JST, Daito Trust Construction (1878.XTKS, mcap ≈ $7.16B USD) filed on TDnet a correction (訂正) to a previously-submitted 公開買付届出書 (public tender offer registration statement) relating to its public tender offer for **THE Global Co., Ltd. (3271)**. The filing also includes a corrected commencement public notice (開始公告の訂正). Scanner classifies this as `tender_offer`, `strength_estimate=5`, `thesis_direction=long` (buyer-side, deal proceeding), `translation_confidence=0.92` (TOB pattern unambiguous). Score 35.0, immediate-route.

This is the **buyer (bidder) side** of a Japanese tender offer — 1878 Daito Trust is acquiring 3271 THE Global. The target (3271) is the more interesting trade; this signal was emitted on the bidder side because 1878 is the filing entity on TDnet for this document. Cross-check whether 3271 also has an active filing in the signal log and whether the convergence engine should have merged these.

## Source signal

- **Primary** — https://www.release.tdnet.info/inbs/140120260415505074.pdf — 2026-04-16 15:00 JST — TDnet — headline (JP): "（訂正）公開買付届出書の訂正届出書提出に伴う「株式会社THEグローバル社（証券コード3271）に対する公開買付けの開始に関するお知らせ」及び開始公告の訂正"
- Translated headline (gloss): "(Correction) Correction of 'Notice regarding commencement of tender offer for THE Global Co., Ltd. (Securities Code 3271)' and the commencement public notice, in connection with submission of amended tender offer registration statement"

## Translation notes

Japanese source. Key terms:
- **公開買付届出書 (Kōkai-kaitsuke todokedesho)** — Tender Offer Registration Statement (filed with FSA).
- **訂正届出書 (Teisei todokedesho)** — Amendment / Correction Registration Statement.
- **開始公告 (Kaishi kōkoku)** — Commencement Public Notice (the legally-required newspaper / public notice that opens the tender window).

Because this is a **correction**, deep-dive must read the PDF to determine **what changed**. The three material possibilities, in order of thesis-relevance:

1. **Price change** — offer-per-share bumped up (bullish target; tightens spread) or down (bearish target; break risk).
2. **Period extension** — window lengthened (acceptance struggling or additional regulatory time needed) or shortened.
3. **Administrative / documentary correction** — error in the registration statement being fixed, no economic change. Most common and benign.

Translation confidence 0.92 is adequate to identify the filing type but does not read offer-price numbers — those must come from deep-dive PDF read.

## Company context

- **Bidder (1878 Daito Trust Construction):** One of Japan's largest residential subcontracting / leasing-management companies. Builds and manages apartment / rental housing on leased land. Mcap ≈ ¥1.14T / $7.16B USD. TSE Prime.
- **Target (3271 THE Global Co., Ltd.):** Real-estate developer. Substantially smaller than 1878. Deep-dive must pull mcap, price-to-offer spread, major shareholders, and whether 1878 is already a holder.
- **Strategic rationale (hypothesis):** Roll-up of a smaller residential-developer peer into Daito's management-and-leasing platform. Exact rationale — whether this is a growth acquisition, a defensive move, or a rescue — must come from the primary announcement made when the offer originally commenced (pre-this-correction).
- Daito Trust is well-covered by Japanese sell-side; Global (3271) is thinly covered.

## Thesis statement (to be completed by deep-dive)

**Pending.** Scanner pre-tagged `long` because the filing supports the deal continuing (corrections usually mean proceeding, not abandoning). If the correction is:
- a **price bump** → target (3271) tender-arb is tight and de-risked; bidder (1878) may be dinged on overpayment concern but typically mildly.
- a **period extension** → target spread may widen on acceptance doubt; neutral to bidder.
- **administrative only** → thesis unchanged from the original offer commencement; target continues to trade near offer price.

Primary trade, if any, is on the **target (3271)**, not the bidder (1878). This candidate stub is emitted on the bidder side because of the filing-entity structure on TDnet.

## Steelman of the opposite view

**Pending deep-dive.** Possible bear angles:

- **Correction is a price cut** — deal re-negotiated downward, target falls to new offer; tender-arb reverses.
- **Correction is a withdrawal of a material representation** — e.g., financing commitment weakened, board-recommendation change, or antitrust concern introduced. Materially raises break risk.
- **FSA inquiry correction** — filing was flagged for disclosure deficiency; suggests scrutiny of the bidder's process. Low-probability but tail-risk.
- **Offering extension because threshold not met** — acceptance rate below minimum condition; if minimum is structural (e.g., 2/3), offer may lapse.
- **Concurrent bidder** — low probability in Japanese TOBs (white knight response is structural in the code, but rare for mid-cap real-estate), but must check.

## Web research layer (mandatory — pending)

- Nikkei / Reuters coverage of Daito Trust / THE Global tender offer (since original commencement announcement — pull date from 3271 TDnet filings).
- 3271 THE Global recent trading versus offer price — spread, volume, open-interest.
- Shareholder response — any 5%-holder blocks, dissenting shareholders, class-action filings.
- 3271 board recommendation — accept / oppose / neutral; whether fairness opinion was filed.
- Comparable Japanese residential-developer TOBs (Starts Corp 2018, Haseko precedents) for premium and acceptance-rate benchmarks.

## Kill conditions

- **Kill 1:** Correction document contains a **price reduction**. Target-side thesis flips; exit tender-arb immediately.
- **Kill 2:** Correction reveals a **material adverse condition** (financing gap, regulator objection). Break-risk-weighted probability shifts > 20 pts; cut position.
- **Kill 3:** Acceptance at minimum-threshold deadline is short by > 10 percentage points; deal likely lapses.
- **Kill 4:** Daito Trust (1878) itself issues a separate TOB-withdrawal filing (撤回) — unambiguous kill.
- **(refine with specific JPY / %-threshold numbers during deep-dive)**

## Catalyst map (skeleton)

| Event | Date/Window | Entry trigger | Exit trigger |
|-------|-------------|---------------|--------------|
| Correction filing effective | 2026-04-16 | N/A | - |
| Market reaction (spread re-pricing on 3271) | 2026-04-17 open | - | - |
| Acceptance period close (per amended terms) | TBD — read from PDF | - | Settle to cash at offer price |
| Payment / settlement | T+30 post-close (Japanese TOB typical) | - | - |

## Position sizing

Satellite-arbitrage (2-5%) if entering target-side (3271). Bidder-side position (1878) is not the natural trade — it's a mcap-$7B large-cap whose multiple is set by its core leasing-management business, not by the modest 3271 acquisition. Tender-arb on 3271 sized to acceptable annualized IRR given days-to-close from revised acceptance-window.

## Source traceability

- https://www.release.tdnet.info/inbs/140120260415505074.pdf — retrieved 2026-04-16 via tdnet scanner (scan_date 2026-04-16)
- signal_id: be5a3d83573287c3395b14e95a798ba8
- source_content_hash: 6949cf4d8a82e20e801b8706
- OpenFIGI: ticker=1878 mic=XTKS → figi=BBG000BNKQW1, issuer_figi=BBG000BNKQL3
- Market cap: enriched via jpx_market_cap (1878.T) → ≈ $7.16B USD
- No related_signal_ids — scanner did not merge with a 3271-side filing this cycle. Deep-dive should check signal_log.json for a 3271 filing under signal_type=tender_offer and, if present, log D-001/D-004 manual-link.
