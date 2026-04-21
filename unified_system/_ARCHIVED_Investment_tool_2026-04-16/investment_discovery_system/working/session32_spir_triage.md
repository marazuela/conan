# SPIR EDGAR Signal Triage — Session 32 (2026-04-12)

## Signal

- **Ticker**: SPIR (Spire Global, Inc.)
- **Market cap**: $722.3M
- **Filing**: 8-K, items 1.01, 3.02, 7.01, 9.01
- **Date**: 2026-04-08 (effective), filed 2026-04-10
- **Accession**: 0001193125-26-151231
- **Matched keywords**: "poison pill" AND "rights plan"
- **Match location**: EX-10.1 (Securities Purchase Agreement)
- **Strength**: 3 (governance_keyword ×2)

## Initial concern

A mid-cap company ($722M) with a **material 8-K** (item 1.01 + item 3.02) mentioning poison pill language in an EX-10.1 exhibit looks like it could be one of:
  1. Actual adoption of a shareholder rights plan (takeover defense)
  2. Activist-induced response
  3. Private placement triggering Rule 13D thresholds

## Verification (sources: EDGAR direct)

### Main 8-K (spir-20260408.htm)

- **Item 1.01**: "entered into a securities purchase agreement ... for the private placement ... of 5,000,000 shares"
- **Item 3.02**: "The disclosures set forth in Item 1.01 above are incorporated by reference"
- **Item 7.01**: Press release announcing the Private Placement (Apr 9)
- **Item 9.01 Exhibits**:
  - 10.1 = **Securities Purchase Agreement**, Apr 8 2026
  - 10.2 = **Registration Rights Agreement**, Apr 8 2026
  - 99.1 = Press Release
- **Registration statement commitment**: file by Apr 23, effective by May 8

**This is a standard PIPE (private placement).** Not a poison pill adoption. Not activist response. Not takeover defense.

### EX-10.1 — where the keywords matched

Two locations:
1. Position ~61104 (char): Company representation/warranty section — Company states it has taken action "in order to render inapplicable any control share acquisition, business combination, **poison pill** (including any distribution under a rights agreement) or other similar anti-takeover provision ... that is or could become applicable to the Purchasers"
2. Position ~110701 (char): Section 4.5 titled **"Shareholder Rights Plan"** — Company agrees no claim will be made that any Purchaser is an "Acquiring Person" under "any control share acquisition, business combination, **poison pill** (including any distribution under a rights agreement) or similar anti-takeover plan or arrangement"

**Both references are standard PIPE-SPA boilerplate where the Company WAIVES any poison pill / anti-takeover provision AGAINST the investors.** This is the OPPOSITE of a poison pill adoption — it is a negotiated safe harbor for the PIPE purchasers so the financing doesn't accidentally trigger takeover defenses when they accumulate shares.

## Ruling

**HARD FALSE POSITIVE. Archive SPIR. No signal.**

Dilutive capital raise (5M shares = ~7% dilution at typical SPIR share count). Bearish-neutral, not activist-bullish. Scoring would be negative on Signal Strength after this interpretation. Below candidate floor.

## Framework refinement — D-029 new variant

This reveals a NEW false-positive sub-pattern:

> **D-029-B: PIPE-SPA anti-takeover waiver boilerplate false positive**
>
> When a company announces a private placement (8-K items 1.01 + 3.02), the accompanying Securities Purchase Agreement exhibit will routinely include a section (often titled "Shareholder Rights Plan" or "Anti-Takeover Provisions") where the Company waives applicability of existing or future poison pills against the PIPE purchasers. This language contains both "poison pill" and "rights plan" (and often "rights agreement") in close proximity.
>
> **Detection heuristic**: If an EDGAR match for poison pill/rights plan has ALL of:
>  - Filing type = 8-K
>  - Item set includes 1.01 AND 3.02 (material agreement + unregistered securities)
>  - Match location = EX-10.1 or similar exhibit
>  - Context phrase within 500 chars contains "Purchaser" OR "Private Placement" OR "Securities Purchase Agreement"
>
> **→ Automatic archive as D-029-B false positive.**
>
> **This is distinct from D-029-A** (routine 10-K / DEF 14A proxy boilerplate where companies routinely discuss their existing or contingent rights plan).

## Other S32 EDGAR D-029-A archives (routine proxy boilerplate)

All below are DEF 14A / PRE 14A (annual meeting proxies) with routine rights plan disclosure — mega-cap companies where this is legal boilerplate:

| Ticker | MCap | Filing | Status |
|--------|------|--------|--------|
| VRSN | $24.1B | DEF 14A | Archive (D-029-A mega-cap proxy boilerplate) |
| BKNG | $137.4B | PRE 14A | Archive (D-029-A mega-cap proxy boilerplate) |
| BSY | $9.5B | DEF 14A | Archive (D-029-A mid-cap proxy boilerplate) |
| AAT | $1.5B | DEF 14A | Archive (D-029-A small-cap REIT proxy boilerplate) |
| WELL | $144.8B | DEF 14A | Archive (D-029-A mega-cap proxy boilerplate) |

## D-029-B formalization recommendation

Add to `tools/edgar_filing_monitor.py` keyword filter:

```python
def _is_pipe_spa_boilerplate(raw_data):
    """D-029-B: PIPE-SPA anti-takeover waiver FP detector."""
    if raw_data.get('filing_type') != '8-K':
        return False
    desc = (raw_data.get('file_description') or '').upper()
    if 'EX-10' not in desc and 'EX10' not in desc:
        return False
    # Optionally: fetch 8-K items or check companion doc 3.02
    return True  # provisional; refine with passage context if available
```

This is a DRAFT rule — needs one more observation before DECISIONS.md entry. Log in OPEN_QUESTIONS as framework refinement candidate.

## References

- Main 8-K: https://www.sec.gov/Archives/edgar/data/1816017/000119312526151231/spir-20260408.htm
- EX-10.1: https://www.sec.gov/Archives/edgar/data/1816017/000119312526151231/spir-ex10_1.htm
- EDGAR index: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001816017&type=8-K
