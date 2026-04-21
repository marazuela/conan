# Litigation & Docket Signal System (Tool 3) — Cold-Start Entry Point

This folder holds an autonomous multi-session Claude project. If you are a new session entering this project, STOP and read in order:

1. **`SESSION_LOCK.md`** — is another session already holding the lock? If LOCKED and less than 4h old, exit. If stale (>4h) or UNLOCKED, proceed.
2. **`SESSION_STATE.md`** — where did the last session leave things? What is the next priority?
3. **`PROGRESS_LOG.md`** — what happened in prior sessions (tail only)?
4. **`INSTRUCTIONS.md`** — HOW this system runs: cold-start, pipeline, execution model, scheduled tasks.
5. **`PROJECT_INSTRUCTIONS.md`** — discipline rules, self-review checklist, standing question.
6. **`OBJECTIVES.md`** — WHAT this system is for.
7. **`CONTEXT.md`** — endpoints, schema, entity-resolution protocol. Read only sections you need.
8. **`DECISIONS.md`** — settled decisions. Do not re-litigate; override by new numbered decision.
9. **`OPEN_QUESTIONS.md`** — what's unresolved. Never ask in chat during scheduled runs — append here.
10. **`INDEX.md`** — full map of every file in the project.

## What this tool does

Scans six US legal-docket channels (PACER/RECAP federal civil, ITC Section 337, PTAB IPR, Delaware Chancery, SEC Enforcement, DOJ/FTC Antitrust) for filings that move publicly-traded equities before those companies self-disclose. Resolves legal-entity party names to issuer FIGIs via a two-stage protocol. Triages → scores → converges → promotes candidates scoring 28+ to full deep-dive briefs. Runs autonomously under four scheduled tasks.

## What this tool is NOT

- Not a replacement for Tools 1 or 2 — structurally independent (D-001).
- Not a source of investment advice — candidate briefs are research artifacts.
- Not a PACER-billing agent — v1 never spends PACER credits autonomously (D-008).
- Not a non-US tool — US-listed equities only in v1 (D-006).

## Phase status (as of 2026-04-14)

- Phase 0 (scaffolding) — **COMPLETE**.
- Phase 1 (endpoint validation) — **NEXT**.
- Phases 2–7 — see `INSTRUCTIONS.md` §15 Implementation Priority Queue.

## Folder map (one line)

`litigation_system/` = working project. `reporting_layer/` = deliverables (performance reports + litigation briefs). `archive/` inside `litigation_system/` = date-stamped snapshots, never delete.

## If something looks wrong

Do NOT edit `DECISIONS.md` retroactively. Append a new D-0XX that overrides the earlier one, citing evidence. History is load-bearing. See the adversarial note at the bottom of `DECISIONS.md` and the self-review discipline in `PROJECT_INSTRUCTIONS.md`.
