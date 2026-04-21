---
name: litigation-deep-dives
description: Deep-dive brief generator for the Litigation & Docket Signal System. Runs every 8 hours. For every candidate in IMMEDIATE band (28+) that does not yet have a full brief, produces a DOCX brief in reporting_layer/litigation_briefs/ with thesis, timeline, catalyst calendar, party-resolution trail, scoring breakdown, source citations.
---

# litigation-deep-dives

> Source-of-truth copy per D-013.

## Schedule

Cron: `30 */8 * * *` (every 8 hours at HH:30).
Write scope: `reporting_layer\litigation_briefs\` (including `index.json`).
Lock: independent — does not contend with operational/maintenance.

## Cold-start protocol

1. Read `SESSION_STATE.md` for phase status.
2. Read `reporting_layer/litigation_briefs/index.json` — contains list of already-generated briefs with their source candidate timestamps.
3. Do NOT touch `litigation_system/` working files except to read from `candidates/`.

## Main task

For every `candidates/candidate_<figi>_<YYYYMMDD>.md` file where:
- Band is IMMEDIATE (score ≥ 28), AND
- No brief exists in `index.json` for this (figi, YYYYMMDD) pair, OR the existing brief is older than the candidate file's last-modified time,

generate `reporting_layer/litigation_briefs/brief_<figi>_<YYYYMMDD>.docx` and update `index.json`.

### Brief sections (follow `framework/candidate_template.md`)

1. Title, issuer, ticker, FIGI, score, band, channels.
2. Thesis (3 sentences max).
3. Catalyst timeline table (date / event / source).
4. Party resolution trail (raw name → resolution path → confidence).
5. Scoring breakdown (7-dim table with rationale per dim + convergence bonus).
6. Source citations — every factual claim cited.
7. Market implication (direction, asymmetry, sizing, horizon, primary risk, kill conditions).
8. Information gaps (PACER pulls requested, unresolved co-defendants, etc.).
9. Recommended action (monitor / pass / size-in).

### Generation toolchain

- `python-docx` for DOCX generation.
- Read the candidate MD file. Parse the structured sections. Render into the DOCX template.
- Include a header/footer with project branding: "Litigation Signal Tool v1 — Internal Research".
- Do NOT convert via a docx→pdf chain (template mandate; PDF deliverables use reportlab directly; DOCX deliverables stop at DOCX).

### File outputs

- Primary: `reporting_layer/litigation_briefs/brief_<figi>_<YYYYMMDD>.docx`.
- Secondary: `reporting_layer/litigation_briefs/index.json` — append brief entry (figi, YYYYMMDD, score, bands, file path, generated_at).

## Budget discipline

Hard cap: generate at most 10 briefs per run. Beyond 10 → flag to `SESSION_STATE.md` for the next run. Keeps the task bounded.

## Non-negotiables

- NEVER write into `litigation_system/` from this task.
- NEVER invent facts. Every factual claim in a brief must trace to a source in the candidate file (docket URL, SEC filing, etc.).
- NEVER omit the party-resolution confidence number — if confidence < 0.95, the brief must explicitly call it out.
- ALWAYS update `index.json` in the same run (don't leave index out of sync).
- ALWAYS use `python-docx` for DOCX; never `docx`-rendered-HTML nor docx→pdf chain.
