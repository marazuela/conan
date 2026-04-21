# Context — Supplemental Reference

> Only read this if INSTRUCTIONS.md doesn't have the detail you need.

---

## Strategy Selection Rationale

Selected from 19-idea longlist via three hard filters:
1. Claude can execute autonomously — free APIs, no auth walls, no CAPTCHA
2. Data is legally public — no proprietary licenses
3. Edge is structural (not temporarily unnoticed) — information asymmetry persists

**EDGAR**: Full-text scan of entire filing universe (not just watchlist) catches signals on companies no one follows yet.
**ESMA shorts**: No aggregated EU short position database exists publicly. Scraping national regulators creates a unique dataset.
**Congressional**: Academic evidence of 4–8% annual alpha on committee-aligned trades. 45-day delay still actionable for multi-week holds.
**Contract awards**: Awards published 1–3 days before company press releases. Systematic scan catches material revenue events early.
**FDA PDUFA**: Binary events with known dates. Edge in mispriced implied vol and neglected small-cap biotechs.

---

## API & Data Source Reference (Validated from Cowork Sandbox — April 9, 2026)

### Fully Verified (200 OK, returns data)

| Source | Endpoint | Auth | Status |
|--------|----------|------|--------|
| SEC EDGAR EFTS | `efts.sec.gov/LATEST/search-index` | User-Agent (email) | Full-text search, 100 hits returned per query |
| SEC data.sec.gov | `data.sec.gov/submissions/CIK{CIK}.json` | User-Agent (email) | Company metadata, filing history |
| OpenFIGI v3 | `api.openfigi.com/v3/mapping` | None | Entity resolution (ticker/ISIN/CUSIP). v2 sunsets July 1, 2026 |
| ClinicalTrials.gov | `clinicaltrials.gov/api/v2/studies` | None | Trial status, phases, endpoints, results |
| Yahoo Finance (yfinance) | `pip install yfinance` Python library | None | Market cap, volume, price, revenue. Works for US and UK (.L suffix) stocks. v7/v10 REST APIs now require auth — use library only |
| Capitol Trades | `capitoltrades.com/trades` | None | **PRIMARY** for congressional trading — HTML scraping, recent trades with politician, ticker, size, dates |
| openFDA | `api.fda.gov/drug/drugsfda.json` | None | Drug approval history, application numbers, submission dates |
| USAspending.gov | `api.usaspending.gov/api/v2/search/spending_by_award/` | None | Federal contract awards, POST with filters |
| FCA UK shorts | `fca.org.uk/publication/data/short-positions-daily-update.xlsx` | None | 579 positions, daily XLSX download |
| Bundesanzeiger | `bundesanzeiger.de/pub/en/nlp?1` | None | German shorts, HTML (needs dynamic parsing) |
| Quiver Quantitative | `api.quiverquant.com/beta/live/congresstrading` | **Now requires auth** | Was free, now 401. Replaced by Capitol Trades scraping (see D-013) |
| House disclosures | `disclosures-clerk.house.gov/FinancialDisclosure` | None | Raw House disclosure data |

### Blocked / Inaccessible from Sandbox

| Source | Issue | Workaround |
|--------|-------|------------|
| Lambda Finance API | DNS blocked | Replaced by Quiver Quantitative |
| CNMV (Spain) | 403 Forbidden | Defer to Phase 3; use browser automation or manual |
| Quiver Quantitative API | Now requires auth (was free) | Replaced by Capitol Trades HTML scraping |
| Senate disclosures | 403 Forbidden | Not needed — Capitol Trades covers both chambers |
| SAM.gov API | Requires key + blocked | Replaced by USAspending.gov |
| FPDS Atom feed | Decommissioned Feb 2026 | Replaced by USAspending.gov |
| Yahoo Finance v7/v10 REST | 401 Unauthorized (now requires auth) | Use yfinance Python library instead |
| FDA.gov PDUFA pages | 404 Not Found (2026 pages may not exist) | Use openFDA API + WebSearch for PDUFA dates |
| biopharmawatch.com | JS-rendered, no data in HTML | Not usable from sandbox; use manual/WebSearch |
| congress.gov API | Slow, XML-only, unreliable for bulk lookups | Build static committee-member lookup table |

---

## Scoring Quick Reference

**7 dimensions**: Signal Strength (×2), Catalyst Clarity (×1), Info Asymmetry (×1.5), Risk/Reward (×1), Edge Decay (×1), Liquidity (×1), Catalyst Timeline (×1)

Max: 42.5 | Convergence bonus: +4 (2 strategies), +8 (3+) | **30+ = Immediate, 22–29 = Watch, 14–21 = Archive, <14 = Discard**

Full rubric: `framework/scoring_system.md`
