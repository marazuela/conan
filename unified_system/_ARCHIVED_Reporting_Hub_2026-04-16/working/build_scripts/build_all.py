"""Driver: build 16 deep-dive PDFs."""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from pdf_gen import build_pdf

OUT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "Candidates", "deep_dives", "pdf"
))
os.makedirs(OUT, exist_ok=True)

TODAY = "2026-04-15"

UNV = "[unverified]"

# -------------------------------------------------------------------
# Shared merger-arb catalyst skeleton for Japan tender offers
# -------------------------------------------------------------------
def tender_catalyst(settlement_done_date: str, offer_close: str, bidder: str):
    return [
        ["Tender offer close", offer_close, "Acceptance > minimum threshold", "Spread closes"],
        ["Settlement / payment", settlement_done_date,
         "Monies paid; shares delisted (if 100%)", "Exit complete"],
        ["Antitrust / JFTC clearance", "rolling", "Approval announced", "Risk premium compresses"],
        ["Bidder financing confirmation", "pre-close",
         "Senior debt locked / cash available", "Deal-break risk falls"],
        ["Competing bid", "0\u201330d", "Interloper emerges", "Spread widens / trade-out"],
    ]


def tender_kill():
    return [
        ["K1: competing superior offer", "Interloper bid > 5% above current offer",
         "TDnet disclosure"],
        ["K2: regulatory block", "JFTC or foreign-antitrust objection",
         "JFTC public register"],
        ["K3: financing pulled", "Bidder announces inability to fund",
         "Bidder IR release"],
        ["K4: minimum threshold missed", "Acceptances below stated floor",
         "Tender-offer result filing"],
        ["K5: non-compliance / revised terms",
         "Offer revised down or extended repeatedly",
         "TDnet extension notice"],
    ]


# -------------------------------------------------------------------
# 1. Toa Road 1882 — litigation/regulatory
# -------------------------------------------------------------------
toa_road = dict(
    title="Toa Road Corporation \u2014 1882.XTKS",
    subtitle="Litigation / regulatory disclosure \u00b7 deep-dive \u00b7 " + TODAY,
    header_kv=[
        ("Ticker", "1882.XTKS"),
        ("Issuer FIGI", "BBG000BKP0P7"),
        ("Market cap", "USD 485m"),
        ("Signal date", "2026-04-13"),
        ("Signal type", "litigation_regulatory"),
        ("Direction", "UNKNOWN (reduced from short \u2014 see below)"),
        ("Score", "29 / 42.5 (immediate route)"),
        ("Deep-dive date", TODAY),
    ],
    tldr=(
        "Toa Road Corp. (road paving / civil engineering contractor, TSE Prime Market) "
        "filed a litigation/regulatory disclosure on 2026-04-13. Public search in the "
        "6-month window returned no specific cartel, criminal, or class-action finding; "
        "the exact nature of the filing could not be verified outside primary-source "
        "Japanese text. Japanese road-paving sector has a multi-year JFTC cartel history "
        "(2023 \"road-pavement bid-rigging\" orders against peers), so a cartel-related "
        "filing here is a priors-aligned risk. Direction is downgraded from the scanner's "
        "provisional SHORT to UNKNOWN until the filing can be translated at \u22650.85 confidence."
    ),
    company_context=[
        "Toa Road is a mid-cap Japanese road paving / civil engineering contractor, listed on TSE Prime (ticker 1882).",
        "Market cap: USD 485m (small-to-mid cap Japanese construction).",
        "Sector: Construction / civil engineering; revenue driven by highway, airport-apron, and municipal paving contracts \u2014 cyclical and public-works-dependent.",
        "Industry context: Japan Fair Trade Commission (JFTC) opened / concluded bid-rigging cases against road-paving contractors in 2023\u20132025; settlement precedents exist at peer level. " + UNV + " for Toa Road\u2019s direct involvement.",
    ],
    release_contents=[
        "Filing type: TDnet litigation/regulatory disclosure (category matches scanner\u2019s litigation_regulatory taxonomy).",
        "Primary-source Japanese PDF: https://www.release.tdnet.info/inbs/140120260413502990.pdf \u2014 content not translated in this deep-dive at \u22650.85 confidence. " + UNV,
        "No Western-press coverage in 6-month search window connecting 1882 to a specific lawsuit, cartel order, or administrative sanction.",
        "Signal-strength tag from scanner is 3/4; translation_confidence reported < 0.85 \u2014 triggers the D-002 direction-honesty cap (thesis_direction forced to unknown).",
    ],
    thesis=(
        "<b>Declared as UNKNOWN per D-002.</b> The disclosure exists, but the underlying event is "
        "not verifiable in English-language search. Without confirmed scope (fine, class size, "
        "criminal vs. civil, counter-party), a directional short/long bet is unsupported. "
        "The trade here is NOT to short on headline risk alone \u2014 it is to set a watch with "
        "entry triggered only after translation lifts confidence \u22650.85 and the monetary scope is known."
    ),
    catalyst_rows=[
        ["Primary disclosure (1882 TDnet)", "2026-04-13", "n/a \u2014 signal source", "n/a"],
        ["Follow-up disclosure (scope / monetary)", "0\u201330d", "Fine or judgment amount disclosed", "Score re-evaluated"],
        ["Next quarterly earnings", UNV + " \u2014 check IR cal.", "Litigation-reserve movement", "Reserve > 5% equity \u2192 short"],
        ["JFTC public register update", "rolling", "1882 named in bid-rigging order", "Validates short thesis"],
        ["Peer translation (Kajima, Taisei, Obayashi)", "rolling", "Peer civil-infra cartel coverage", "Sector contagion read"],
    ],
    steelman=[
        "Translation-confidence cap is working as designed: many Japanese litigation disclosures are routine (vendor contract disputes, labour cases) and non-material even at face value.",
        "Japanese construction-sector litigation reserves are typically well-disclosed in the half-year report; if the 1H26 reserve does not move, the market will treat this as non-event.",
        "Small civil suits (e.g., labour, site-safety) are disclosed with the same TDnet category as material antitrust matters \u2014 without translation, the scanner cannot distinguish, and defaulting to short is unsafe.",
        "Toa Road is profitable and dividend-paying; short borrow on an 485m-mcap Japanese name is costly and liquidity-constrained, so entry friction is high.",
    ],
    kill_rows=[
        ["K1: translation reveals trivial matter", "Confidence \u22650.85 + matter < 1% equity", "Primary-source translation"],
        ["K2: reserve unchanged at 1H26", "Litigation reserve flat YoY", "1H26 earnings PDF"],
        ["K3: JFTC public register clean", "1882 not named in any active case", "JFTC public docket"],
        ["K4: peer-neutral market reaction", "Sector index flat despite disclosure", "TOPIX-17 Construction"],
        ["K5: borrow / liquidity block", "Short fee > 5% or ADV < USD 1m/day", "Broker stock-loan desk"],
    ],
    peer_comparables=[
        "JFTC 2023 bid-rigging order against road-paving contractors \u2014 specific defendants and fines differ by contractor; relevant for sector prior.",
        "Kajima / Taisei 2021 civil-infrastructure cartel findings \u2014 settlements were single-digit % of equity; market impact transient.",
        UNV + " \u2014 no 1882-specific peer precedent surfaced in search.",
    ],
    position_sizing=(
        "No position recommended at this time. If translation confirms material civil-antitrust exposure "
        "(fine > 2\u20133% market cap), the trade is a short-biased pair vs. TOPIX-17 Construction; cap gross "
        "at 0.3\u20130.5% NAV given borrow friction and position concentration risk in small-caps."
    ),
    sources=[
        "Primary: TDnet PDF https://www.release.tdnet.info/inbs/140120260413502990.pdf \u2014 not translated in-run.",
        "JFTC cartel history: public JFTC orders & press releases (context only; 1882 inclusion " + UNV + ").",
        "Signal ID (scanner): see 1882_XTKS_toa-road-oration-litigation-regulatory.md in non_us_discovery_system/candidates/.",
    ],
)

# -------------------------------------------------------------------
# 2. Yomeishu Seizo 2540 \u2014 tender offer (Reno / Murakami)
# -------------------------------------------------------------------
yomeishu = dict(
    title="Yomeishu Seizo Co., Ltd. \u2014 2540.XTKS",
    subtitle="Tender offer (Reno / Murakami vehicle) \u00b7 deep-dive \u00b7 " + TODAY,
    header_kv=[
        ("Ticker", "2540.XTKS"),
        ("Issuer FIGI", "BBG000BCT6K3"),
        ("Market cap", "USD 351m"),
        ("Signal date", "2026-04-09"),
        ("Signal type", "tender_offer"),
        ("Direction", "LONG (merger-arb)"),
        ("Score", "35 / 42.5"),
        ("Deep-dive date", TODAY),
    ],
    tldr=(
        "Reno, Inc. (Yoshiaki Murakami activist vehicle) has executed a tender offer for Yomeishu "
        "Seizo at JPY 4,050/share (\u2248 USD 28m-equivalent of daily context; JPY ~28bn total spend). "
        "The tender completed in the 2026-02-25 \u2192 2026-04-08 window, with settlement commencing "
        "2026-04-15. A side-agreement confirms Tsumura & Co. will acquire the Yakuyo Yomeishu medical-"
        "liqueur brand for ~JPY 6.8bn post-delisting. This is a hard-arb setup with the legal "
        "control-change done; residual spread reflects settlement mechanics only. Recommendation: "
        "no new long entry at this stage (tender already settled); monitor for post-delisting "
        "brand-transfer closing and any minority-squeeze-out true-up risk."
    ),
    company_context=[
        "Yomeishu Seizo: Japan\u2019s flagship producer of Yakuyo Yomeishu (medicinal-herb liqueur) \u2014 iconic in the domestic OTC-medicinal category.",
        "Market cap: USD 351m; small-cap JP consumer / pharmaceutical hybrid.",
        "Activist history: Murakami vehicles (Reno, City Index Eleventh) have taken several Japanese consumer names private in 2022\u20132025.",
        "Tsumura & Co. is a major Kampo (traditional herbal medicine) producer \u2014 acquiring the Yakuyo Yomeishu brand is additive to its core OTC-herbal portfolio.",
    ],
    release_contents=[
        "Bidder: Reno, Inc. (Murakami-related vehicle).",
        "Offer price: JPY 4,050/share.",
        "Aggregate consideration: \u2248 JPY 28bn.",
        "Tender period: 2026-02-25 \u2013 2026-04-08; settlement window commenced 2026-04-15.",
        "Side transaction: Tsumura & Co. to acquire Yakuyo Yomeishu medicinal-liqueur brand post-delisting for \u2248 JPY 6.8bn.",
        "Premium vs. unaffected price: " + UNV + " \u2014 not explicitly cited in available English press; Murakami-vehicle bids typically 20\u201335% over 3-month VWAP in Japan precedents.",
    ],
    thesis=(
        "<b>Merger-arb LONG, conditional.</b> With the tender mechanically complete and settlement "
        "under way, the residual trade is a low-bps capture of any remaining spread vs. offer price, "
        "less borrow / financing / currency hedging. The more interesting read-through is the "
        "<i>Tsumura</i> post-delisting brand acquisition as an inferred data point on Kampo / herbal-OTC "
        "M&amp;A demand \u2014 Tsumura\u2019s own multiple is the better-liquidity expression of that theme."
    ),
    catalyst_rows=tender_catalyst("2026-04-15+ (in progress)", "2026-04-08 (closed)", "Reno (Murakami)"),
    steelman=[
        "Tender is already completed \u2014 there is no merger-arb entry here for a new long; remaining spread is noise.",
        "Minority squeeze-out (kabushiki-heigou / cash-out) price typically matches tender \u2014 but Japanese precedent occasionally sees a modest bump, a risk for a <i>short-the-spread</i> counterparty.",
        "If Tsumura walks from the brand acquisition, Reno\u2019s post-delisting restructuring economics weaken, but by then public-market participation is moot.",
        "Currency: Murakami-vehicle tenders are JPY-denominated; USD-funded arb books must hedge \u2014 adds friction worth 20\u201350 bps.",
    ],
    kill_rows=tender_kill(),
    peer_comparables=[
        "Reno / Murakami 2024 tender offer for Cosmos Pharmaceutical-related names \u2014 activist-to-control pattern.",
        "Other Japanese small-cap consumer de-listings at 20\u201335% premia to 3-month VWAP \u2014 establishes Murakami-bid distribution.",
        UNV + " for exact 2026 peer premium distribution.",
    ],
    position_sizing=(
        "Recommendation: no new arb entry (offer closed). If residual spread > 1.5% vs. offer price "
        "and minority-squeeze-out is still pending, sub-position at 0.25\u20130.5% NAV with 60-day carry; "
        "hedge JPY exposure to reduce to spread-only P&amp;L."
    ),
    sources=[
        "Globe and Mail / TipRanks \u2014 Reno launches tender offer for Yomeishu Seizo at JPY 4,050 (ref. from research).",
        "Primary: TDnet PDF https://www.release.tdnet.info/inbs/140120260408599998.pdf.",
        "Non_us stub: 2540_XTKS_yomeishu-seizo-co-ltd-tender-offer.md.",
    ],
)

# -------------------------------------------------------------------
# 3. Itochu-Shokuhin 2692 — parent Itochu take-private
# -------------------------------------------------------------------
itochu_shokuhin = dict(
    title="ITOCHU-SHOKUHIN Co., Ltd. \u2014 2692.XTKS",
    subtitle="Parent-subsidiary tender offer (ITOCHU 8001) \u00b7 deep-dive \u00b7 " + TODAY,
    header_kv=[
        ("Ticker", "2692.XTKS"),
        ("Issuer FIGI", "BBG000FRFVY1"),
        ("Market cap", "USD 1,032m"),
        ("Signal date", "2026-04-10"),
        ("Signal type", "tender_offer (parent consolidation)"),
        ("Direction", "LONG (merger-arb)"),
        ("Score", "35 / 42.5"),
        ("Deep-dive date", TODAY),
    ],
    tldr=(
        "ITOCHU Corp (8001, ~USD 88bn mcap) has launched a tender offer via a wholly-owned SPV "
        "(G.K. FMDI) for the 47.54% of ITOCHU-SHOKUHIN it doesn\u2019t already own, at JPY 13,000/share "
        "(aggregate spend JPY 78.4bn). Tender commenced 2026-02-26, closed 2026-04-09. This is a "
        "textbook Japanese parent-subsidiary take-private; ITOCHU was 52.46% owner pre-bid, 100% post. "
        "Recommendation: no new arb entry; tender is mechanically complete. Residual exposure is "
        "squeeze-out price risk (typically equal to tender; rare upside)."
    ),
    company_context=[
        "ITOCHU-SHOKUHIN: food-trading/distribution subsidiary of ITOCHU, focused on chilled and frozen food wholesale to Japanese retailers.",
        "Market cap: USD 1.03bn; 52.46% owned by ITOCHU Corp (8001) pre-bid.",
        "ITOCHU is one of Japan\u2019s five mega-trading-houses (s\u014dg\u014d sh\u014dsha); subsidiary tidying has been a visible capital-allocation theme since 2022 (Berkshire-backed policy of simplification).",
    ],
    release_contents=[
        "Bidder: ITOCHU Corp (8001) via G.K. FMDI (wholly-owned SPV).",
        "Offer price: JPY 13,000/share.",
        "Aggregate consideration: JPY 78.4bn for the 47.54% minority.",
        "Minimum acceptance: 1.8m shares (2/3 voting threshold).",
        "Tender period: 2026-02-26 \u2192 2026-04-09.",
        "Post-close: ITOCHU-SHOKUHIN will be 100% owned and delisted.",
        "Premium: " + UNV + " vs. unaffected close; parent-subsidiary take-privates in Japan 2024\u20132026 cluster around 25\u201335% above 3-month VWAP.",
    ],
    thesis=(
        "<b>Merger-arb LONG, settled.</b> Parent-sub consolidations with 52%+ pre-bid ownership and "
        "voting approval have ~100% historical completion. The trade is carry of residual spread "
        "until squeeze-out; new entries at this stage earn only settlement-mechanics bps. The "
        "read-through value is as a 2026 data point for ITOCHU\u2019s simplification cadence \u2014 useful in "
        "triangulating timing of future subsidiary consolidations (e.g., ITOCHU Enex, ITOCHU Techno-Solutions)."
    ),
    catalyst_rows=tender_catalyst("2026-04-16 settlement",
                                  "2026-04-09 (closed)",
                                  "ITOCHU / G.K. FMDI"),
    steelman=[
        "Parent bought at the price <i>it</i> chose \u2014 there\u2019s no external check; minority squeeze-out is formally legal but the fairness opinion may be litigated (rare but non-zero in JP).",
        "Global merger-arb desks have priced this at 0\u201330bps for months \u2014 no alpha left for late entrants.",
        "Regulatory friction is low: JFTC rarely blocks wholly-owned subsidiary reconsolidations.",
        "Minority lawsuit risk is the only real optionality; success rate in Japan is low, capping upside.",
    ],
    kill_rows=tender_kill(),
    peer_comparables=[
        "Sumitomo Metal Mining / peers \u2014 2024 parent-sub consolidations; 25\u201335% premium range.",
        "Mitsui & Co. minority buy-ins in trading-house subsidiaries (2023\u20132024).",
        "ITOCHU\u2019s own 2022 buy-in of ITOCHU Techno-Solutions (if applicable) is a proxy for execution cadence. " + UNV,
    ],
    position_sizing=(
        "No new entry. If residual squeeze-out spread > 1.0%, sub-position at 0.5\u20131.0% NAV with "
        "JPY hedge. Position decays with settlement; expected hold 30\u201360 days."
    ),
    sources=[
        "TradingView / Reuters 2026 \u2014 ITOCHU to launch tender for ITOCHU-SHOKUHIN at JPY 13,000/share (research).",
        "Primary: TDnet PDF https://www.release.tdnet.info/inbs/140120260410501548.pdf.",
        "Non_us stub: 2692_XTKS_itochu-shokuhin-co-ltd-tender-offer.md.",
    ],
)

# -------------------------------------------------------------------
# 4. Tsuruha Holdings 3391 — Aeon consolidation of drugstore group
# -------------------------------------------------------------------
tsuruha = dict(
    title="Tsuruha Holdings Inc. \u2014 3391.XTKS",
    subtitle="AEON Co. tender offer + open-market accumulation \u00b7 deep-dive \u00b7 " + TODAY,
    header_kv=[
        ("Ticker", "3391.XTKS"),
        ("Issuer FIGI", "BBG000JXTF38"),
        ("Market cap", "USD 5,812m"),
        ("Signal date", "2026-04-15"),
        ("Signal type", "tender_offer (parent consolidation, cross-listed via AEON)"),
        ("Direction", "LONG (merger-arb / control-change)"),
        ("Score", "35 / 42.5"),
        ("Deep-dive date", TODAY),
    ],
    tldr=(
        "AEON Co. (8267) completed a JPY 11,400/share tender (Dec 3 2025 \u2013 Jan 6 2026) plus open-market "
        "purchases through 2026-04-30 to reach 50.9% voting control of Tsuruha Holdings, capping a multi-"
        "year consolidation of Japan\u2019s largest drugstore group (post Welcia-Tsuruha merger effective "
        "2025-12-01). The 2026-04-15 TDnet filing is the control-secure confirmation. Tender premium was "
        "4.6% vs. 2025-04-11 unaffected close. Recommendation: control is now secured, minority "
        "squeeze-out is the next step; remaining arb is thin. Broader read-through: AEON is now Japan\u2019s "
        "dominant retail-pharmacy consolidator with \u22485,600 stores and >JPY 2trn revenue."
    ),
    company_context=[
        "Tsuruha Holdings: Japan\u2019s top-2 drugstore operator; merged with Welcia (AEON\u2019s pre-existing drugstore arm) effective 2025-12-01 via share-exchange (4.34 Welcia : 1 Tsuruha, resulting 51/49 Tsuruha/Welcia equity split).",
        "Market cap: USD 5.81bn.",
        "Parent: AEON Co. (8267), Japan\u2019s largest retailer (mega-cap USD 30bn).",
        "Strategic context: AEON has been accumulating Tsuruha since 2024, navigating activist opposition (Orbis Investments) to the Welcia-Tsuruha merger structure.",
    ],
    release_contents=[
        "Bidder: AEON Co. (8267).",
        "Primary tender: JPY 11,400/share, Dec 3 2025 \u2013 Jan 6 2026.",
        "Subsequent open-market accumulation: Jan 9 \u2013 Apr 30, 2026.",
        "Control threshold achieved: 50.9% voting by 2026-04-15.",
        "Tender premium: 4.6% over 2025-04-11 unaffected close (thin \u2014 reflects prior accumulation).",
        "Welcia delisted 2025-11-27; Welcia-Tsuruha share-exchange effective 2025-12-01.",
        "Counter-view on record: Orbis Investments publicly opposed the merger structure as undervaluing Tsuruha.",
    ],
    thesis=(
        "<b>Merger-arb LONG, late-stage.</b> Control is effectively done; the residual trade is a "
        "small spread to the inevitable squeeze-out at \u2264JPY 11,400. An interesting <i>asymmetric</i> "
        "angle: Orbis-style activists argued Tsuruha was worth materially more than the tender price, "
        "so a minority-holder lawsuit for fair-value at squeeze-out is a tail-scenario optional payoff. "
        "Base-case: no trade. Tail case (class-action true-up): sub-1% NAV lottery position."
    ),
    catalyst_rows=[
        ["Control secured (filing)", "2026-04-15", "AEON \u2265 50.9%", "Signal confirmed"],
        ["Minority squeeze-out", "0\u201390d", "Cash-out resolution at AGM", "Arb closes"],
        ["Orbis / minority lawsuit", "0\u2013180d", "Court filing vs. Tsuruha board", "Option-value unlock"],
        ["Welcia-Tsuruha integration synergies", "H2 2026+", "First combined quarterly", "Re-rating of AEON itself"],
        ["JFTC follow-up", "rolling", "Unexpected divestiture order", "Deal retreat"],
    ],
    steelman=[
        "Tender premium was only 4.6% \u2014 on an activist-opposed setup, fair-value litigation risk is non-trivial.",
        "Tsuruha\u2019s underlying business (drugstore chain with \u22653,500 stores) is among Japan\u2019s most defensible retail categories \u2014 a squeeze-out at tender price may under-compensate minorities.",
        "AEON\u2019s own share price reaction will be the better expression of the synergy thesis; Tsuruha arb is a low-return footnote.",
        "Orbis has historically litigated Japanese fair-value matters, so minority upside optionality is real but low-probability.",
    ],
    kill_rows=[
        ["K1: Squeeze-out completes cleanly at tender price", "JPY 11,400 cash-out executes", "AGM minutes / TDnet"],
        ["K2: Orbis lawsuit not filed in 180d", "No court docket", "Public court record"],
        ["K3: Welcia-Tsuruha synergies track plan", "Run-rate EBITDA synergies \u2265 disclosed plan", "AEON quarterly results"],
        ["K4: No JFTC remediation", "No divestiture order", "JFTC public register"],
        ["K5: Borrow cost > expected spread", "Stock-loan > 3% annualized", "Broker stock-loan desk"],
    ],
    peer_comparables=[
        "Welcia delisting 2025-11 \u2014 inside-group precedent at the same premium band.",
        "Seven &amp; i Holdings / Sogo-Seibu carve-out (2023) \u2014 retail-group restructuring at narrow premia.",
        "FamilyMart / UNY (2019\u20132020) Itochu-parent take-private \u2014 minority-litigation precedent. " + UNV + " on outcome relevance to 3391.",
    ],
    position_sizing=(
        "No new merger-arb entry \u2014 tender is done and premium is thin. Tail-risk lottery on "
        "fair-value lawsuit can be sized at 0.1\u20130.2% NAV if an Orbis-style plaintiff files within "
        "60 days. Core read-through: AEON (8267) is the better expression of the consolidation thesis."
    ),
    sources=[
        "BusinessWire / Orbis Statement 2025 \u2014 Orbis opposition to Tsuruha-Welcia merger / AEON tender.",
        "Primary: TDnet PDF https://www.release.tdnet.info/inbs/140120260415504841.pdf.",
        "Non_us stub: 3391_XTKS_tsuruha-holdings-tender-offer.md.",
    ],
)

# -------------------------------------------------------------------
# 5. Aica Kogyo 4206 — note: actually an India open offer (bidder side)
# -------------------------------------------------------------------
aica = dict(
    title="Aica Kogyo Company, Limited \u2014 4206.XTKS",
    subtitle="Outbound India open offer for Stylam Industries \u00b7 deep-dive \u00b7 " + TODAY,
    header_kv=[
        ("Ticker", "4206.XTKS"),
        ("Issuer FIGI", "BBG000BJG1K3"),
        ("Market cap", "USD 1,417m"),
        ("Signal date", "2026-04-13"),
        ("Signal type", "tender_offer (outbound bidder, mis-tagged in scanner)"),
        ("Direction", "NEUTRAL / SHORT-biased \u2014 scanner default flipped post-deep-dive"),
        ("Score", "35 / 42.5 (route downgraded post-deep-dive)"),
        ("Deep-dive date", TODAY),
    ],
    tldr=(
        "<b>The scanner mis-tagged direction.</b> Aica Kogyo is NOT the takeover target; it is the "
        "<i>bidder</i> in a mandatory open offer for 26% of Stylam Industries (India-listed decorative "
        "laminate manufacturer) at INR 2,250/share (\u2248 44.06m shares). This follows Aica\u2019s 27.12% "
        "promoter-stake acquisition in Stylam, which triggered the mandatory open offer under Indian "
        "securities law. <b>As the bidder, Aica faces the typical acquirer discount \u2014 capital "
        "deployment, integration risk, and FX translation exposure \u2014 not a merger-arb premium.</b> "
        "The provisional LONG must be reversed. Recommendation: remove from immediate-route; re-"
        "classify as watchlist (sentiment-only) at neutral or short bias."
    ),
    company_context=[
        "Aica Kogyo: TSE-listed decorative laminates / building-material chemicals maker (adhesives, resins, flooring).",
        "Market cap: USD 1.42bn.",
        "Strategy: has been adding foreign capacity \u2014 Stylam Industries acquisition extends Aica into India\u2019s high-growth decorative-laminates market (Stylam is Asia\u2019s largest single-location laminate factory).",
        "Open-offer manager: ICICI Securities Ltd.",
    ],
    release_contents=[
        "Transaction: Aica Kogyo bidding for 26% (44.06m shares) of Stylam Industries (NSE/BSE-listed).",
        "Offer price: INR 2,250/share (vs. Stylam market " + UNV + ").",
        "Trigger: mandatory open offer under SEBI SAST regulations, following Aica\u2019s 27.12% promoter-stake acquisition.",
        "Tender window: Apr 22 \u2013 May 6, 2026.",
        "Implied total outlay: \u2248 INR 99bn (USD ~1.2bn) at full take-up; materially large relative to Aica\u2019s USD 1.42bn market cap.",
    ],
    thesis=(
        "<b>Direction reversed to NEUTRAL / slight SHORT on the bidder.</b> Aica Kogyo is spending an "
        "outsized fraction of its market cap on a cross-border acquisition in a high-multiple sector "
        "(Indian decorative laminates). Historical bidder reaction: 1\u20134% drift lower in the 30 days "
        "post-announcement, plus risk premium for integration and INR/JPY hedging. Short entry is "
        "sub-scale absent material leverage or visible governance red-flag; default is no trade, "
        "reclassify candidate."
    ),
    catalyst_rows=[
        ["Open-offer window", "Apr 22 \u2013 May 6, 2026", "Acceptance data daily", "Completion confirmed"],
        ["SEBI clearance", "pre-open", "Regulator green-light", "Bidder risk compresses"],
        ["Aica FY26 earnings", UNV, "Guidance on Stylam accretion", "Thesis firms"],
        ["INR/JPY FX", "rolling", "INR down > 3% in window", "Accretion narrows (bidder negative)"],
        ["Stylam peer re-rating", "0\u201360d", "Peer laminate M&A spike", "Justifies bid price"],
    ],
    steelman=[
        "Stylam is a cash-flow accretive asset with best-in-class Asian laminate manufacturing footprint \u2014 synergy math could be EPS-positive from year 2.",
        "Aica\u2019s FY24\u201325 free cash flow supports the acquisition without material leverage increase. " + UNV,
        "Japan-to-India cross-border deals have underperformed historically, but decorative-laminates is a genuinely high-growth end market.",
        "Bidder stock drifts are transient; true risk is governance/integration over 2\u20133 year horizon.",
    ],
    kill_rows=[
        ["K1: acquisition abandoned", "SEBI blocks / promoters renege", "SEBI public notice"],
        ["K2: Aica accretion > 10% Y2", "FY28 guide shows clean EPS lift", "Earnings call"],
        ["K3: leverage spike", "Net-debt/EBITDA > 2.5x post-close", "Aica balance sheet"],
        ["K4: INR collapse", "INR/JPY > 3% adverse in 30d", "BoJ / RBI data"],
        ["K5: peer re-rating", "Stylam peers up > 15% in window", "India MCX / BSE"],
    ],
    peer_comparables=[
        "Japan-to-India M&A track record: Suzuki / Maruti-Suzuki is the positive exemplar; most other deals (SoftBank-Snapdeal, Asahi-Jhunjhunwala names) underperformed.",
        "Aica\u2019s own 2021\u20132024 overseas acquisitions \u2014 check integration track record. " + UNV,
        "Decorative laminates sector consolidation in India: Greenlam, Merino, Century Ply 2024\u20132025 multiples as reference.",
    ],
    position_sizing=(
        "No position on Aica as a long. If a short expression is wanted, 0.25\u20130.5% NAV with a "
        "60-day holding period, paired against TOPIX-17 Chemicals to isolate the bidder-drift effect."
    ),
    sources=[
        "Aica Kogyo / Stylam public SEBI filings (open offer letter).",
        "Primary: TDnet PDF https://www.release.tdnet.info/inbs/140120260413502738.pdf.",
        "Non_us stub: 4206_XTKS_aica-kogyo-company-limited-tender-offer.md.",
    ],
)

# -------------------------------------------------------------------
# 6. Aeon Fantasy 4343 — special losses (China impairment)
# -------------------------------------------------------------------
aeon_fantasy = dict(
    title="AEON Fantasy Co., Ltd. \u2014 4343.XTKS",
    subtitle="FY2026 impairment / special losses \u00b7 deep-dive \u00b7 " + TODAY,
    header_kv=[
        ("Ticker", "4343.XTKS"),
        ("Issuer FIGI", "BBG000LTB5H0"),
        ("Market cap", "USD 311m"),
        ("Signal date", "2026-04-09"),
        ("Signal type", "impairment_loss"),
        ("Direction", "SHORT (reduced conviction \u2014 see below)"),
        ("Score", "31 / 42.5"),
        ("Deep-dive date", TODAY),
    ],
    tldr=(
        "AEON Fantasy (in-mall amusement-arcade operator, AEON group subsidiary) booked ~JPY 2.3bn of "
        "one-off special losses in FY2026 (year ended 2026-02-28), driven by impairment of its China "
        "subsidiary plus FX losses and doubtful-account reserves. Despite the charge, the company "
        "reported positive net income of JPY 2.79bn for the period \u2014 i.e. the impairment is a clearing "
        "event, not a going-concern flag. The scanner\u2019s SHORT bias is now weakly supported; this looks "
        "like kitchen-sink housekeeping rather than an adverse earnings re-rating. Recommendation: "
        "downgrade to watchlist; re-evaluate if China store-count discloses acceleration in closures."
    ),
    company_context=[
        "AEON Fantasy: operator of in-mall amusement arcades (Mollyfantasy, etc.) across Japan and Asia (China, ASEAN).",
        "Market cap: USD 311m (at the bottom of the $300m floor).",
        "Parent: AEON Co. (8267, Japan\u2019s largest retailer, USD 30bn).",
        "China exposure: 100+ stores; operations have been a multi-year drag post-pandemic and amid China consumer softness.",
    ],
    release_contents=[
        "One-off special losses total: \u2248 JPY 2.3bn in FY2026.",
        "Composition: China subsidiary impairment (largest bucket), FX losses, doubtful-account reserves.",
        "Net income for FY2026: JPY 2.79bn \u2014 positive despite the charge (charge is < 100% of pre-items earnings).",
        "No revision to consolidated parent (AEON Co.) results.",
        "Forward-looking language: company expects profitability recovery post-charge.",
    ],
    thesis=(
        "<b>SHORT, low conviction.</b> The impairment is pre-announced, quantified (\u2248 JPY 2.3bn), and "
        "offset by positive net income. The market typically treats pre-flagged clearing charges as "
        "neutral-to-positive (removes overhang). The residual SHORT case rests on whether Chinese unit "
        "deterioration continues into FY2027 \u2014 an operational, not a one-off, risk. Better expression: "
        "wait for the FY2027 store-count disclosure and short only if China footprint is growing at "
        "negative unit-economics."
    ),
    catalyst_rows=[
        ["FY2026 result (released)", "2026-04-09", "Store-count / China EBIT disclosure", "Clarifies trajectory"],
        ["FY2027 guidance", UNV, "Negative China same-store sales", "Adds conviction to short"],
        ["AEON 8267 group disclosure", TODAY, "Parent-level guidance change", "Flags systemic group issue"],
        ["China retail / footfall data", "rolling", "China SS-sales < -5% sector", "Short entry trigger"],
        ["Further impairment", "0\u2013180d", "Second tranche of losses", "Hard short trigger"],
    ],
    steelman=[
        "Pre-flagged clearing impairments typically prompt relief rallies in Japanese names \u2014 the market-priced scenario is often worse than the release.",
        "AEON Fantasy has been closing underperforming units for 3+ years \u2014 this is late-cycle, not early-cycle, deterioration.",
        "Parent AEON\u2019s retail-consolidation strategy would benefit from absorbing or spinning the arcade business if it continues to drag \u2014 a corporate-action floor under the name.",
        "USD 311m mcap + Japanese small-cap borrow cost makes a short operationally expensive vs. the likely P&L capture.",
    ],
    kill_rows=[
        ["K1: market relief rally", "Stock +5% on release day", "XTKS tick"],
        ["K2: no second impairment in 6m", "No additional special losses", "Quarterly TDnet"],
        ["K3: China store closures on plan", "Actual \u2264 target closures", "FY2027 disclosures"],
        ["K4: AEON group-action", "Parent injects capital / restructures", "8267 TDnet"],
        ["K5: borrow cost block", "Stock-loan > 5%", "Broker desk"],
    ],
    peer_comparables=[
        "Other Japanese small-mid-cap China-exposed service names \u2014 Hidaya, Skylark, Watami 2023\u20132025 China retrenchments.",
        "AEON group 2024\u20132025 prior retail-unit impairments (Ministop, Aeon Hokkaido sister deals).",
        "Round1, Genki Sushi \u2014 Japanese mall-entertainment/food peers with similar China-exposure risk.",
    ],
    position_sizing=(
        "No high-conviction short on the release. If expressed, 0.25\u20130.5% NAV with 60-day hold; "
        "paired against AEON Co. (8267) to neutralize group-consolidation optionality. Exit on any "
        "parent-level corporate action or a \u22655% up-day."
    ),
    sources=[
        "AEON Fantasy FY2026 results release.",
        "Primary: TDnet PDF https://www.release.tdnet.info/inbs/140120260409500600.pdf.",
        "Non_us stub: 4343_XTKS_aeon-fantasy-special-losses.md.",
    ],
)

# -------------------------------------------------------------------
# 7. Bengo4.com 6027 — impairment / fee revision
# -------------------------------------------------------------------
bengo4 = dict(
    title="Bengo4.com, Inc. \u2014 6027.XTKS",
    subtitle="Impairment / fee-revision disclosure \u00b7 deep-dive \u00b7 " + TODAY,
    header_kv=[
        ("Ticker", "6027.XTKS"),
        ("Issuer FIGI", "BBG007HP08C9"),
        ("Market cap", "USD 389m"),
        ("Signal date", "2026-04-15"),
        ("Signal type", "impairment_loss (scanner) \u2192 possibly fee revision (press)"),
        ("Direction", "UNKNOWN (conflict between scanner tag and press coverage)"),
        ("Score", "31 / 42.5"),
        ("Deep-dive date", TODAY),
    ],
    tldr=(
        "The scanner flagged Bengo4.com (online legal-services platform \u2014 attorney matchmaking + "
        "e-contracting via CloudSign) with signal_type=impairment_loss on 2026-04-15. Public search "
        "within the 6-month window returned a separate, concurrent disclosure: a fee revision with "
        "\u2248 JPY 300m incremental ARR impact. No specific impairment amount or asset was confirmed in "
        "English press. This may be a scanner classification error (fee schedule \u2260 impairment) or "
        "two separate disclosures. <b>Direction flipped to UNKNOWN.</b> Recommendation: require Japanese-"
        "source translation at \u22650.85 confidence before acting."
    ),
    company_context=[
        "Bengo4.com: Japan\u2019s largest online legal-services platform \u2014 attorney directory + e-contracting (CloudSign is the dominant JP e-signature product).",
        "Market cap: USD 389m.",
        "Growth profile: ARR-driven SaaS business with contracting-side network effects (CloudSign dominates JP e-signature).",
        "Competitive: faces competition from DocuSign Japan, freee Sign, and new-entrant fintechs.",
    ],
    release_contents=[
        "Scanner tag: impairment_loss, translation_confidence < 0.85 (applied D-002 direction cap).",
        "Concurrent press finding: fee revision worth \u2248 JPY 300m incremental ARR \u2014 classic SaaS pricing uplift, a <i>positive</i> signal if stand-alone.",
        "No Japanese-source impairment amount has been surfaced in English press within the 6-month window; this may be a scanner classification artifact.",
        "Primary-source PDF: https://www.release.tdnet.info/inbs/140120260415504708.pdf \u2014 not translated in-run.",
    ],
    thesis=(
        "<b>UNKNOWN (refuse the scanner\u2019s SHORT default).</b> If the disclosure is a <i>fee revision</i>, "
        "the direction is LONG (SaaS pricing uplift is accretive). If the disclosure is an impairment, "
        "direction depends on the impaired asset (goodwill of an acquired practice-area would be short-"
        "neutral; impairment of CloudSign core would be materially negative). Without translation-at-"
        "confidence, no trade. Candidate should remain on the watchlist pending Japanese-source review."
    ),
    catalyst_rows=[
        ["Primary disclosure (6027 TDnet)", "2026-04-15", "n/a \u2014 signal source", "n/a"],
        ["Translation at \u22650.85", "0\u201314d", "Confirmed fee revision", "LONG re-tag"],
        ["Translation at \u22650.85", "0\u201314d", "Confirmed impairment of CloudSign goodwill", "SHORT re-tag"],
        ["Next quarterly earnings", UNV, "ARR run-rate change", "Directional confirmation"],
        ["DocuSign Japan / freee Sign commentary", "rolling", "Peer pricing action", "Read-through validation"],
    ],
    steelman=[
        "Bengo4.com has been consistently growing ARR and gross margin since 2021; a fee uplift disclosure is consistent with prior pricing-power thesis.",
        "If the scanner is mis-classifying a fee revision as an impairment, the direction is the <i>opposite</i> of the default SHORT \u2014 a classic case for D-002 honesty protocol.",
        "E-signature / e-contracting in Japan remains under-penetrated vs. US (legal requirement for physical hanko / seal only fully relaxed in 2022\u20132024); market tailwinds intact.",
        "Translation-confidence discipline exists precisely to prevent false-positive SHORTs on mis-classified benign disclosures.",
    ],
    kill_rows=[
        ["K1: confirmed fee revision only", "Japanese text shows pricing uplift", "Translation"],
        ["K2: impairment of a non-core asset", "< 5% total intangibles impaired", "Translation + balance sheet"],
        ["K3: ARR run-rate unchanged", "No material change at next Q", "Quarterly release"],
        ["K4: peer fee-revision echo", "DocuSign JP / freee Sign follow", "Industry press"],
        ["K5: BB coverage clean", "No analyst downgrades in window", "Sell-side notes"],
    ],
    peer_comparables=[
        "freee K.K. (4478) \u2014 Japanese SaaS peer; pricing-revision cadence since 2023.",
        "Money Forward (3994) \u2014 JP SaaS peer; fee-revision track record.",
        "DocuSign (DOCU US) \u2014 global e-signature benchmark for pricing-power.",
    ],
    position_sizing=(
        "No position. If translation confirms a clean fee revision, the direction re-tags LONG; "
        "sizing up to 0.5% NAV over 60-day hold. If impairment of core, sizing up to 0.5% NAV short."
    ),
    sources=[
        "Primary: TDnet PDF https://www.release.tdnet.info/inbs/140120260415504708.pdf (not translated in-run).",
        "Press on Bengo4 fee revision (research, " + UNV + " as primary-source).",
        "Non_us stub: 6027_XTKS_bengo4-com-impairment-loss.md.",
    ],
)

# -------------------------------------------------------------------
# 8. Makino Milling Machine 6135 — tender offer (active 2026?)
# -------------------------------------------------------------------
makino = dict(
    title="Makino Milling Machine Co., Ltd. \u2014 6135.XTKS",
    subtitle="Tender offer (re-attempted 2026; prior Nidec bid withdrawn May 2025) \u00b7 deep-dive \u00b7 " + TODAY,
    header_kv=[
        ("Ticker", "6135.XTKS"),
        ("Issuer FIGI", "BBG000BLH4Q5"),
        ("Market cap", "USD 1,710m"),
        ("Signal date", "2026-04-10"),
        ("Signal type", "tender_offer"),
        ("Direction", "LONG (merger-arb) \u2014 with elevated deal-break prior"),
        ("Score", "35 / 42.5"),
        ("Deep-dive date", TODAY),
    ],
    tldr=(
        "Makino Milling Machine (precision machine-tool builder) filed a tender-offer disclosure on "
        "2026-04-10. Historical context: Nidec\u2019s hostile bid in April 2025 was withdrawn within weeks "
        "(May 2025) after target-board opposition \u2014 so the 2026 filing is either (a) a <i>different</i> "
        "bidder re-attempting, or (b) a friendly / management-invited offer. Public 6-month search has "
        "NOT definitively confirmed the 2026 bidder identity or terms; primary-source translation "
        "required. <b>Because of the 2025 deal-break precedent, the arb-completion prior is lower than "
        "the typical 95%+</b> for Japanese friendly tenders. Recommendation: no position until bidder "
        "and price confirmed at \u22650.85 translation confidence."
    ),
    company_context=[
        "Makino Milling Machine: global precision machine-tool maker (CNC machining centers, EDM, 5-axis).",
        "Market cap: USD 1.71bn.",
        "Prior M&amp;A history: Nidec Corp\u2019s hostile tender offer April 2025, withdrawn May 2025 after target-board opposition.",
        "Strategic fit for potential acquirers: aerospace, EV-tooling, semiconductor-equipment supply chains all value the 5-axis / EDM IP portfolio.",
    ],
    release_contents=[
        "Scanner tag: tender_offer, signal_date 2026-04-10.",
        "Primary-source PDF: https://www.release.tdnet.info/inbs/140120260410501710.pdf \u2014 not translated in-run.",
        "Bidder identity: " + UNV + " in 6-month English-press window; may be (a) a DIFFERENT bidder (friendly), (b) Nidec re-attempting, or (c) an MBO.",
        "Offer price: " + UNV + ".",
        "Premium: " + UNV + ".",
    ],
    thesis=(
        "<b>Merger-arb LONG with elevated deal-break prior.</b> Standard Japanese friendly-tender "
        "completion rate is 95\u201398%; Makino is encumbered by the Nidec-rejection precedent which "
        "proves the board is willing to refuse. A new 2026 bid is more likely to be friendly / PE "
        "structured / management-supportive, but deal-break risk remains materially above base rate. "
        "Sizing should reflect this: half the usual arb size for a comparable scoring 35."
    ),
    catalyst_rows=tender_catalyst(UNV, UNV, UNV + " (2026 bidder not confirmed)"),
    steelman=[
        "The 2025 Nidec rejection was specifically about board ownership structure; a friendly / PE bid would clear that hurdle cleanly.",
        "Makino\u2019s market cap / free-cash-flow profile is attractive for PE carve-out theses (aerospace/EV tooling spin potential).",
        "A re-attempted bid from Nidec at a <i>higher</i> price is plausible and would complete if the board changes stance; optional upside.",
        "Absent primary-source confirmation, committing to a SHORT based on deal-break priors is equally unsupported \u2014 the honest read is NO TRADE.",
    ],
    kill_rows=[
        ["K1: bidder confirmed hostile / Nidec 2", "2026 bidder = Nidec on same terms", "Translation + bidder IR"],
        ["K2: bidder pulls financing", "Senior-debt LOI withdrawn", "Bidder IR"],
        ["K3: board opposition", "Target board recommends against", "Target TDnet"],
        ["K4: JFTC blocks", "Anti-trust objection", "JFTC register"],
        ["K5: friendly MBO confirmed", "Management-invited offer at premium", "Target TDnet"],
    ],
    peer_comparables=[
        "JSR / Japan Industrial Partners (2024) \u2014 PE take-private of Japanese industrial.",
        "Toshiba / JIP (2023) \u2014 successful mega-cap MBO after years of activist pressure; reference for board-flip dynamics.",
        "OKK \u2194 DMG MORI 2022 \u2014 peer machine-tool consolidation precedent.",
    ],
    position_sizing=(
        "No new entry without bidder/price confirmation. Once confirmed friendly at premium \u226520%, "
        "size at 0.5% NAV (half of normal Japanese merger-arb sizing) with 90-day hold and JPY hedge. "
        "If hostile re-attempt, no position."
    ),
    sources=[
        "Primary: TDnet PDF https://www.release.tdnet.info/inbs/140120260410501710.pdf (not translated in-run).",
        "2025 Nidec withdrawal history: public press, April\u2013May 2025.",
        "Non_us stub: 6135_XTKS_makino-milling-machine-co-ltd-tender-offer.md.",
    ],
)

# -------------------------------------------------------------------
# 9. Solasto 6197 — MBO (MBK Partners / PE)
# -------------------------------------------------------------------
solasto = dict(
    title="Solasto Corporation \u2014 6197.XTKS",
    subtitle="Management buyout (MBK Partners PE sponsor) \u00b7 deep-dive \u00b7 " + TODAY,
    header_kv=[
        ("Ticker", "6197.XTKS"),
        ("Issuer FIGI", "BBG00BGK7648"),
        ("Market cap", "USD 637m"),
        ("Signal date", "2026-04-09"),
        ("Signal type", "mbo_announcement"),
        ("Direction", "LONG (merger-arb)"),
        ("Score", "35 / 42.5"),
        ("Deep-dive date", TODAY),
    ],
    tldr=(
        "Solasto (medical / nursing / childcare outsourcing services) announced an MBO at JPY 1,119/"
        "share with MBK Partners as PE sponsor. Press references cite the offer price as \u2248 2x the "
        "undisturbed price and above the midpoint of the IFA DCF range \u2014 a <b>standard, well-priced "
        "Japanese PE take-private</b>. Tender period specifics not fully surfaced in 6-month English "
        "press but the transaction structure is consistent with 95%+ completion priors. Recommendation: "
        "merger-arb LONG, standard Japanese PE-take-private playbook."
    ),
    company_context=[
        "Solasto: outsourced medical-administration / nursing-care / childcare-services provider.",
        "Market cap: USD 637m.",
        "Secular thesis: Japan\u2019s aging-demographic tailwinds drive structural growth in medical-administration and nursing-care outsourcing.",
        "PE sponsor: MBK Partners \u2014 the largest North-Asia-focused PE firm (Korea/Japan/China exposure); track-record of operational take-privates.",
    ],
    release_contents=[
        "Offer price: JPY 1,119/share.",
        "Structural form: MBO with management rollover + MBK Partners capital.",
        "Premium: press cites \u2248 2x undisturbed price (i.e. ~100%); above IFA DCF midpoint \u2014 suggests competitive / well-contested process.",
        "Tender period: " + UNV + " in English press; typical Japanese MBO is 30\u201345 business days.",
        "Delist path: take-private via tender + squeeze-out cash-out; full delisting typical.",
    ],
    thesis=(
        "<b>Merger-arb LONG.</b> Well-priced PE-sponsored MBO with management rollover is the cleanest "
        "category in Japanese merger-arb. MBK Partners has a strong execution record; financing risk "
        "is low. Minor residual risks: (a) tender-close mechanical spread, (b) minor interloper risk "
        "(always non-zero), (c) a <i>lower</i> cash-out squeeze-out is essentially disallowed in Japan. "
        "Size at full Japanese-arb convention."
    ),
    catalyst_rows=tender_catalyst(UNV + " (likely May\u2013Jun 2026)",
                                  UNV + " (30\u201345 bizdays from 2026-04-09)",
                                  "MBK Partners / management"),
    steelman=[
        "Solasto\u2019s secular-growth profile supports a higher standalone DCF than the MBO price; aggrieved-shareholder litigation is a known tail risk for Japanese PE MBOs.",
        "MBK-sponsored deals have been completed smoothly in 2024\u20132025; reference class is clean.",
        "Borrow cost on a Japanese small-mid cap MBO target can widen during the tender window \u2014 watch for squeeze dynamics.",
        "The 2x undisturbed premium means the stock likely already gapped up; incremental arb return is low single-digit bps.",
    ],
    kill_rows=tender_kill(),
    peer_comparables=[
        "Benefit One / Dai-ichi Life (2024) \u2014 health-care services consolidation in Japan; bidding-war precedent.",
        "Nichiigakkan / Bain Capital (2021) \u2014 Japanese medical-services PE take-private; similar vertical.",
        "Tsubakimoto Group / MBK Partners (" + UNV + ") \u2014 MBK execution track-record reference.",
    ],
    position_sizing=(
        "Standard Japanese merger-arb sizing: 1.0\u20131.5% NAV, 60-90 day hold, JPY-hedged. Exit on "
        "settlement or on any confirmed interloper. No leverage."
    ),
    sources=[
        "Primary: TDnet PDF https://www.release.tdnet.info/inbs/140120260409500870.pdf.",
        "MBK Partners historical deal precedent (general context).",
        "Non_us stub: 6197_XTKS_solasto-mbo-tender-offer.md.",
    ],
)

# -------------------------------------------------------------------
# 10. Daikin 6367 — US HVAC price-fixing class action
# -------------------------------------------------------------------
daikin = dict(
    title="Daikin Industries, Ltd. \u2014 6367.XTKS",
    subtitle="US HVAC price-fixing class action \u00b7 deep-dive \u00b7 " + TODAY,
    header_kv=[
        ("Ticker", "6367.XTKS"),
        ("Issuer FIGI", "BBG000BLNXT1"),
        ("Market cap", "USD 37,335m"),
        ("Signal date", "2026-04-10"),
        ("Signal type", "litigation_regulatory"),
        ("Direction", "SHORT (low conviction on mega-cap)"),
        ("Score", "29 / 42.5"),
        ("Deep-dive date", TODAY),
    ],
    tldr=(
        "Daikin Industries (world\u2019s largest HVAC manufacturer, \u224835% US market share) was named "
        "defendant in a US putative class action filed 2026-03-20 (E.D. Mich.) alleging price-fixing "
        "of HVAC equipment from 2020-01-01 onward. Co-defendants include Daikin US subsidiaries "
        "(Comfort Tech, Applied Americas, Thermal-Netics) and " + UNV + " competitors. This is <i>NOT</i> "
        "the PFAS / fluorinated-chemical regulatory vector that is the typical Daikin tail; it is a "
        "fresh US antitrust matter. At USD 37bn mcap, a single-digit-% damages outcome (plausible "
        "range in tried HVAC antitrust cases: 3\u20136% of US revenue over the class period) is a "
        "material but not existential hit. <b>SHORT is low-conviction \u2014 typical US class-action "
        "headline decay is 6\u201312 months, and Daikin has strong operational tailwinds.</b>"
    ),
    company_context=[
        "Daikin: Japan-listed, global-#1 HVAC OEM; ~40% global residential share, ~35% US share post-Goodman acquisition (2012).",
        "Market cap: USD 37.3bn.",
        "Product mix: residential + commercial HVAC, chiller equipment, fluorinated chemicals (pfas-adjacent).",
        "Prior litigation: PFAS-related environmental cases in Tennessee / Georgia ongoing; these are separate from the 2026-03 antitrust matter.",
    ],
    release_contents=[
        "Case: putative class action filed 2026-03-20 in US District Court, Eastern District of Michigan.",
        "Claims: alleged price-fixing of HVAC equipment from 2020-01-01 to present.",
        "Defendants: Daikin Industries + Comfort Tech + Applied Americas + Thermal-Netics (US subsidiaries); " + UNV + " on co-defendants beyond Daikin group.",
        "Daikin disclosure: TDnet 2026-04-10; standard form \u201cnotice of filing of lawsuit\u201d.",
        "Discovery / motions timeline: typically 18\u201336 months from filing to trial; class certification decision 12\u201318 months from filing.",
    ],
    thesis=(
        "<b>SHORT, low conviction.</b> US antitrust class actions against non-US OEMs follow a "
        "predictable pattern: news-day drop 1\u20133%, 30-day drift \u00b15%, and resolution (settlement or "
        "dismissal) 2\u20135 years out at 3\u20138% of implicated revenue. For Daikin, the implicated US "
        "HVAC revenue is \u2248 USD 10\u201315bn/yr; a 5% damages midpoint = USD 0.5\u20130.75bn \u2248 1\u20132% of market "
        "cap \u2014 not needle-moving. Typical mega-cap headline decay is 6\u201312 months. Short is better "
        "expressed as relative to HVAC peers (Carrier, Trane, Lennox) via pair."
    ),
    catalyst_rows=[
        ["Motion to dismiss ruling", "6\u201312m", "Dismissal granted", "Cover short"],
        ["Class-certification hearing", "12\u201318m", "Class certified", "Short re-upped"],
        ["Settlement discussions", "18\u201336m", "Settlement rumor", "Cover half"],
        ["Q1/Q2 2026 earnings", "2026-05/08", "Litigation reserve > USD 300m booked", "Confirms scope"],
        ["Peer filings", "0\u20136m", "Co-defendant lists expand", "Validates sector-short"],
    ],
    steelman=[
        "US class-action filing rates against foreign OEMs are high; settlement multiples are typically small fractions of the alleged damages.",
        "Daikin\u2019s US operations are structurally demand-led by residential HVAC replacement cycles \u2014 antitrust tail doesn\u2019t touch near-term EBITDA.",
        "Post-Goodman integration, Daikin operating leverage in the US is a multi-year tailwind; any dip from litigation is an entry point, not a short.",
        "HVAC antitrust enforcement in the US is not an active DOJ priority \u2014 private plaintiffs have a higher-friction path than DOJ criminal referrals.",
        "The \u201cfluorinated chemicals\u201d tail (PFAS) is a bigger mid-horizon risk and is NOT this filing.",
    ],
    kill_rows=[
        ["K1: motion to dismiss granted", "Case dismissed at pleading stage", "PACER docket"],
        ["K2: reserve not recognized", "1H26 earnings: no litigation provision disclosed", "1H26 TDnet"],
        ["K3: peer relative outperformance", "CARR/TT/LII outperform Daikin by < 3% in 90d", "Market data"],
        ["K4: settlement at < 1% mcap", "Settlement terms < USD 370m", "Daikin settlement disclosure"],
        ["K5: DOJ does not pick up", "No criminal parallel within 12m", "DOJ antitrust press"],
    ],
    peer_comparables=[
        "LG / Samsung 2016\u20132020 US washing-machine antitrust \u2014 ultimate settlements were low-single-digit % of revenue.",
        "Japanese auto-parts cartel (2013\u20132017) \u2014 larger scale; settlements up to USD 1.5bn/defendant, materially higher than HVAC comp.",
        "Carrier / Trane US HVAC consolidation dynamics (GREE / Midea competitive pressure).",
    ],
    position_sizing=(
        "0.3\u20130.5% NAV short, paired long Carrier (CARR US) or Trane Technologies (TT US) to isolate "
        "the Daikin-specific headline effect. 90-day hold; cover on any dismissal motion granted."
    ),
    sources=[
        "TradingView / Reuters 2026-04 \u2014 \u201cDaikin notice regarding filing of lawsuit in United States\u201d.",
        "Primary: TDnet PDF https://www.release.tdnet.info/inbs/140120260410501326.pdf.",
        "Non_us stub: 6367_XTKS_daikin-industries-ltd-litigation-regulatory.md.",
    ],
)

# -------------------------------------------------------------------
# 11. Curves Holdings 7085 — profit upgrade
# -------------------------------------------------------------------
curves = dict(
    title="CURVES HOLDINGS Co., Ltd. \u2014 7085.XTKS",
    subtitle="Upward profit-forecast revision (FY08/26) \u00b7 deep-dive \u00b7 " + TODAY,
    header_kv=[
        ("Ticker", "7085.XTKS"),
        ("Issuer FIGI", "BBG002YJ0ZV4"),
        ("Market cap", "USD 504m"),
        ("Signal date", "2026-04-13"),
        ("Signal type", "profit_upgrade"),
        ("Direction", "LONG"),
        ("Score", "29 / 42.5"),
        ("Deep-dive date", TODAY),
    ],
    tldr=(
        "Curves Holdings (women\u2019s fitness-club franchise in Japan) upgraded FY08/26 guidance to "
        "revenue JPY 42.3bn (+12.6% YoY) and operating profit JPY 7.7bn (+21.4% YoY). H1 revenue "
        "printed JPY 19.97bn (+9.8% YoY) with H1 earnings JPY 2.23bn (EPS JPY 24.22). "
        "This is a clean profit-upgrade signal on a mid-cap Japanese franchise operator with "
        "operating-leverage tailwinds. Direction LONG, conviction medium. Recommendation: standard "
        "upgrade-momentum long with 30-60 day hold; exit on full-year print."
    ),
    company_context=[
        "Curves Holdings: Japan-franchise operator of Curves, a women-only 30-minute circuit fitness chain.",
        "Market cap: USD 504m.",
        "Fiscal year: ends August (FY08/26 = year ending 2026-08-31).",
        "Growth drivers: post-pandemic membership recovery, mild pricing power, low-single-digit store-count growth.",
    ],
    release_contents=[
        "FY08/26 guidance raised to: revenue JPY 42.3bn (+12.6% YoY), operating profit JPY 7.7bn (+21.4% YoY).",
        "H1 FY08/26 actuals: revenue JPY 19.967bn (+9.8% YoY), net income JPY 2.229bn (EPS JPY 24.22).",
        "Operating-profit growth outpaces revenue growth \u2014 clean margin-expansion signal.",
        "No material one-off items cited; the upgrade is operational.",
        "No change to dividend / capital-return policy disclosed.",
    ],
    thesis=(
        "<b>LONG, medium conviction.</b> Japanese small-mid-cap profit upgrades of the +20% OP-growth "
        "variety tend to produce a 30-60 day drift of +5\u201312% absent offsetting macro drag. Curves\u2019 "
        "operating model is high-fixed-cost / low-variable-cost \u2014 incremental membership converts at "
        "high incremental margin, so the upgrade is durable rather than timing-related. Risk: typical "
        "sell-side was already close to the new guide, so incremental surprise may be modest."
    ),
    catalyst_rows=[
        ["H2 FY08/26 trading update", "~2026-06-15", "SS sales > +5%", "Reiterate long"],
        ["FY08/26 full-year release", "~2026-10", "In-line or better than upgrade", "Exit long"],
        ["New-store-opening cadence", "rolling", "Net new stores > plan", "Add"],
        ["Dividend increase", "AGM ~Nov 2026", "DPS raised", "Positive tail"],
        ["Peer comps (RIZAP, Central Sports)", "rolling", "Peer guidance lift", "Sector validation"],
    ],
    steelman=[
        "Japanese fitness-chain margins are cyclically high right now; H2 faces tougher comps and normalization risk.",
        "Franchisee receivables growth can run ahead of cash \u2014 check cash conversion at H1 for deteriorating working capital.",
        "Small-mid cap Japanese names have seen stock prices under-react to upgrades when the Topix is weak \u2014 market-beta risk.",
        "Curves is a franchise-model, not company-owned \u2014 royalty revenue is less defensive than gym-operator revenue if franchisees fail.",
    ],
    kill_rows=[
        ["K1: H1 cash conversion weak", "OCF / OP < 70% at H1", "1H26 CF statement"],
        ["K2: SS sales stall", "+1% or worse in H2", "H2 trading update"],
        ["K3: franchisee receivables spike", "Doubtful accounts + > 20% YoY", "Balance sheet"],
        ["K4: macro JP consumer rollover", "JP consumer confidence < 40", "ESP / BoJ data"],
        ["K5: peer guidance cut", "RIZAP / Central Sports downgrade", "Peer TDnet"],
    ],
    peer_comparables=[
        "RIZAP Group (2928) \u2014 JP health/fitness; recent guidance cadence.",
        "Central Sports (4801) \u2014 integrated gym operator; margin-cycle reference.",
        "Snap Fitness / Anytime franchisee operators \u2014 international franchise-margin proxy.",
    ],
    position_sizing=(
        "0.5\u20131.0% NAV long, 60-day hold, exit on FY08/26 print. JPY-hedged. "
        "Upsize to 1.5% only if H1 cash conversion > 80% confirmed."
    ),
    sources=[
        "Primary: TDnet PDF https://www.release.tdnet.info/inbs/140120260413502360.pdf.",
        "Curves Holdings IR (guidance revision release).",
        "Non_us stub: 7085_XTKS_curves-holdings-co-ltd-profit-upgrade.md.",
    ],
)

# -------------------------------------------------------------------
# 12. Aeon Hokkaido 7512 — special losses (regional GMS impairment)
# -------------------------------------------------------------------
aeon_hokkaido = dict(
    title="Aeon Hokkaido Corporation \u2014 7512.XTKS",
    subtitle="Special losses disclosure \u00b7 deep-dive \u00b7 " + TODAY,
    header_kv=[
        ("Ticker", "7512.XTKS"),
        ("Issuer FIGI", "BBG000H7HQ72"),
        ("Market cap", "USD 759m"),
        ("Signal date", "2026-04-09"),
        ("Signal type", "impairment_loss"),
        ("Direction", "SHORT (conviction medium)"),
        ("Score", "31 / 42.5"),
        ("Deep-dive date", TODAY),
    ],
    tldr=(
        "Aeon Hokkaido (regional GMS/supermarket operator, AEON group subsidiary) disclosed special "
        "losses on 2026-04-08/09. Exact amount and asset not surfaced in English press (primary-source "
        "translation required). Regional GMS (general-merchandise-store) operators in Japan are under "
        "structural pressure from secular footfall decline, e-commerce cannibalization, and depopulation "
        "in non-Tokyo regions \u2014 Hokkaido especially suffers population outflow. <b>SHORT direction is "
        "supported by the secular backdrop.</b> Recommendation: watchlist long with short-bias if impairment "
        "confirmed > 5% book value."
    ),
    company_context=[
        "Aeon Hokkaido: regional GMS / supermarket operator, footprint concentrated in Hokkaido.",
        "Market cap: USD 759m.",
        "Parent: AEON Co. (8267).",
        "Secular pressures: Hokkaido population has declined ~8% since 2015 (Japan Census); GMS footfall down high-single-digits; e-commerce penetration growing.",
        "2024\u20132025 AEON group restructurings: several regional-grocer subsidiaries under review for consolidation.",
    ],
    release_contents=[
        "Filing: TDnet impairment disclosure 2026-04-08.",
        "Scanner signal_strength 3/4; translation_confidence below 0.85 triggered direction-cap review.",
        "Amount / assets impaired: " + UNV + " in 6-month English-press window.",
        "Context: AEON group has booked multiple regional-GMS impairments in 2023\u20132025 (Ministop, Aeon Super-Center).",
        "No offsetting income items cited in press.",
    ],
    thesis=(
        "<b>SHORT, medium conviction.</b> Regional Japanese GMS operators face secular footfall and "
        "population headwinds that support a structural-short thesis. The impairment disclosure is "
        "consistent with that trajectory. Parent-AEON may ultimately consolidate / delist the "
        "subsidiary \u2014 a risk to the short (take-under premium), but 2024\u20132025 AEON group actions "
        "have typically been at slim premia or at book value, not at large premiums."
    ),
    catalyst_rows=[
        ["Full impairment detail", "0\u201330d", "Amount / asset confirmed", "Re-rate thesis"],
        ["FY2027 guidance", UNV + " (Apr\u2013May 2026)", "SS sales < -3%", "Short trigger"],
        ["AEON parent consolidation", "0\u2013180d", "Tender offer / carve-out", "Cover / trade-out"],
        ["Hokkaido population data", "annual", "-1%+ YoY continued", "Short validated"],
        ["Peer regional-GMS data", "rolling", "Izumi, Valor regional comps", "Sector read"],
    ],
    steelman=[
        "Parent AEON has a pattern of consolidating underperforming subsidiaries at small premia to market \u2014 this is a risk to any short.",
        "Hokkaido tourism inbound is recovering strongly post-2023 border reopen \u2014 may partially offset depopulation drag.",
        "AEON group restructuring 2023\u20132025 has been incremental rather than drastic; no near-term forced sale.",
        "Regional grocery margins in Japan are structurally low but stable; impairments are episodic, not run-rate.",
    ],
    kill_rows=[
        ["K1: AEON announces consolidation tender", "8267 tender disclosure for 7512", "AEON / 7512 TDnet"],
        ["K2: SS sales stabilize", "-1% or better QoQ", "Monthly SS release"],
        ["K3: impairment < 2% book", "Small cleaning only", "Detailed disclosure"],
        ["K4: Hokkaido population rebound", "Prefectural census positive", "Hokkaido gov\u2019t data"],
        ["K5: borrow cost block", "Stock-loan > 5%", "Broker desk"],
    ],
    peer_comparables=[
        "Izumi Co. (8273) \u2014 Chugoku-region GMS; secular pressure analog.",
        "Valor Holdings (9956) \u2014 Chubu-region grocer; similar model, stronger growth profile (counter-example).",
        "Ministop (9946) \u2014 AEON-group convenience subsidiary; prior impairment / restructuring case study.",
    ],
    position_sizing=(
        "0.3\u20130.6% NAV short pending translation. If impairment confirmed > 5% book, upsize to "
        "0.8\u20131.0% NAV. Pair against AEON (8267) or Seven &amp; i (3382) to reduce group-action risk."
    ),
    sources=[
        "Primary: TDnet PDF https://www.release.tdnet.info/inbs/140120260408500265.pdf (not translated in-run).",
        "AEON group restructuring context (public press, 2024\u20132025).",
        "Non_us stub: 7512_XTKS_aeon-hokkaido-special-losses.md.",
    ],
)

# -------------------------------------------------------------------
# 13. ITOCHU 8001 — bidder on subsidiary tender (cross-ref with 2692 / 8934)
# -------------------------------------------------------------------
itochu_parent = dict(
    title="ITOCHU Corporation \u2014 8001.XTKS",
    subtitle="Bidder in parallel tender offers (subsidiary consolidation) \u00b7 deep-dive \u00b7 " + TODAY,
    header_kv=[
        ("Ticker", "8001.XTKS"),
        ("Issuer FIGI", "BBG000B9WJ55"),
        ("Market cap", "USD 87,870m"),
        ("Signal date", "2026-04-10"),
        ("Signal type", "tender_offer (BIDDER, not target) \u2014 direction inverted from scanner"),
        ("Direction", "NEUTRAL / LONG-biased (mega-cap capital-allocation signal)"),
        ("Score", "35 / 42.5 (route reconsidered)"),
        ("Deep-dive date", TODAY),
    ],
    tldr=(
        "<b>Scanner tag misleading for trading.</b> ITOCHU Corp (Japan\u2019s second-largest s\u014dg\u014d "
        "sh\u014dsha, USD 88bn mcap) is the <i>bidder</i>, not the target, in parallel tender offers: "
        "(i) ITOCHU-SHOKUHIN 2692 at JPY 13,000/share (JPY 78.4bn spend, take-private); "
        "(ii) Sun Frontier Fudousan 8934 at JPY 2,800/share (up to ~JPY 18.6bn for 12% + 5.5bn "
        "3rd-party allotment, equity-method affiliate). <b>There is no merger-arb trade on 8001 itself.</b> "
        "The signal reads as a positive capital-allocation / simplification data point (consistent with "
        "Buffett-era policy of subsidiary tidying). Recommendation: remove from candidate list or "
        "reclassify as watchlist LONG-read on ITOCHU\u2019s capital-return / simplification cadence."
    ),
    company_context=[
        "ITOCHU Corp: one of Japan\u2019s five s\u014dg\u014d sh\u014dsha (trading houses); businesses across textiles, machinery, food, general products, chemicals, energy.",
        "Market cap: USD 87.87bn (mega-cap).",
        "Berkshire Hathaway owns a ~8\u20139% stake across the big-5 trading houses; this has anchored a simplification / buyback / subsidiary-tidying strategy since 2020.",
        "ITOCHU has consolidated multiple subsidiaries 2022\u20132025 (Techno-Solutions, Enex, FamilyMart).",
    ],
    release_contents=[
        "Parallel transactions announced 2026-02 onwards; TDnet confirmations 2026-04-09/10.",
        "(i) ITOCHU-SHOKUHIN take-private via G.K. FMDI SPV at JPY 13,000/share; JPY 78.4bn; minimum 1.8m shares (2/3 voting).",
        "(ii) Sun Frontier Fudousan partial stake via SI Co. SPV at JPY 2,800/share; target 6.66m shares (~12% incremental); JPY 5.5bn 3rd-party allotment.",
        "Total announced outlay: \u2248 JPY 85\u2013100bn (< 0.1% of ITOCHU\u2019s market cap) \u2014 small relative to parent size.",
        "Use of proceeds: simplification, full consolidation of food-trading subsidiary; increased influence in real-estate services.",
    ],
    thesis=(
        "<b>No direct trade on 8001.</b> The disclosure is a bidder-side simplification move, not a "
        "target event. At 0.1% of mcap, it is neutral to ITOCHU\u2019s own P&amp;L. However, the signal is "
        "positive for the ongoing narrative of trading-house simplification / buybacks / Berkshire-era "
        "capital stewardship. For a discretionary LONG holder of ITOCHU, this is confirmatory."
    ),
    catalyst_rows=[
        ["Parallel tender settlements", "2026-04-09 \u2013 2026-04-16", "Completions confirmed", "Simplification executed"],
        ["Next buyback announcement", UNV + " (Q1 earnings)", "Buyback authority +", "Positive LONG signal"],
        ["FY26 earnings call", UNV + " (2026-05)", "Simplification economics discussed", "Re-rating potential"],
        ["Berkshire stake update", "annual 13F", "Stake held or raised", "Ownership anchor"],
        ["Peer sh\u014dsha parallel moves", "rolling", "Mitsui / Sumitomo echo actions", "Sector-level read"],
    ],
    steelman=[
        "Trading-house simplification is already priced; another subsidiary tidy-up at 0.1% mcap won\u2019t re-rate ITOCHU.",
        "Japanese trading houses have had strong 2023\u20132025 outperformance on Buffett-branding; the trade has matured.",
        "Commodity cycle is a bigger driver of 8001 stock than subsidiary actions \u2014 the TDnet filing is low-information.",
        "The true LONG thesis on 8001 rests on buybacks + dividend growth, not subsidiary tidy-ups.",
    ],
    kill_rows=[
        ["K1: buyback cadence slows", "No new authorization in 2 consecutive quarters", "ITOCHU IR"],
        ["K2: commodity cycle rolls", "Brent < USD 60 for 30d", "Oil data"],
        ["K3: Berkshire trims", "13F shows reduction", "Berkshire 13F"],
        ["K4: simplification pause", "No more subsidiary tidy-ups in 6m", "ITOCHU TDnet"],
        ["K5: FX adverse", "JPY/USD < 130 (JPY strengthens) > 10%", "FX market"],
    ],
    peer_comparables=[
        "Mitsui &amp; Co. (8031) \u2014 peer sh\u014dsha; similar subsidiary-consolidation cadence.",
        "Sumitomo Corp (8053), Marubeni (8002), Mitsubishi Corp (8058) \u2014 trading-house peer group.",
        "Berkshire Hathaway\u2019s 2020\u20132025 big-5 trading-house thesis (annual letters).",
    ],
    position_sizing=(
        "No new trade purely on this signal. For an existing ITOCHU LONG, this is confirmatory. "
        "Maintain position at standard discretionary sizing (0.5\u20132% NAV) with buyback-cadence "
        "and commodity-cycle as the primary monitoring axes."
    ),
    sources=[
        "TradingView / Reuters 2026-04 \u2014 ITOCHU Shokuhin & Sun Frontier tender-offer press.",
        "Primary: TDnet PDF https://www.release.tdnet.info/inbs/140120260410501650.pdf.",
        "Non_us stub: 8001_XTKS_itochu-oration-tender-offer.md.",
    ],
)

# -------------------------------------------------------------------
# 14. AEON 8267 — same as Tsuruha bidder side
# -------------------------------------------------------------------
aeon_parent = dict(
    title="AEON Co., Ltd. \u2014 8267.XTKS",
    subtitle="Bidder in Tsuruha control consolidation \u00b7 deep-dive \u00b7 " + TODAY,
    header_kv=[
        ("Ticker", "8267.XTKS"),
        ("Issuer FIGI", "BBG000BN0FD8"),
        ("Market cap", "USD 30,388m"),
        ("Signal date", "2026-04-15"),
        ("Signal type", "tender_offer (BIDDER, direction inverted from scanner)"),
        ("Direction", "LONG (strategic-consolidation read)"),
        ("Score", "35 / 42.5"),
        ("Deep-dive date", TODAY),
    ],
    tldr=(
        "<b>Scanner mis-directed: AEON is the BIDDER, not the target.</b> AEON Co. (Japan\u2019s "
        "largest retailer, USD 30bn mcap) on 2026-04-15 confirmed control (50.9% voting) of Tsuruha "
        "Holdings (3391) via the completed Dec 2025 JPY 11,400/share tender + subsequent open-market "
        "purchases through 2026-04-30. This caps the multi-year creation of Japan\u2019s dominant drugstore "
        "group (Welcia-Tsuruha merged 2025-12-01; combined \u22485,600 stores, >JPY 2trn revenue). "
        "<b>For AEON itself, the signal is strategic-positive</b>: drugstore consolidation lifts group "
        "EBITDA margin and defensive quality. Recommendation: watchlist LONG on AEON for the "
        "synergy-realization re-rating thesis over 12\u201324 months."
    ),
    company_context=[
        "AEON Co.: Japan\u2019s #1 retailer (GMS, supermarkets, specialty stores, financial services, drugstores via Welcia then Welcia-Tsuruha).",
        "Market cap: USD 30.4bn.",
        "Strategic posture 2023\u20132026: integrating drugstore vertical (Welcia + Tsuruha + various regional chains); consolidating regional GMS subsidiaries (Aeon Hokkaido, Aeon Kyushu).",
        "Activist backdrop: Orbis Investments publicly opposed the 2024\u20132025 Welcia-Tsuruha merger structure, arguing it underpriced Tsuruha minorities.",
    ],
    release_contents=[
        "Filing confirms 2026-04-15 AEON achieved 50.9% voting control of Tsuruha Holdings.",
        "Path: tender offer Dec 3 2025 \u2013 Jan 6 2026 at JPY 11,400/share + open-market accumulation Jan 9 \u2013 Apr 30 2026.",
        "Tender premium: 4.6% over 2025-04-11 unaffected close (thin \u2014 reflects prior accumulation).",
        "Combined entity (Welcia-Tsuruha): \u22485,600 stores, >JPY 2trn revenue; Japan\u2019s #1 drugstore.",
        "Residual: minority squeeze-out of Tsuruha to follow; Orbis-style fair-value litigation risk remains.",
    ],
    thesis=(
        "<b>LONG on AEON.</b> The drugstore-consolidation thesis is now the dominant structural read "
        "on AEON. Drugstores in Japan have higher GP% than GMS and are growing; Welcia-Tsuruha at "
        "\u22485,600 stores is a market-structure moat. Synergy realization (procurement, private-label "
        "rollout, real-estate rationalization) over 12\u201324 months is the catalyst. Risk: Orbis-style "
        "litigation and minority-shareholder fair-value claims could force additional cash outlay."
    ),
    catalyst_rows=[
        ["Control confirmation (filing)", "2026-04-15", "\u2265 50% voting", "Signal source"],
        ["Welcia-Tsuruha integration plan", "0\u2013180d", "Synergy targets articulated", "Re-rating catalyst"],
        ["FY27 guidance lift", "2026-10", "EBITDA guide raised on drugstore mix", "Long confirmed"],
        ["Orbis / minority litigation", "0\u2013180d", "Court filing", "Headline risk"],
        ["Aeon Hokkaido / Fantasy restructurings", "rolling", "Subsidiary consolidations", "Capital-allocation focus"],
    ],
    steelman=[
        "Japanese retail has persistent single-digit margins; drugstore consolidation lifts mix but not enough to drive a material re-rating on a USD 30bn mcap.",
        "Orbis-style fair-value litigation has a long tail of headline risk and could force AEON to top up the tender price in squeeze-out.",
        "AEON group has many underperforming regional subsidiaries (Hokkaido, Fantasy) that drag group returns.",
        "Domestic retail is a zero-growth nominal market; drugstore tailwind is real but aging-demographic pull isn\u2019t enough to offset GMS headwind.",
    ],
    kill_rows=[
        ["K1: synergy target misses", "FY28 EBITDA guide below consensus", "FY28 guidance"],
        ["K2: Orbis minority settlement forces top-up", "Settlement > JPY 100/share true-up", "Court filing / settlement disclosure"],
        ["K3: regional GMS drag accelerates", "Group SS sales stall 2+ quarters", "Monthly SS release"],
        ["K4: Seven &amp; i restructure competitive shock", "SHB/IYH re-rating outpaces AEON", "Market data"],
        ["K5: JFTC mandates divestiture", "JFTC order against drugstore combination", "JFTC register"],
    ],
    peer_comparables=[
        "Seven &amp; i Holdings (3382) \u2014 peer convenience/GMS group; restructuring reference.",
        "Welcia Holdings 2025 delisting \u2014 inside-group precedent.",
        "PPIH (7532, Don Quijote) \u2014 retail consolidator; growth-through-acquisition analog.",
    ],
    position_sizing=(
        "Discretionary LONG 0.5\u20131.5% NAV, 12\u201318 month hold, JPY-hedged. Upsize on synergy-guide "
        "confirmation; trim on any Orbis-style settlement > JPY 100/share."
    ),
    sources=[
        "Primary: TDnet PDF https://www.release.tdnet.info/inbs/140120260415504769.pdf.",
        "BusinessWire Orbis statement 2025 \u2014 opposition backdrop.",
        "Non_us stub: 8267_XTKS_aeon-co-ltd-tender-offer.md.",
    ],
)

# -------------------------------------------------------------------
# 15. Sun Frontier Fudousan 8934 — partial acquisition by Itochu
# -------------------------------------------------------------------
sun_frontier = dict(
    title="Sun Frontier Fudousan Co., Ltd. \u2014 8934.XTKS",
    subtitle="ITOCHU partial tender (equity-method affiliate path) \u00b7 deep-dive \u00b7 " + TODAY,
    header_kv=[
        ("Ticker", "8934.XTKS"),
        ("Issuer FIGI", "BBG000QCBR02"),
        ("Market cap", "USD 1,017m"),
        ("Signal date", "2026-04-10"),
        ("Signal type", "tender_offer (partial) + 3rd-party allotment"),
        ("Direction", "LONG (merger-arb, partial)"),
        ("Score", "35 / 42.5"),
        ("Deep-dive date", TODAY),
    ],
    tldr=(
        "ITOCHU Corp, via wholly-owned SPV SI Co., tendered for up to 6.66m shares (\u224812% increment) "
        "of Sun Frontier Fudousan at JPY 2,800/share plus a JPY 5.5bn 3rd-party allotment. The tender "
        "closed 2026-04-09; settlement 2026-04-16. Sun Frontier will become an ITOCHU equity-method "
        "affiliate and <b>remain publicly listed</b> (unlike ITOCHU-SHOKUHIN, which is going fully "
        "private). Structure is a partial-tender + capital-injection hybrid. Recommendation: "
        "no take-private arb; the equity-method-affiliate structure leaves the stock trading with "
        "minority-shareholder overhang but strategic-partner support. Direction LONG is valid at "
        "modest conviction on the capital-injection dilution / control-premium trade-off."
    ),
    company_context=[
        "Sun Frontier Fudousan: real-estate services & property-management (Tokyo mid-cap).",
        "Market cap: USD 1.02bn.",
        "Pre-transaction ownership: ITOCHU has held a minority position; post-transaction ITOCHU equity-method affiliate.",
        "ITOCHU\u2019s strategic rationale: deepening real-estate exposure through a mid-market broker/operator without full take-private.",
    ],
    release_contents=[
        "Bidder: ITOCHU via SI Co. (wholly-owned SPV).",
        "Tender price: JPY 2,800/share.",
        "Target: up to 6.66m shares (\u224812% of shares outstanding).",
        "Companion: JPY 5.5bn 3rd-party allotment (capital injection at negotiated price).",
        "Tender period: commenced 2026-02-26; closed 2026-04-09.",
        "Settlement: 2026-04-16.",
        "Post-transaction: ITOCHU equity-method affiliate; Sun Frontier to remain listed.",
    ],
    thesis=(
        "<b>Merger-arb LONG on the partial tender; LONG-read on the full stock.</b> The partial tender "
        "closes at a price floor of JPY 2,800, which anchors the stock\u2019s post-deal range. Post-"
        "settlement, strategic partnership with ITOCHU (real-estate synergies, balance-sheet support) "
        "creates optionality for a future full take-private at higher price. Risk: partial tenders "
        "with 3rd-party allotments dilute existing EPS; if growth doesn\u2019t compensate, stock drifts sideways."
    ),
    catalyst_rows=tender_catalyst("2026-04-16", "2026-04-09 (closed)", "ITOCHU / SI Co."),
    steelman=[
        "Partial tenders with 3rd-party allotments are dilutive; Sun Frontier EPS may be flat-to-down in FY27 depending on capital deployment speed.",
        "Equity-method affiliate status offers strategic optionality but doesn\u2019t guarantee a future take-private; the minority overhang can persist for years.",
        "JPY 2,800 may not be a durable floor if Japanese rates rise materially (real estate is rate-sensitive).",
        "Real-estate services mid-caps have been sector-underperformers in 2025 on JGB yield widening.",
    ],
    kill_rows=tender_kill(),
    peer_comparables=[
        "Tokyu Fudousan / peer TSE real-estate consolidators.",
        "Mitsui Fudousan &amp; Tokio Marine cross-shareholding unwinds (2024\u20132025).",
        "FamilyMart / Itochu 2020 take-private \u2014 sh\u014dsha-affiliate precedent.",
    ],
    position_sizing=(
        "0.5\u20131.0% NAV post-settlement LONG for optionality on a future full take-private. "
        "Hedge via JGB duration exposure on sector basket if rate risk is a concern."
    ),
    sources=[
        "Primary: TDnet PDF https://www.release.tdnet.info/inbs/140120260410501837.pdf.",
        "TradingView / Reuters 2026-04 \u2014 ITOCHU / Sun Frontier coverage.",
        "Non_us stub: 8934_XTKS_sun-frontier-fudousan-co-ltd-tender-offer.md.",
    ],
)

# -------------------------------------------------------------------
# 16. Cuscal CCL.XASX — institutional placement
# -------------------------------------------------------------------
cuscal = dict(
    title="Cuscal Limited \u2014 CCL.XASX",
    subtitle="Institutional placement + SPP funding Paymark (NZ) acquisition \u00b7 deep-dive \u00b7 " + TODAY,
    header_kv=[
        ("Ticker", "CCL.XASX"),
        ("Issuer FIGI", "BBG000BTKL40"),
        ("Market cap", "USD 575m (AUD ~805m)"),
        ("Signal date", "2026-04-14"),
        ("Signal type", "equity_placement"),
        ("Direction", "NEUTRAL (downgraded from short: proceeds fund strategic acquisition)"),
        ("Score", "30.5 / 42.5"),
        ("Deep-dive date", TODAY),
    ],
    tldr=(
        "Cuscal (ASX-listed Australian payments / banking-services infrastructure provider) "
        "completed a AUD 30m institutional placement at AUD 4.00/share (5.0% discount to last "
        "close), alongside an AUD 3m non-underwritten Share Purchase Plan on the same pricing. "
        "<b>Proceeds fund a specific, announced-same-day acquisition of Paymark, New Zealand\u2019s "
        "leading payments processor, for AUD 27m.</b> This is NOT a generic capital-buffer top-up "
        "(the scanner\u2019s default SHORT is downgraded): the raise-and-deploy is tight (98%+ of "
        "proceeds earmarked), the tight 5% discount signals strong institutional demand, and the "
        "acquisition extends Cuscal\u2019s cross-Tasman payments footprint. Recommendation: "
        "reclassify as NEUTRAL with LONG-read on 12-month synergy-realization; remove from "
        "immediate SHORT route."
    ),
    company_context=[
        "Cuscal: Australian payment infrastructure (BIN sponsor, card-issuing processor, direct-entry payments processor). IPO\u2019d on ASX in late 2023.",
        "Market cap: USD 575m (AUD ~805m at 0.7138).",
        "Client base: mutual banks, credit unions, fintechs (86400 legacy), neo-banks.",
        "Capital structure: recent IPO, moderate float; Mastercard was a pre-IPO shareholder.",
        "52-week range: AUD 2.44\u20134.55 " + UNV + " as of signal date.",
    ],
    release_contents=[
        "Institutional placement: AUD 30m at AUD 4.00/share (5.0% discount to last close, fully underwritten).",
        "Share Purchase Plan (SPP): AUD 3m (non-underwritten) at the same AUD 4.00/share.",
        "Use of proceeds: AUD 27m acquisition of Paymark (NZ payments processor) + transaction costs.",
        "Acquisition announced same day \u2014 no generic buffer language.",
        "Post-transaction share count: dilution \u2248 0.9\u20131.0% (immaterial on absolute basis).",
        "Paymark strategic fit: expands Cuscal into NZ payments infrastructure; creates cross-Tasman platform.",
    ],
    thesis=(
        "<b>NEUTRAL, with LONG-read on synergy.</b> The default short-placement-overhang thesis does "
        "not apply here because (a) the discount is tight (5%, not the 10\u201315% typical of emergency "
        "raises), (b) proceeds are fully earmarked for a named accretive acquisition, (c) dilution "
        "is minor (< 1%), and (d) strategic rationale is clear (cross-Tasman payments platform). "
        "The trade-worthy angle is 6\u201312-month integration execution and the NZ payments market share "
        "re-rate. No short. No immediate long at placement price \u2014 wait for post-settlement trading "
        "and first integration data point."
    ),
    catalyst_rows=[
        ["Placement settlement (T+2)", "2026-04-16 / 17", "Placement trades settle", "Overhang checkpoint"],
        ["SPP completion", "3\u20134 weeks post-placement", "AUD 3m target hit", "Retail absorption signal"],
        ["Paymark acquisition close", "0\u201390d", "NZ FIRB/Commerce Commission clearances", "Deal closes"],
        ["FY26 earnings (Aug 2026)", "~2026-08", "Integration guidance issued", "Re-rating catalyst"],
        ["Mastercard stake update", UNV, "Mastercard top-up or trim", "Anchor signal"],
    ],
    steelman=[
        "Even well-priced placements produce a 1\u20133 month consolidation as institutions take profit on allocation; short-term grind is a real risk to any long.",
        "Paymark is a sub-scale NZ business; integration risk is non-trivial and synergy math can be over-promised.",
        "Australian payments infrastructure is commoditizing; Cuscal\u2019s moat is real but not durable against bigger BIN-sponsor entrants.",
        "NZ payments regulation (Commerce Commission) has been actively squeezing card-scheme economics \u2014 acquisition timing into a regulatory squeeze is ambiguous.",
    ],
    kill_rows=[
        ["K1: Paymark deal breaks", "NZCC or FIRB blocks", "NZCC / FIRB public notices"],
        ["K2: placement discount widens post-settlement", "Stock < AUD 3.90 within 30d", "ASX tick"],
        ["K3: integration miss", "FY27 guidance below pre-deal consensus", "FY27 guide"],
        ["K4: NZ regulatory squeeze", "NZCC interchange cap", "NZCC public notice"],
        ["K5: Mastercard exits", "Pre-IPO holder sell-down", "ASX substantial-holder notice"],
    ],
    peer_comparables=[
        "Tyro Payments (TYR.AX) \u2014 Australian payments peer; IPO-era comp.",
        "EML Payments (EML.AX) \u2014 Australian cross-border payments; regulatory-risk case study.",
        "Paymark pre-acquisition comparables (NZ payments space, pre-Ingenico/Worldline consolidation).",
    ],
    position_sizing=(
        "No position at placement price. If Paymark closes on-time and FY26 guidance is reiterated, "
        "build LONG 0.25\u20130.5% NAV on the synergy-realization thesis with 12-month hold. "
        "AUD hedge optional."
    ),
    sources=[
        "Sharecafe 2026-04-14 \u2014 \u201cCuscal announces A$27m acquisition of New Zealand\u2019s Paymark, launches equity raise\u201d.",
        "ASX announcement documentKey 2924-03078874-2A1666525 (placement completion).",
        "Non_us stub: CCL_XASX_cuscal-institutional-placement.md.",
    ],
)


ALL = [
    ("1882_XTKS_toa-road-oration-litigation-regulatory", toa_road),
    ("2540_XTKS_yomeishu-seizo-co-ltd-tender-offer", yomeishu),
    ("2692_XTKS_itochu-shokuhin-co-ltd-tender-offer", itochu_shokuhin),
    ("3391_XTKS_tsuruha-holdings-tender-offer", tsuruha),
    ("4206_XTKS_aica-kogyo-company-limited-tender-offer", aica),
    ("4343_XTKS_aeon-fantasy-special-losses", aeon_fantasy),
    ("6027_XTKS_bengo4-com-impairment-loss", bengo4),
    ("6135_XTKS_makino-milling-machine-co-ltd-tender-offer", makino),
    ("6197_XTKS_solasto-mbo-tender-offer", solasto),
    ("6367_XTKS_daikin-industries-ltd-litigation-regulatory", daikin),
    ("7085_XTKS_curves-holdings-co-ltd-profit-upgrade", curves),
    ("7512_XTKS_aeon-hokkaido-special-losses", aeon_hokkaido),
    ("8001_XTKS_itochu-oration-tender-offer", itochu_parent),
    ("8267_XTKS_aeon-co-ltd-tender-offer", aeon_parent),
    ("8934_XTKS_sun-frontier-fudousan-co-ltd-tender-offer", sun_frontier),
    ("CCL_XASX_cuscal-institutional-placement", cuscal),
]

if __name__ == "__main__":
    for stem, dd in ALL:
        path = os.path.join(OUT, stem + ".pdf")
        build_pdf(path, dd)
        print("wrote", path)
    print("done:", len(ALL))
