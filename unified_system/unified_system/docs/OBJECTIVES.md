# Objectives — Unified Investment Discovery System

## Mandate

Surface 2–5 high-conviction, actionable investment opportunities per week by scanning primary-source regulatory, legal, and positioning data across 13 global markets + US litigation. Every opportunity must survive triage → scoring → deep-dive → kill-condition gating.

## What "Actionable" Means

- Tradable at $3M+ position size without crushing the spread (liquidity gate).
- Clear catalyst with a defined timeline (binary date, process milestone, or narrow window).
- Explicit kill conditions — you know when you're wrong.
- Primary-source thesis — no pundit takes, no secondary reporting as the load-bearing claim.

## Geographic Coverage

**Operational**:
- US (EDGAR, ESMA cross-listed, Congressional, FDA)
- UK (LSE RNS)
- Japan (TDnet)
- Australia (ASX)
- EU multi-regulator short data (FCA, AMF, AFM, BaFin, CNMV, CONSOB)

**Blocked / in-build**:
- Canada (SEDAR+) — blocked on ca_universe.json
- Hong Kong (HKEx) — planned
- Korea (KIND) — planned
- India (BSE / NSE) — planned
- Brazil (CVM) — planned
- Mexico (BMV) — planned

## Signal Type Coverage

- Merger arbitrage (announced deals with spread)
- Activist & governance (13D, proxy fights, poison pills, cooperation agreements)
- Binary catalysts (FDA PDUFA, clinical readouts, regulatory decisions)
- Short positioning / flow signals (ESMA short book, insider clusters)
- Litigation (federal civil class actions, SEC enforcement, Delaware Chancery, antitrust)

## Deliverables

1. **Daily signal digest PDF** — every 4 hours. Highest-priority findings, new signals, candidate status changes, convergence alerts, catalyst calendar.
2. **Per-candidate dossier PDF** — generated on new/updated candidate. Full deep-dive with evidence labels.
3. **Weekly strategic report PDF** — Sundays. Scanner health trends, pipeline metrics, hit rates, coverage gaps.
4. **Machine-readable candidates_index.json** — registry of all active candidates.

## Success Criteria

- 16+ scanners feeding one unified pipeline.
- 3 scheduled tasks (down from 10) with no lock collisions.
- Daily digest PDFs produced automatically.
- Cross-scanner convergence detection working across all signal types + geographies.
- 5 scoring profiles producing accurate, profile-appropriate scores.
- Separation of concerns: operational tasks write operational data; reporting task reads and writes PDFs only.

## Anti-Goals (what this system does NOT try to do)

- Not a quant factor model. No systematic long-short.
- Not a high-frequency shop. Scanners run every 3 hours, not every 3 seconds.
- Not a research aggregator. This is primary-source only. No Seeking Alpha, no StockTwits, no Twitter sentiment.
- Not a compliance tool. Does not check insider-trading rules, position limits, or regulatory restrictions.

## Operator

Pedro + a Claude Opus 4.7 scheduled task. The scheduled task is a dual-mode operator (operational + maintenance) plus an independent reporting task. Interactive sessions with Pedro happen ad hoc and may also run in this same project folder.
