# D-036 Evidence Log

Per Q-013, each observation of a pre-catalyst ≥10-15% single-day move near a hard catalyst is logged here. Purpose: accumulate 2-3 more observations after the initial REPL case so the rule can be formalized in DECISIONS.md.

Columns: ticker | catalyst | cat date | T-N | pre-move (%) | vol ratio | news? | outcome | signal correct?

## Observations

### 1. REPL (Reference case — confirming, SHORT)
- Ticker: REPL
- Catalyst: PDUFA
- Catalyst date: 2026-04-10
- T-N at signal: T-2 (Apr 8)
- Pre-move: -24.6%
- Volume ratio: 2.0×
- Discrete news trigger: No
- Eventual outcome: CRL (Apr 10) + SECOND CRL later
- Signal direction correct? YES (bearish pre-move → CRL)

### 2. AXSM (weaker form — long)
- Ticker: AXSM
- Catalyst: ADA sNDA PDUFA
- Catalyst date: 2026-04-30
- T-N at signal: T-14 (Apr 9)
- Pre-move: +3.3% on 1.85× vol (magnitude below 15% threshold → weaker form)
- Discrete news trigger: No
- Eventual outcome: PENDING
- Signal direction correct?: TBD

### 3. VERA (weaker form, partly news-driven)
- Ticker: VERA
- Catalyst: IgAN BLA
- Catalyst date: 2026-07-07
- T-N: T-62 (Apr 10)
- Pre-move: +10.04% on 2.23× vol
- Discrete news trigger: YES (Wolfe upgrade + $200M investment) → disqualifies per draft rule
- Outcome: PENDING
- Signal direction correct?: TBD

### 4. TVTX (confirming — LONG) — NEW S52
- Ticker: TVTX
- Catalyst: FSGS sNDA PDUFA
- Catalyst date: 2026-04-13
- T-N at signal: T-3 (Apr 8) — UP day while REPL crashed -24.6%
- Pre-move (Apr 8): ~+6% while peer/sympathy name was down -24.6% (relative strength divergence)
- Secondary signal: Apr 13 session close +6.01% on 2.5M vol (elevated, in "no decision" band $28-32)
- Volume ratio (Apr 8): elevated but not ≥1.5× strictly measured
- Discrete news trigger: No (REPL's CRL was unrelated company news, not TVTX-specific)
- Eventual outcome: ✅ APPROVED (2026-04-13 after close) — FILSPARI approved for FSGS without nephrotic syndrome
- Signal direction correct?: **YES** — inverse-sympathy/relative-strength reading was bullish and resolved bullish

## Tally

- Confirming observations: **2** (REPL short, TVTX long)
- Weaker-form pending: 2 (AXSM, VERA)
- Confirming direction accuracy so far: 2/2 (100%)

## Interpretation

TVTX is the first **bullish** resolved observation and it is an inverse-sympathy case (divergence from a peer that cratered on its own catalyst), not a classic single-name breakout. This widens the draft rule:

- The rule should include relative-strength/inverse-sympathy divergence as a valid bullish variant, not only absolute single-day magnitude.
- Magnitude threshold of 15% applied cleanly to REPL but not to TVTX's +6% — what mattered for TVTX was the *spread* vs. peer REPL (~30pp), not the absolute move.
- Proposed refinement (pending S53+ review): add Variant-B to D-036 — "same-session relative-strength spread ≥20pp versus a correlated peer cratering on adverse catalyst, within 5 trading days of own hard catalyst, no idiosyncratic news" → treat as bullish leading signal.

## Next observations

- AXSM (PDUFA 2026-04-30): will resolve bullish/bearish bucket
- VERA (PDUFA 2026-07-07): further out
- Any new ≥10% pre-catalyst moves detected by FDA PDUFA scanner

Last updated: 2026-04-14 S52
