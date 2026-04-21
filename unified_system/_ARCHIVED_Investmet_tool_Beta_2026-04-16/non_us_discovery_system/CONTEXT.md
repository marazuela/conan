# CONTEXT — Non-US Primary-Source Discovery System (Tool 2)

> Only read this if INSTRUCTIONS.md doesn't have the detail you need.

---

## Strategy Selection Rationale

The nine exchanges were chosen to maximize universe coverage while keeping the build tractable:

- **UK LSE RNS**: most transparent corporate-action regime globally; Rule 2.7 takeover + TR-1 + 3.1 filings are high-signal.
- **Japan TDnet**: ~3,800-name universe with near-zero English research coverage; Tanshin material-fact filings are the daily signal heartbeat.
- **Australia ASX**: English-language, high-signal regime; Appendix 4C cash-flow filings flag distress in small-caps early.
- **Canada SEDAR+**: NI 43-101 technical reports for Canada's vast mining universe are free, binary, and institution-tracked.
- **Hong Kong HKEx**: mandatory profit warnings + connected-transaction disclosures; English-required regime.
- **Korea KIND**: chaebol-aware corporate governance events; Korean-language barrier is the moat.
- **India BSE/NSE**: SEBI Regulation 30 material disclosures; SAST 5%+ shareholder filings; promoter pledge disclosures.
- **Brazil CVM**: CVM Resolution 44 material facts; Portuguese-language barrier is the moat.
- **Mexico BMV**: Spanish-language material facts; completes LatAm coverage.

Excluded from initial scope: continental European exchanges (Frankfurt, Paris, Amsterdam, Madrid) because Tool 1 already reads EU short positions and many EU companies cross-list on LSE with RNS coverage. Singapore, Taiwan, Thailand, Indonesia, South Africa — deferred pending evaluation after Phase 10.

---

## API & Data Source Reference — TO BE VALIDATED PER PHASE

Endpoint validation is the first step of every phase. The table below is a planning starting point; entries are upgraded to `✅ VERIFIED` only after live probes from sandbox. Entries remain `⚠️ UNVERIFIED` until then.

### Primary endpoints (planned, to verify)

| # | Exchange | Endpoint (planned) | Auth | Status |
|---|----------|-------------------|------|--------|
| 1 | LSE RNS | `londonstockexchange.com/news` + RSS / API | None expected | ⚠️ UNVERIFIED |
| 2 | TDnet | `www.release.tdnet.info/inbs/I_list_001_001.html` | None | ⚠️ UNVERIFIED |
| 3 | ASX | `www2.asx.com.au/asx/statistics/announcements.do` + ASX RSS | None | ⚠️ UNVERIFIED |
| 4 | SEDAR+ | `sedarplus.ca` + `ceto.sedar.com` | None | ⚠️ UNVERIFIED |
| 5 | HKEx | `www1.hkexnews.hk/ncms/search/advanced.html` | None | ⚠️ UNVERIFIED |
| 6 | KIND | `kind.krx.co.kr` | None (rate-limit sensitive) | ⚠️ UNVERIFIED |
| 7 | BSE India | `www.bseindia.com/corporates/ann.html` | None | ⚠️ UNVERIFIED |
| 7 | NSE India | `www.nseindia.com/companies-listing/corporate-filings-announcements` | None (needs headers) | ⚠️ UNVERIFIED |
| 8 | CVM | `sistemas.cvm.gov.br` + `www.rad.cvm.gov.br/ENET/` | None | ⚠️ UNVERIFIED |
| 9 | BMV | `www.bmv.com.mx/en/markets/emisnet` | None | ⚠️ UNVERIFIED |

### Support endpoints (inherited from Tool 1, already verified there)

| Source | Endpoint | Auth | Status |
|--------|----------|------|--------|
| OpenFIGI v3 | `api.openfigi.com/v3/mapping` | None | ✅ VERIFIED (from Tool 1) |
| Yahoo Finance (yfinance library) | Python `yfinance` package | None | ✅ VERIFIED (from Tool 1) |

Exchange suffix convention for yfinance market-cap lookups:
- UK: `.L`
- Japan: `.T`
- Australia: `.AX`
- Canada: `.TO` (TSX) or `.V` (TSXV)
- Hong Kong: `.HK`
- Korea: `.KS` (KOSPI) or `.KQ` (KOSDAQ)
- India: `.BO` (BSE) or `.NS` (NSE)
- Brazil: `.SA`
- Mexico: `.MX`

---

## Scoring Quick Reference

**7 dimensions**: Signal Strength (×2), Catalyst Clarity (×1), Info Asymmetry (×1.5), Risk/Reward (×1), Edge Decay (×1), Liquidity (×1), Catalyst Timeline (×1).

Max: 42.5 | Convergence bonus: +4 (2 strategies), +8 (3+) | **28+ Immediate, 22–27 Watch, 14–21 Archive, <14 Discard**

Full rubric: `framework/scoring_system.md`.

---

## Entity Resolution Protocol

1. Every signal carries `ticker_local` + `mic`. These are the primary keys.
2. OpenFIGI call: `POST api.openfigi.com/v3/mapping` with `[{"idType": "TICKER", "idValue": "7203", "micCode": "XTKS"}]` → returns FIGI.
3. Roll up to `issuer_figi` (composite FIGI) for cross-listing awareness.
4. Convergence keys on `issuer_figi`.
5. If OpenFIGI cannot resolve, log to `working/unresolved_entities.md` and exclude from convergence.
6. Never key convergence on local-language company name. Never.

MIC codes for the 9 exchanges:
- LSE: `XLON`
- TDnet/TSE: `XTKS`
- ASX: `XASX`
- TSX: `XTSE`, TSXV: `XTSX`
- HKEx: `XHKG`
- KRX: `XKRX` (KOSPI), `XKOS` (KOSDAQ)
- BSE: `XBOM`, NSE: `XNSE`
- B3 (Brazil): `BVMF`
- BMV: `XMEX`

---

## Translation Confidence Convention

Non-English scanners produce `translation_confidence` (0.0 – 1.0) estimating how certain the direction extraction is. Applied per-passage for passages that drive `thesis_direction`.

- ≥ 0.85 — direction may be set explicitly.
- 0.70 – 0.85 — direction set to `neutral` or `unknown`.
- < 0.70 — signal emitted but triaged out at Stage 1.

Implementation: in-session Claude translation with an explicit confidence self-assessment per directionally-relevant span.
