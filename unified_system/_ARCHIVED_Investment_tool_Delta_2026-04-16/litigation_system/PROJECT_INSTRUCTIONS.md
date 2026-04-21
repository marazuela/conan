# PROJECT INSTRUCTIONS — Litigation & Docket Signal System (Tool 3)

The charter. This file is the top-level governing document for every session — interactive or scheduled — that touches this project. Read it once per project-lifetime; reread on any phase transition.

---

## 1. Prime Directive

**Maximum quality and accuracy in identifying publicly-traded equity investment opportunities from legal-epistemology-native sources, before the affected company's own disclosure of them.**

Volume is not a goal. Speed is a derived goal (edge decays). The single optimization target is: every surviving-triage signal represents a real, material, correctly-attributed litigation event that has not yet been priced in.

---

## 2. Creativity Standard

Be clever where creativity helps the thesis. Be boring where correctness demands it.

- Creative in synthesis: connecting a PTAB FWD to a 10-K patent-material-to-revenue disclosure is welcome.
- Creative in sourcing: discovering a new docket-entry-type that signals early is welcome.
- Never creative in party resolution: `resolution_confidence` is a stopwatch, not a judgment call.
- Never creative in scoring: weights are fixed per D-0XX decisions, not per-session intuition.

Clever-but-wrong fails worse than boring-and-right. A mis-attributed candidate poisons the pipeline for every downstream consumer.

---

## 3. Reasoning Standard

For any non-trivial question:

1. Decompose into sub-questions with distinct failure modes.
2. Consider 2–3 approaches before committing.
3. Steelman the opposing thesis — if the market is pricing this correctly, why?
4. Stress-test with explicit counter-examples.
5. Label every claim as **VERIFIED** (primary-source checked this session), **INFERRED** (derived from verified facts via documented logic), or **SPECULATED** (plausible but unverified).

Training memory is never a source. Tool 1 and Tool 2 are not sources either — every source re-verified in this tool's session.

---

## 4. Mandatory Self-Review Checklist

Every deliverable passes all 12 items before release. The first 10 are from PROJECT_TEMPLATE Part 12; the last 2 are litigation-specific, derived from `LITIGATION_FAILURE_MODES.md`.

1. **Accuracy** — every factual claim verifiable in-session?
2. **Logic** — each conclusion follows from premises?
3. **Completeness** — full scope addressed, not just the easy part?
4. **Adversarial** — reread as hostile reviewer; what breaks?
5. **Calibration** — verified / inferred / speculated labeled distinctly?
6. **Source** — authoritative, current, correctly interpreted?
7. **Creativity** — most interesting correct answer, or first correct one?
8. **Data freshness** — within expected window, not stale cache or memory?
9. **Signal validity** — could this be mundane docket housekeeping rather than the thesis?
10. **Narrative** — does the market narrative match, conflict with, or ignore the thesis?
11. **Party-resolution integrity** (litigation-specific, F-01, F-02, F-14) — did we verify `resolution_confidence ≥ 0.85`? Did we check the Exhibit 21 lookup is still valid (cache `last_verified` < 180 days)? Does materiality to the parent hold, or is this a peripheral-subsidiary signal?
12. **Docket-parse resilience** (litigation-specific, F-03 through F-11) — are we reading a substantive docket event, not a consolidation/sealing/remand/amended-caption artifact? Is the dedup key `(court + case_number + docket_entry_id)` and not caption-based?

If any item is not a clear YES, the deliverable is not released. For scheduled sessions, the failing deliverable is parked in `working/` and documented in `OPEN_QUESTIONS.md`.

---

## 5. Data and Source Discipline

- **Verify, don't remember.** Every endpoint schema live-probed at build time and on every maintenance cycle (Tool Validation Protocol).
- **Never assume an API field.** `raw_data` is the escape hatch; if a field's name is uncertain, record the raw response and parse defensively.
- **Cite everything.** Every candidate claim includes a source URL (docket, press release, filing). No citation → no claim.
- **Flag source conflicts.** If CourtListener says one thing and PACER-index says another, record both; do not silently choose.
- **PACER cost discipline (D-008).** No autonomous PACER spending. Ever. Missing document bodies are flagged for user-directed manual pull via `working/pacer_pulls_requested.md`.

---

## 6. Workspace Structure

- One concept per file. No catch-alls.
- Every new or substantially-changed file is reflected in `INDEX.md` in the same turn.
- `PROGRESS_LOG.md` is appended after every major work block, not at session end only.
- Decisions are recorded in `DECISIONS.md` numbered sequentially. A settled decision is not re-litigated; it is overridden by a later-numbered decision if new evidence invalidates it.
- Never delete. Always archive to `archive/YYYY-MM-DD_<reason>/`.

---

## 7. Autonomous Execution

- Take initiative. The schedule fires cold sessions that cannot wait for guidance.
- Question the plan continuously. If the priority queue in `SESSION_STATE.md` is stale or wrong, propose an override, record it in `DECISIONS.md`, and proceed. Do not silently deviate.
- Surface blockers to `OPEN_QUESTIONS.md` and keep working on unblocked items.
- The only invalid stop is "I finished the obvious first step" (see anti-early-stop, PROJECT_TEMPLATE Part 8).

---

## 8. Session Continuity Protocol

**Cold-start read order** (4 files max; never all files):

1. `SESSION_STATE.md` — current phase, actives, warnings, next queue.
2. `INSTRUCTIONS.md` — architecture, pipeline, session rules.
3. `OPEN_QUESTIONS.md` — only if `SESSION_STATE.md` flags blockers.
4. The ONE task-specific file needed (strategy spec, scoring rubric, failure-mode note).

**Shutdown protocol** (executed in this exact order; step 5 is last):

1. Flush all working state to files. Incomplete work goes to `working/`.
2. Overwrite `SESSION_STATE.md`. This is the relay baton.
3. Append a session block to `PROGRESS_LOG.md` (✅ done / 🔄 in progress / ⏭️ next / ⚠️ blockers).
4. Update `INDEX.md` if any files changed.
5. Overwrite `SESSION_LOCK.md` with `UNLOCKED / Timestamp: <UTC> / Session: completed`.

**The handoff test:** if a new session reads `SESSION_STATE.md` and cannot, in one minute, state what to do next, the handoff failed. Fix the file, not the protocol.

---

## 9. Maximum Utilization Rule

Work until the context limit — but never sacrifice handoff quality for extra output.

**Priority under context pressure:**

1. Handoff quality (shutdown protocol completed cleanly).
2. Output quality (no half-baked candidates released).
3. Output volume (more candidates is nice; not at cost of 1 or 2).

If uncertain whether there is capacity for the next work block AND the full 5-step shutdown, shut down now.

---

## 10. Scheduled Session Behavior

- **No chat questions.** Append to `OPEN_QUESTIONS.md` and continue with unblocked work.
- **Dependencies reset every session.** Always run `pip install <packages> --break-system-packages` in Phase 1 of every session. Never assume prior installs.
- **Fail forward.** A broken endpoint is logged to Tool Health and skipped; it does not halt the whole pipeline.
- **`SESSION_STATE.md` is the contract.** Its priority queue is the next session's job unless a higher-priority blocker emerges.
- **Settled decisions are not re-litigated.** D-000 through D-013 are settled. Override only via a new-numbered decision with new evidence.

---

## 11. The Standing Question

Before every output:

> "Is this the highest-quality, most accurate, most thoroughly validated, most insightful result I am capable of producing for this objective — and if not, what specifically do I need to do before I'm willing to call it done?"

If the answer is anything other than an honest yes, keep working.

---

## 12. Litigation-Domain Additions

- **Never key convergence on a party-name string.** Always key on `issuer_figi` (per D-003). A single misapplied key here silently corrupts every downstream convergence.
- **Legal information only.** No PACER autonomous spending (D-008). No sealed-filing content speculation.
- **No lyrics, no regurgitation of copyrighted news coverage.** Summarize docket entries and public press releases in our own words; quote only short fragments with attribution.
- **PII not persisted.** Plaintiff individuals in Chancery cases are classified and, where they are not public-company executives, discarded after classification — never written to durable candidate files.

---

## Reporting (external) — added 2026-04-15

Producer-only. Performance reports and litigation briefs are generated by the project-root `Reporting Hub/` (tasks `reporting-hub-performance`, `reporting-hub-deep-dives`), which reads this system's state files (`SESSION_STATE.md`, `PROGRESS_LOG.md`, `candidates/`) and writes exclusively to `Reporting Hub/litigation_briefs/` and `Reporting Hub/performance_reports/litigation/`.

- Do NOT write to `Reporting Hub/` from this system.
- Do NOT recreate a `reporting_layer/` subfolder — it was removed on 2026-04-15.
- Hub read contract: `Reporting Hub/SOURCES.md`. Current availability flag for litigation: `pre-launch` — the hub skips this tool until operational files exist.
