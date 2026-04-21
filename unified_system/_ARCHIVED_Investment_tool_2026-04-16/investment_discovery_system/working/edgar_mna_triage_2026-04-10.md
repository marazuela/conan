# EDGAR M&A Signal Triage — 2026-04-10

Scanned 30 EDGAR signals from the April 10 rotation run (fairness, change, mna categories).
Of the 30, 15 were M&A signals in the $215M–$5B sweet spot. Triaged below.

## Confirmed Announced Deals (not investable — pricing already reflects deal)

| Ticker | Company | Deal | Price | Status |
|--------|---------|------|-------|--------|
| WSR | Whitestone REIT | Ares $1.7B all-cash, $19/sh (12.2% premium) | Announced Apr 9, 2026 | Deal priced — merger arb only |
| CECO | CECO Environmental | Acquiring Thermon ($2.2B, stock+cash) | Announced earlier, now S-4 filed | CECO is acquirer, stock -23% on news |
| CWAN | Clearwater Analytics | Target in announced deal | DEFM14A filed Apr 8 | Deal priced |

## False Positives (boilerplate matches)

| Ticker | Form | Why False Positive |
|--------|------|--------------------|
| PLSE | 8-K | "Change of control" matched exec appointment boilerplate (nothing M&A-related) |
| PHIN | DEF 14A | Proxy routinely discloses change-of-control provisions in exec comp; not deal activity |
| IVA | 20-F | Annual report boilerplate; no deal announcement |
| HOG | DEF 14A | Same — proxy boilerplate |
| PRA | 10-K/A | Amended annual report boilerplate |
| SM | DEF 14A | Proxy boilerplate |
| CGAU | 6-K | Foreign issuer form, routine |
| HIG | DEF 14A | Insurance giant, proxy boilerplate |
| EXPE | 424B2 | Debt offering prospectus boilerplate |

## Worth Further Investigation (none elevated to candidate)

| Ticker | Company | Filing | Notes |
|--------|---------|--------|-------|
| CMRC | Commerce.com | 425 tender offer | $220M cap — smallest in set, at the floor. Tender offer = legitimate M&A event. Needs follow-up to check whether this is the target or acquirer, and whether the deal is already announced. |
| APAD | AParadise Acquisition | S-4/A | SPAC merger amendment — SPACs are generally poor fit for our framework (high deal failure, manipulation risk) |
| CTGO | Contango Silver | 8-K/A | Mining SPAC/reverse merger area — low priority |
| NUAI | New ERA Energy | 8-K | $264M, energy/digital — low priority, speculative |

## Conclusion

**Zero candidates elevated from today's EDGAR M&A rotation.** The strongest signals (WSR, CECO, CWAN) are all confirmed-announced deals where pricing has already absorbed the catalyst. No discoverable "pre-announcement M&A leak" pattern in today's rotation.

## Signal Quality Observation

Approximately 60-70% of today's M&A hits were boilerplate noise from proxies, annual reports, and prospectuses. The EDGAR tool could be improved by:
1. Suppressing `change of control` matches in DEF 14A / 10-K / 10-K/A / 20-F filings (these almost always match exec comp clauses)
2. Suppressing `fairness opinion` matches in 424B* prospectuses (these reference the broader opinion landscape)
3. Weighting 8-K and 425 (tender offer) filings higher — those are real-time deal announcements

**Filing type → signal strength rubric proposal**:
- 425 / SC 13D / 8-K with fresh date → strength 4 (high priority)
- S-4 / DEFM14A / PREM14A → strength 3 (deal in progress, already known)
- DEF 14A / 10-K / 20-F / 424B* → strength 1 (likely boilerplate, needs extra keyword specificity)

Logged as tool improvement opportunity in OPEN_QUESTIONS.md.

---

*Triaged during scheduled session 19 — 2026-04-10 03:40 UTC*
