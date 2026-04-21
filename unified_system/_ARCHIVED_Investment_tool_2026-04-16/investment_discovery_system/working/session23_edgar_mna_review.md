# EDGAR M&A Rotation Review — Session 23

**Category**: mna (manually triggered, bypassing rotation to avoid activist/governance proxy-season blindness — D-030)
**Window**: 2 days back (Apr 8–10)
**Signals returned**: 38 raw (no mcap filter)

## Finding: SAME FORM-WHITELIST FAILURE MODE AS DISTRESS (D-031)

Same contamination pattern observed in Session 22's distress scan. Raw signals are:
1. **SPAC / de-SPAC boilerplate** — S-4/A proxies (SOAR x3, CSTAF), DEFM14A (WLAC), 425 communications (APUS), SC TO-C (ASRT, FORA). The "merger agreement" and "fairness opinion" keywords fire legitimately on these forms, but they're proxy/registration boilerplate, not new business signal.
2. **Routine annual reports** — 20-F (DLNG, LNG partners annual report), 10-K (IXAQF — SPAC annual), 424B5 (PLRZ — routine prospectus supplement).
3. **Pre-IPO S-1** — ALBT (Avalon GloboCare).

## Candidates that would be interesting IF they passed gates: 0

The only signals that look like "real" M&A rather than SPAC/boilerplate noise are:

| Ticker | Company | Form | Signal | Mcap | Pass floor? |
|--------|---------|------|--------|------|-------------|
| ASRT | Assertio Holdings | 8-K / SC TO-C | Active tender offer situation | **$116M** | ❌ below $215M |
| FORA | Forian | 8-K / SC TO-C | Tender offer | **$67M** | ❌ below $215M |
| OMEX | Odyssey Marine | 8-K | Merger agreement | **$59M** | ❌ below $215M |
| HURA | TuHURA Biosciences | 8-K | Merger agreement | **$125M** | ❌ below $215M |

**All four fail the $215M market cap floor.** ASRT is closest (54% of floor) and would be worth monitoring if it re-rates higher on deal structure, but currently not in the candidate universe.

## D-031 Extension

The form-whitelist fix proposed in Session 22 for the `distress` category should also be applied to `mna`:
- **Whitelist**: 10-K, 10-Q, 8-K (real corporate events)
- **Blacklist forms**: S-4, S-4/A, DEFM14A, DEFM14C, 425, SC TO-C, SC TO-I, SC TO-T, 424B*, S-1, S-1/A
- **Rationale**: Merger agreements, tender offers, and fairness opinions legitimately appear in these excluded forms but as pre-existing deal disclosures, not new signal.

**Additional fix for M&A specifically**: even with the form whitelist, the M&A category will typically return small/micro-cap consolidation noise because most deal activity happens below mid-cap. Consider raising the mcap floor for this category to $500M (same as proposed for distress).

## Q-010 Expansion

Extend Q-010 to cover both distress and M&A categories. Expected reduction: ~95% of signals dropped, ~2-5% survive to deep dive vs current ~5% (none of which are actionable).

## Decision

**M&A rotation produces 0 candidates** for the Apr 8–10 window. No candidate writeups triggered. Rotation state left at `distress` (index 2) so next auto-rotate goes to `governance` (deprioritized per D-030 but will still execute dedup) → then activist (blind) → then m_and_a. For next session, skip directly to m_and_a again manually, or implement the form-whitelist fix.

## Action item for next build session (non-blocking)

Implement form whitelist in `tools/edgar_filing_monitor.py:scan_keywords()`:
```python
# After EFTS query, filter hits:
ALLOWED_FORMS = {"10-K", "10-Q", "8-K", "10-K/A", "10-Q/A", "8-K/A"}
EXCLUDED_FORMS_CONTAIN = ["S-4", "DEFM14", "425", "SC TO", "424B", "S-1"]
```
