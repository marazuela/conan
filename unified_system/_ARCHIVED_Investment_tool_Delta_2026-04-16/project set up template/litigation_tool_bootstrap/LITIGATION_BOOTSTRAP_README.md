# LITIGATION TOOL — BOOTSTRAP FOLDER

This folder contains everything a fresh Cowork session needs to instantiate the **Litigation & Docket Signal** investment discovery tool (called "Tool 3" in the parent project family, alongside Tool 1 = US-centric catalyst discovery and Tool 2 = Non-US primary-source discovery).

This folder is **self-contained and portable**. Cut-paste the entire `litigation_tool_bootstrap/` folder into a fresh working directory, open a Cowork session pointed at it, and the session will have every reference it needs — including the parent `PROJECT_TEMPLATE.md` — to stand up the full tool.

---

## READ ORDER FOR THE NEW SESSION

Do not skim. Do not parallelize. These files are ordered so each one builds on the last:

1. **`PROJECT_TEMPLATE.md`** (duplicated into this folder from the parent project) — the governing philosophy for every multi-session autonomous tool in this family. Read it **in full** before anything else. Non-negotiables, the two founding principles, the ten relay files, the four-task topology, the ten-point self-review — all of it. Everything else in this folder adapts that template to the litigation domain; you cannot evaluate the adaptation without knowing what it adapted from.

2. **`LITIGATION_OBJECTIVES.md`** — Primary goal, mandate, universe, constraints, holding horizon, position sizing. Everything that should end up in the new project's `OBJECTIVES.md` file on day one.

3. **`LITIGATION_CONTEXT.md`** — Domain background, validated endpoint table, entity-resolution protocol (which is different from Tool 1/2 because courts don't use tickers), scoring quick reference.

4. **`LITIGATION_STRATEGIES.md`** — Per-source specification for each of the six litigation channels in v1 scope. Each channel spec includes: what to scan, why it's an asymmetry, endpoint/access method, entity-resolution notes, signal-type taxonomy, triage filters.

5. **`LITIGATION_SCORING.md`** — The 7-dimension scoring rubric, adapted for litigation signals. Two dimensions change meaning relative to Tool 1/2; one is new.

6. **`LITIGATION_DECISIONS_SEED.md`** — Pre-written D-000 through D-00N decisions that the new session should copy verbatim into its `DECISIONS.md` on day one. These are the founding architectural choices that must NOT be re-litigated.

7. **`LITIGATION_PHASING.md`** — Build sequence, milestones, success criteria per phase. Translates the priority queue into a concrete week-by-week plan.

8. **`LITIGATION_FAILURE_MODES.md`** — Domain-specific failure catalog on top of PROJECT_TEMPLATE Part 17. Docket parsing, sealing orders, PACER billing, RECAP coverage gaps, and the other things that will break this tool specifically.

---

## WHAT THIS BOOTSTRAP FOLDER IS AND ISN'T

**It IS:**
- The complete adaptation conversation, pre-written. PROJECT_TEMPLATE demands an adaptation conversation before any work begins ("no files are created, no tasks are scheduled, and no code is written until that adaptation conversation has produced explicit answers"). This folder **is** that conversation, resolved into documents.
- Medium-thick. Sources, entity-ID system, scoring dimensions, founding decisions, and phasing are locked. Scanner implementation details, exact endpoint schemas, and Phase 2+ scope are left for the new session to work through.
- A forcing function. The new session cannot skip the adaptation step because the adaptation is already done — its job is to execute against it, not redesign it.

**It ISN'T:**
- The tool itself. No `<domain>_system/` folder, no `SESSION_STATE.md`, no `SESSION_LOCK.md`, no scanners, no scheduled tasks. Those get instantiated by the new session, following PROJECT_TEMPLATE Part 16.
- A license to skip PROJECT_TEMPLATE. The non-negotiables (overwrite-only locks, 4-hour stale window, write-scope isolation, Tool Validation Protocol, no chat questions in scheduled sessions, shut down before context exhausts, archive-don't-delete, verify-don't-remember) apply verbatim.
- Flexible on the founding decisions in `LITIGATION_DECISIONS_SEED.md`. Those are D-000 through D-00N. They get copied to `DECISIONS.md` and are treated as settled.

---

## DAY-ONE INSTANTIATION PROTOCOL FOR THE NEW SESSION

Once the new session has read files 1–8 above:

1. Create the working project folder (sibling to this bootstrap folder, or wherever the user directs). Use the name `litigation_system/` and the task-name prefix `litigation-` unless the user overrides.
2. Create the two-folder split per PROJECT_TEMPLATE Part 2: `litigation_system/` (writable) and `reporting_layer/` (read-isolated).
3. Populate the ten relay files in `litigation_system/` per PROJECT_TEMPLATE Part 3:
   - `PROJECT_INSTRUCTIONS.md` — copy the charter structure from PROJECT_TEMPLATE 3.1; adapt the self-review checklist domain items from `LITIGATION_FAILURE_MODES.md`.
   - `README.md` — cold-start read order per PROJECT_TEMPLATE 3.2.
   - `INSTRUCTIONS.md` — architecture + pipeline, with scanners and cadence from `LITIGATION_STRATEGIES.md` and `LITIGATION_PHASING.md`.
   - `OBJECTIVES.md` — copy wholesale from `LITIGATION_OBJECTIVES.md`.
   - `CONTEXT.md` — copy wholesale from `LITIGATION_CONTEXT.md`.
   - `SESSION_STATE.md` — initial state = `Build / Phase 0`, priority queue from `LITIGATION_PHASING.md`.
   - `SESSION_LOCK.md` — `UNLOCKED` with initial timestamp.
   - `PROGRESS_LOG.md` — empty header, ready for the first session block.
   - `INDEX.md` — one line per file that now exists.
   - `DECISIONS.md` — copy D-000 through D-00N verbatim from `LITIGATION_DECISIONS_SEED.md`.
   - `OPEN_QUESTIONS.md` — empty template, ready for first Q-001.
4. Create `framework/scoring_system.md` from `LITIGATION_SCORING.md`.
5. Create `strategies/` with one `.md` per channel, using `LITIGATION_STRATEGIES.md` as the source.
6. Create empty `tools/`, `signals/`, `candidates/`, `reports/`, `working/`, `archive/` folders.
7. In `reporting_layer/`: `performance_reports/`, `litigation_briefs/docx/`, `litigation_briefs/pdf/`, `litigation_briefs/index.json`, `working/`, `archive/`.
8. Register the four scheduled tasks per PROJECT_TEMPLATE Part 4, with cadence specified in `LITIGATION_PHASING.md` (note: litigation cadence deviates from the default 3-hourly — see D-005 in the decisions seed).
9. Run the first operational session **manually** to confirm lock acquisition, deps install, one scanner runs end-to-end, SESSION_STATE gets rewritten, lock releases. Do NOT let the scheduled task fire until the manual run succeeds.

---

## NON-NEGOTIABLE ENTRY POINTS FROM PROJECT_TEMPLATE

Re-stated here so they cannot be overlooked:

1. SESSION_LOCK is overwrite-only. Never `rm`. Sandbox cannot reliably delete.
2. 4-hour stale-lock window.
3. Write-scope isolation: `litigation_system/` writers; `reporting_layer/` readers.
4. Tool Validation Protocol every session.
5. No chat questions in scheduled sessions — append to `OPEN_QUESTIONS.md`.
6. Shut down before context exhausts. Handoff quality > output quality > output volume.
7. Never delete — always archive.
8. Verify, don't remember. Training memory is not a source.

---

## RELATIONSHIP TO TOOLS 1 AND 2

Tool 3 (Litigation) is structurally **complementary** to Tools 1 and 2, not duplicative. Tool 1 reads financial-disclosure-native sources (EDGAR, ESMA, USAspending). Tool 2 reads non-US-exchange-native sources (LSE RNS, TDnet, HKEx, etc.). Tool 3 reads **legal-epistemology-native sources** — court dockets, administrative-law filings, patent-trial records. Every candidate Tool 3 produces is one Tools 1 and 2 categorically cannot find, because their sources don't see it until the defendant/plaintiff self-discloses, at which point the edge is gone.

Cross-tool signal merging happens only via a separate analyzer project, never through direct file coupling. This mirrors the Tool 1 / Tool 2 separation discipline (D-004 in the parent projects).

---

## FINAL WORD TO THE NEW SESSION

Do not jump to instantiation. Read all eight files first. If after reading them anything is unclear or contradictory, that is a founding-level issue — append to `OPEN_QUESTIONS.md` in the new project as Q-001 and continue with the parts that are clear. Do not ask Pedro in chat unless the session is interactive and he has indicated he is present. The bootstrap folder is the adaptation conversation; treat it as if Pedro and a prior session already had that conversation and handed you the output.
