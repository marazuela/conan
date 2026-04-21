# S33 VRDN PDUFA Date Verification + REVEAL-1 Positive Readout

**Date**: 2026-04-12 (S33)
**Ticker**: VRDN — Viridian Therapeutics (CIK 1590750)
**Conflict**: S33 scanner reported PDUFA **2026-06-30**; SESSION_STATE carried **2026-06-12**. Which is correct?

## Verification

Pulled VRDN filings index via `data.sec.gov/submissions/CIK0001590750.json` and reviewed the Mar 30 2026 8-K.

**Authoritative date: 2026-06-30**

Quoted from VRDN 8-K EX-99.1 (March 30, 2026), available at https://www.sec.gov/Archives/edgar/data/1590750/000119312526130433/d27955dex991.htm :

> "Veligrotug on Track with a PDUFA Target Action Date of June 30, 2026 ... The veligrotug BLA is under Priority Review at the FDA with a Prescription Drug User Fee Act (PDUFA) target action date of June 30, 2026."

The SESSION_STATE 06-12 figure was stale. The scanner was correct. Update SESSION_STATE. No framework problem; this is normal date drift as sponsors clarify PDUFA target dates through the review cycle.

## Bonus: Mar 30 Positive Readout for Second Drug (elegrobart / REVEAL-1)

The same 8-K discloses **REVEAL-1 Phase 3 for elegrobart met its primary endpoint in active TED**:

- Q4W proptosis responder rate (PRR) **54%** vs placebo **18%** — highly statistically significant
- Q8W PRR **63%** vs placebo **18%**
- Complete resolution of diplopia in **51%** Q4W vs **16%** placebo at week 24
- Generally well tolerated, low rates of hearing impairment
- REVEAL-2 (chronic TED) on track for topline Q2 2026
- BLA submission for elegrobart anticipated Q1 2027

**Interpretation for existing VRDN candidate score**:
- Platform de-risking: VRDN now has **two positive Phase 3 programs** (veligrotug THRIVE/THRIVE-2 already positive + elegrobart REVEAL-1 positive)
- Hearing impairment is the key differentiator from teprotumumab (Tepezza, Amgen) — VRDN flagged "low rates" which is the competitive angle
- Pipeline breadth reduces single-catalyst risk: even if veligrotug hits unexpected obstacles on 6/30, elegrobart BLA Q1 2027 becomes the next anchor
- $875M cash reported Q4 2025 → well-funded through commercial launch

**Score impact**: Modest positive. The veligrotug PDUFA is still the primary binary catalyst, but Signal Strength can tick up slightly on platform validation. Holding VRDN at 30.00 pending S34 deeper review of veligrotug BLA status, manufacturing readiness, and updated competitive position vs Tepezza (AMGN).

## Action Items for S34+

1. Update SESSION_STATE.md VRDN PDUFA date: 2026-06-12 → **2026-06-30**
2. Pull the full 8-K (and Corporate Presentation EX-99.2) for any additional disclosures
3. Review 10-K (Feb 26 2026) for manufacturing, supply chain, launch readiness
4. Check if VRDN candidate file exists in `candidates/`; if yes update, if no create
5. Monitor VRDN tape Apr 13+ — Mar 30 positive readout should have produced an up-move; verify it was received well

## Sources

- VRDN EDGAR submissions feed: https://data.sec.gov/submissions/CIK0001590750.json (verified S33)
- Mar 30 8-K Item 7.01: https://www.sec.gov/Archives/edgar/data/1590750/000119312526130433/d27955d8k.htm
- Mar 30 Press Release EX-99.1 (authoritative PDUFA quote): https://www.sec.gov/Archives/edgar/data/1590750/000119312526130433/d27955dex991.htm
