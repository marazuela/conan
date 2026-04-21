# Strategy — India BSE / NSE

**Exchange:** Bombay Stock Exchange (BSE) and National Stock Exchange (NSE) — India
**MIC:** XBOM (BSE), XNSE (NSE)
**Language:** English (primary regulatory language). Some Hindi/regional language filings for SME boards — not in scope.
**Translation required:** No — English is the statutory language for SEBI disclosures.
**Build phase:** 7
**Status:** STUB — to flesh out in Phase 7 after Korea is stable.

---

## 1. Data source

Both exchanges publish real-time corporate announcements. SEBI (Securities and Exchange Board of India) mandates continuous disclosure under LODR Regulations.

**Planned primary endpoints (UNVERIFIED — probe at Phase 7):**

- BSE: `https://www.bseindia.com/corporates/ann.html` — corporate announcements.
- NSE: `https://www.nseindia.com/companies-listing/corporate-filings-announcements` — corporate filings.
- SEBI SAST filings (substantial acquisition): `https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&ssid=15&smid=0`.

Most large-cap issuers file on both BSE and NSE. Dedup required by `issuer_figi` (not ticker — BSE scrip code ≠ NSE symbol).

## 2. Filing categories of interest

| SEBI / exchange disclosure type | Signal type |
|----------------------------------|-------------|
| Regulation 30 (LODR) material event disclosure | `lodr_material_event` |
| SAST Regulations — substantial acquisition | `sast_acquisition` |
| Open offer under SAST | `sast_open_offer` |
| Scheme of arrangement (Sections 230-232 Companies Act) | `scheme_of_arrangement` |
| Quarterly results (unaudited / audited) | `quarterly_results` |
| Board meeting intimation | `board_meeting_intimation` |
| Credit rating change | `credit_rating_change` |
| Loss of significant contract | `contract_loss` |
| Promoter pledge increase/decrease | `promoter_pledge_change` |
| Insolvency proceedings (IBC / NCLT) | `insolvency_proceeding` |
| Buyback offer | `buyback_offer` |

## 3. Signal filters (Stage 1 triage)

- Main board only (BSE and NSE main), not SME or Emerge platforms.
- Ticker + `.BO` (BSE) or `.NS` (NSE) resolves via yfinance. Prefer NSE quote for liquidity.
- Market cap ≥ USD $300M.
- Dedup BSE vs. NSE filings of same event at `issuer_figi` level.

## 4. Entity resolution (D-003)

OpenFIGI:
- BSE: `{"idType": "TICKER", "idValue": "<scrip code>", "micCode": "XBOM"}`
- NSE: `{"idType": "TICKER", "idValue": "<symbol>", "micCode": "XNSE"}`

Both resolve to the same `issuer_figi` — this is the dedup key.

Cross-listing awareness: ADRs (Infosys, Wipro, HDFC Bank). GDRs on LSE for several issuers. Flag `cross_listed_on`.

## 5. Signal output

Standard schema. `translation_confidence = "n/a"`. `raw_data.filing_exchange` = `BSE` or `NSE`, `raw_data.lodr_regulation_section` where applicable.

## 6. Deep dive checklist

- For SAST open offers: offer price vs. market, acquirer identity (strategic, PE, promoter-group entity), minimum tender, regulatory approvals (CCI, sectoral regulators).
- For promoter pledge changes: rapid pledge increases are often stress signals; decreases near zero often bullish.
- For insolvency: NCLT admission date, resolution professional identity, expected resolution plan voting timeline, committee of creditors composition.
- For Reg 30 material events: parse the specific sub-regulation to gauge materiality.
- Web research layer: Economic Times, Mint, Business Standard, Moneycontrol, BQ Prime.
- Sectoral regulators: RBI (banking), IRDAI (insurance), TRAI (telecom), SEBI (securities) can each impose catalyst-shifting actions.

## 7. Known risks

- **High filing volume.** India has ~5,000 listed companies; daily filings can exceed 2,000. Aggressive triage needed.
- **Promoter-led governance risk.** Promoter (founding family) actions drive many Indian catalysts; opacity of promoter intent is a standing risk.
- **Rupee volatility.** Market cap filter in USD must use same-day INR/USD rate.
- **Settlement cycle.** India's T+1 settlement introduces different liquidity dynamics than T+2 markets.

## 8. Tool file

`tools/bse_nse_scanner.py` — Phase 7. Single scanner covers both exchanges with dedup.
