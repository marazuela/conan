# Conan Decision Register — Code vs Memory/Docs

**Date:** 2026-04-27
**Purpose:** every place where memory, docs, comments, or prior reports disagree with running code. Code wins by default; the disagreement itself is a finding.

---

## Disposition legend

- **code-wins** — memory/docs are stale, must be updated
- **memory-wins** — code is wrong, must be fixed (logged as a finding)
- **defer** — investigation incomplete, revisit
- **retired** — rule is no longer current; both should be cleaned up

---

## Register

### D-001: 60-day catalyst rule for watch→active promotion
- **Memory says:** "watch→active: catalyst within 60d + challenger approval (implicit); 2026-04-27: + extensions.routine_declined IS DISTINCT FROM 'true'. Demote threshold 60d to prevent oscillation." (`candidate_watch_active_promotion.md`)
- **Code says:** Rule lives in [.claude/skills/candidate_aging.md:56-99](.claude/skills/candidate_aging.md), executed by Claude in a Cowork scheduled task. Not in Python, TypeScript, SQL trigger, or RPC.
- **Disposition:** **memory-correct, code-incomplete.** Memory accurately describes intended behavior. Code does not enforce it; the skill is the enforcer. Memory should be amended to add: "Enforced exclusively by `candidate_aging` Claude skill; no DB-level guard." — see F-001.
- **Linked finding:** F-001

### D-002: Thesis_writer 15/day daily cap
- **Memory says:** "v2: Claude drafts all theses (Immediate band, via Claude app routines API, 15/day cap)" (`thesis_authoring_by_claude.md`)
- **Code says:** Cap enforced by an SQL count query inside [.claude/skills/thesis_writer.md:60-70](.claude/skills/thesis_writer.md), executed by Claude before drafting. No CHECK constraint, no trigger, no DB-level guard.
- **Disposition:** **memory-correct, code-incomplete.** Same pattern as D-001. Memory should add the enforcement-mechanism note. — see F-002.
- **Linked finding:** F-002

### D-003: Convergence windows (14d standard / 30d litigation)
- **Memory says:** *no entry — convergence window values not in MEMORY.md*
- **Code says:** Hardcoded in three places: rubric_engine.py:494-500, pre_edge_monitor.py:28-36, convergence.ts:107-110. No config source.
- **Disposition:** **add memory entry + spawn task to consolidate.** — see F-003.
- **Linked finding:** F-003

<!-- additional rules to add as discovered -->
