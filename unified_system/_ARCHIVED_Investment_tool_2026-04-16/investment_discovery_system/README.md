# Investment Discovery System

Autonomous system that discovers non-traditional investment opportunities in publicly listed equities by scanning obscure public data sources that traditional research misses.

**Owner**: Pedro | **Started**: April 2026 | **Execution**: Claude via hourly Cowork scheduled sessions (concurrency-locked)

## Status

**Phase**: Pre-build — framework complete, all 5 strategies validated end-to-end, awaiting approval to begin tool development.

## How to Start (Cold-Start Read Order)

1. Read `SESSION_STATE.md` — the relay baton: current phase, what's done, what's in progress, what's next, active warnings
2. Read `INSTRUCTIONS.md` — execution rules, architecture, pipeline, session rules, priority queue
3. Read `OPEN_QUESTIONS.md` — if SESSION_STATE flags blockers
4. Read only the specific file needed for the current task (strategy spec, scoring rubric, API reference in `CONTEXT.md`)

Do **not** read all files. SESSION_STATE + INSTRUCTIONS gives full working context. PROGRESS_LOG is history — read only when you need to trace a past decision.

## Quick Reference

| What | Where |
|------|-------|
| Full objectives & success criteria | `OBJECTIVES.md` |
| Execution rules & architecture | `INSTRUCTIONS.md` |
| API endpoints & validation status | `CONTEXT.md` |
| All decisions with rationale | `DECISIONS.md` |
| Open blockers & questions | `OPEN_QUESTIONS.md` |
| File inventory | `INDEX.md` |
| Session history | `PROGRESS_LOG.md` |
| Strategy specs | `strategies/` |
| Scoring rubric | `framework/scoring_system.md` |
| Candidate template | `framework/candidate_template.md` |
