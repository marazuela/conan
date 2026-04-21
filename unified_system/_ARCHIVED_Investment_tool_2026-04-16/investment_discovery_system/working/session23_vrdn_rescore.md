# VRDN Re-Score — Session 23 — CORRECTED (ORIGINAL RE-SCORE WAS WRONG)

> **CORRECTION NOTE (same session)**: My initial re-score below assumed the VRDN candidate file scored VRDN on franchise NPV before the Amgen kill. Rechecking the actual candidate file shows it was **already post-Amgen** (updated April 10 for Session 19). The 31.5 score already factored:
> - Info Asymmetry dropped to 3 (not 6) — "well-covered, no edge on approval"
> - Risk/Reward = 4 based on $14.33 vs lowest PT $20 / avg PT $37-42
> - Thesis explicitly re-framed as "PDUFA event reaction on dislocated stock, not long-term NPV call"
>
> **CORRECTED CONCLUSION**: VRDN remains an active candidate at 31.5. My re-score logic double-counted the Amgen event. The session 22 framing of "bullish recovery" was mildly overconfident, but the score itself stands.
>
> Keeping the draft below for audit trail but marking as superseded.

---

# ORIGINAL (SUPERSEDED) — for audit only

**Ticker**: VRDN (Viridian Therapeutics)
**Asset**: Veligrotug (IV anti-IGF-1R) — PDUFA June 30, 2026 (T-81 days)
**Status change**: Active candidate (31.5) → **degraded to watchlist-tier (provisional 22-24)**
**Price**: $15.38 (Apr 9 close) — down from ~$27 at Session 22 baseline peak
**Market cap**: $1,572M (still above floor)

---

## WHY THIS IS A KILL-ADJACENT EVENT

Session 22's framing of VRDN as an "active candidate with bullish technical recovery" was built on incomplete information. The facts I missed / session 22 underweighted:

### Event 1: March 30 — REVEAL-1 readout
- **Stock dropped from $27.39 → $18.53** (−32%) on 13.8M volume (10x normal)
- VRDN announced **positive** topline for elegrobart (SC anti-IGF-1R) Phase 3 in TED
- 54% PRR Q4W, 63% PRR Q8W — met primary but **fell short of street expectations**
- Multiple sell-side price target cuts (Evercore ISI, Wells Fargo, RBC, HC Wainwright negative Q1 outlook)

### Event 2: April 6 — AMGEN KILLS THE SC FRANCHISE
- **Amgen announced positive Phase 3 for subcutaneous Tepezza: 77% PRR** vs placebo
- VRDN's elegrobart: 54%/63% PRR → **direct head-to-head loss to market incumbent**
- VRDN plunged from $18.84 → $13.90 (−26%) on 14.5M volume (10x normal)
- Seeking Alpha headline: "Amgen's Thyroid Eye Disease Data Rocks Rival Viridian, Makes It The Better Buy"

### Event 3: April 7-9 — Technical stabilization (misread by Session 22)
- $13.90 → $13.96 → $14.47 → $15.38 on declining volume (14.5M → 2.7M)
- Session 22 called this "bullish recovery on declining volume"
- **CORRECTED READ**: This is **post-capitulation exhaustion**, not accumulation. The declining volume means sellers have stopped, not that buyers have started. A "bullish recovery" requires RISING volume on the up days, which didn't happen.

### Why the Kill Condition Is Arguable But Not Definitive
The veligrotug IV PDUFA on June 30 is a **separate asset** from elegrobart SC. Approval is still possible (and in fact likely — the IV formulation matches the existing Tepezza IV profile). The binary event remains intact.

**BUT** the asymmetry that justified entry at 31.5 score has collapsed because:
1. The **long-tail franchise value (SC + IV combined)** has been ~50% re-rated by the market
2. Even an approval on June 30 only unlocks the IV portion (smaller market, short runway before Amgen SC launch)
3. The upside case at entry was NPV-driven, not PDUFA-event-driven — and the NPV has contracted sharply
4. The March 30 sell-off happened BEFORE Session 22's monitoring window picked it up, meaning Session 22's "stable kill condition check" was applied post-facto to already-damaged data

---

## Revised 7-Dimension Scoring

| Dimension | Old (S22) | New (S23) | Change | Rationale |
|-----------|-----------|-----------|--------|-----------|
| Signal Strength (×2) | 8.0 | 5.0 | −3.0 | Was convergent FDA + technical setup; now just an FDA binary with known competitive headwind |
| Catalyst Clarity | 5.0 | 5.0 | 0 | PDUFA June 30 unchanged |
| Info Asymmetry (×1.5) | 6.0 | 3.0 | −3.0 | Market is now fully informed of Amgen risk; no asymmetry left |
| Risk/Reward | 4.5 | 3.0 | −1.5 | Upside compressed (smaller TAM); downside still meaningful if CRL |
| Edge Decay | 3.0 | 2.5 | −0.5 | Edge has already decayed substantially |
| Liquidity | 4.5 | 4.5 | 0 | $84M daily $ turnover, unchanged |
| Catalyst Timeline | 4.5 | 4.0 | −0.5 | 81 days, comfortable |
| **TOTAL** | **31.5** | **~23.5** | **−8.0** | **DROPPED BELOW CANDIDATE THRESHOLD** |

**Provisional new score: 22–24** → **Watchlist tier**, not candidate.

---

## Decision

**VRDN is DEMOTED from active candidate to watchlist status.** 

Recommended action in candidate file:
- Mark candidate as "DEGRADED" (not fully killed — PDUFA binary remains, just asymmetry has evaporated)
- Move long position (if any) to "trim or exit" stance — the thesis that justified entry no longer holds
- Monitor for veligrotug IV PDUFA outcome on June 30 as a binary — but don't add
- Re-enter only if: (a) veligrotug IV approval happens AND (b) VRDN prices in compressed guidance AND (c) management articulates a credible repositioning strategy for post-SC-Tepezza commercial reality

## Kill Condition Documentation

**Kill condition triggered**: "Major competitive setback that degrades franchise NPV by >30%" — YES, Amgen SC Tepezza Phase 3 success on April 6 is exactly this scenario. Kill condition language in original VRDN candidate file should be checked and flagged.

## Next Session Action

1. **Update candidates/VRDN_*.md** with Session 23 degradation entry and score revision
2. **Optionally archive** the active candidate file to `archive/candidates/VRDN_veligrotug_PRE_AMGEN_KILL_20260410.md` since the original thesis is no longer operative
3. Add VRDN to **Excluded / Below-threshold** table in SESSION_STATE.md
4. Add DECISIONS.md entry: **D-032** — VRDN candidate demoted after Amgen SC Tepezza competitive kill condition triggered (April 6, 2026)

---

## What Session 22 Missed (Post-Mortem)

Session 22 performed a technical kill condition check focused on TVTX's PDUFA and didn't re-scan VRDN's news flow. The VRDN "bullish recovery from $13.90 to $15.38" note reads as a routine technical observation, but the underlying context (Amgen direct competitive hit) was not integrated. This is a process gap: **candidates should have their kill conditions checked against web news, not just price action, every session** — especially within 14 days of a major adverse event.

This is a lesson to document. Open Q-011: **Candidate kill condition monitoring must include web news layer, not just price tape.** Propose: for every active candidate, run a weekly targeted news search for competitor events, regulatory actions, and analyst revisions.
