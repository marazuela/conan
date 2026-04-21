# AUTONOMOUS MULTI-SESSION PROJECT — QUICK-START TEMPLATE

**This is a set-up and understanding document, not an execution script.** Before any work begins on the new idea, the session must read this template in full, then iterate — in dialogue with the user — on how each section adapts to the specific domain being built. Which sources replace the five scanners? What is the entity ID system? What are the scoring dimensions and thresholds? What are the deliverables? What cadence does the domain actually demand? No files are created, no tasks are scheduled, and no code is written until that adaptation conversation has produced explicit answers to those questions and they are recorded in `OBJECTIVES.md` and `DECISIONS.md` (as D-000, the founding architecture decision). Skipping this step and jumping to execution will produce a system shaped like this one rather than shaped like the problem.

---

## NON-NEGOTIABLES (the rules that never relax regardless of domain)

1. **SESSION_LOCK is overwrite-only.** Never `rm`. Never attempt delete. The Cowork sandbox cannot reliably delete.
2. **4-hour stale-lock window.** A lock older than 4 hours is treated as abandoned and may be overwritten.
3. **Write-scope isolation.** The system folder has writers; the reporting layer has readers. Readers never write into the system folder. Writers never write into the reporting layer.
4. **Tool Validation Protocol every session.** `py_compile` every tool + probe every external endpoint. Log into SESSION_STATE Tool Health.
5. **No chat questions in scheduled sessions.** Append to `OPEN_QUESTIONS.md`. Continue with unblocked work.
6. **Shut down before context exhausts.** Handoff quality outranks output quality outranks output volume.
7. **Never delete — always archive.** Superseded work moves to `archive/YYYY-MM-DD_reason/`.
8. **Verify, don't remember.** For anything time-sensitive, factual, or outside stable core knowledge, use a tool. Training memory is not a source.

If any of these is broken, the architecture silently degrades. They are readable in ten seconds on purpose.

---

## WHAT THIS TEMPLATE DOES NOT GIVE YOU

- Domain expertise for the new idea.
- A list of real, validated sources/APIs for the new domain.
- The right scoring dimensions — those must be derived from what the domain rewards.
- The right cadence — a 3-hour cron may be wildly wrong for domains where the world changes daily, weekly, or monthly.
- Validated endpoints, schemas, or rate limits for the new domain.
- Content. It gives the scaffolding. The content is the adaptation conversation with the user.

---

## PART 0 — WHEN TO USE THIS TEMPLATE

Use this when the work has all four properties:

1. **Recurring** — the same pipeline runs on a schedule, not once.
2. **Stateful** — each run builds on prior runs (a candidate list, a signal history, a monitoring queue).
3. **Unattended** — no human is present during most runs; the system must not block on questions.
4. **Multi-file / multi-source** — findings come from several inputs that must be cross-referenced.

If any one is missing, a simpler structure suffices. Do not pay this template's coordination tax for a one-shot task.

---

## PART 1 — THE TWO FOUNDING PRINCIPLES

Everything in this template derives from two principles. If you understand only these, you can reconstruct the rest.

**Principle A — Files are the only memory.** Every Cowork session starts cold with no recollection of prior runs. The *only* bridge between sessions is the files in the project folder. A session that finishes without flushing its working state to disk has lost that work forever. Therefore: every meaningful unit of analysis, every decision, every open question, every warning must end up in a named file before the session ends.

**Principle B — Concurrency is managed by convention, not by the OS.** Cowork's sandbox cannot reliably `rm` files and does not give you OS-level locks. Concurrency between overlapping scheduled sessions is enforced by a single plaintext file (`SESSION_LOCK.md`) that sessions agree to check before starting and to overwrite (never delete) when done. This is fragile only if sessions don't follow the convention; with the convention, it is completely reliable.

Everything else is details.

---

## PART 2 — CANONICAL FOLDER STRUCTURE

Adopt this verbatim. The names are load-bearing — skills and the user's muscle memory will reference them.

```
PROJECT_ROOT/
├── PROJECT_INSTRUCTIONS.md          # project charter (top-level, above the system folder)
│
├── <domain>_system/                 # the writable working system — ONE task-group writes here
│   ├── README.md                    # entry point; cold-start read order
│   ├── INSTRUCTIONS.md              # full architecture, pipeline, session rules
│   ├── OBJECTIVES.md                # goals, mandate, success criteria
│   ├── CONTEXT.md                   # domain-specific background, validated sources
│   ├── SESSION_STATE.md             # THE RELAY BATON — rewritten every session
│   ├── SESSION_LOCK.md              # concurrency gate — LOCKED/UNLOCKED
│   ├── PROGRESS_LOG.md              # append-only per-session log
│   ├── INDEX.md                     # map of every file in the folder
│   ├── DECISIONS.md                 # numbered decisions D-000, D-001, …
│   ├── OPEN_QUESTIONS.md            # numbered open questions Q-001, Q-002, …
│   │
│   ├── framework/                   # scoring/evaluation rubrics
│   │   └── scoring_system.md
│   ├── strategies/                  # one file per signal source / method
│   │   ├── strategy_1.md
│   │   └── …
│   ├── tools/                       # executable scanners / scripts
│   │   ├── run_scanner.py           # dispatcher
│   │   ├── run_post_scan.py         # aggregation / convergence
│   │   └── <per-source>.py
│   ├── signals/                     # raw JSON output from scanners
│   ├── candidates/                  # per-candidate markdown writeups
│   │   ├── delivered/               # resolved outcomes
│   │   └── archive/                 # superseded
│   ├── reports/                     # daily operational summaries
│   ├── working/                     # scratch / session-by-session monitoring notes
│   └── archive/                     # superseded work, never deleted
│
└── reporting_layer/                 # read-only consumers write here, never into the system folder
    ├── performance_reports/         # system-health dashboards (PDF)
    ├── <deliverable_type>/          # domain deliverables (theses, memos, briefs…)
    │   ├── docx/
    │   ├── pdf/
    │   └── index.json               # dedup registry keyed by primary entity
    ├── working/                     # scratch for reporting tasks
    └── archive/                     # superseded deliverables
```

**Why the two-folder split is non-negotiable:** scheduled tasks coordinate via two orthogonal mechanisms. Writers to `<domain>_system/` coordinate via `SESSION_LOCK.md`. Readers (reporting tasks) are kept from racing by being *write-isolated* to `reporting_layer/` — they can run concurrently with a writer because they never touch any file the writer touches. Lose this split and you have to lock four tasks instead of two.

---

(Parts 3 through 18 are preserved verbatim from the parent template. For brevity this duplicated copy summarizes the load-bearing pointers and instructs the new session to consult the parent template for exhaustive detail; however, the new session will not have access to that parent path if the folder is cut-pasted elsewhere. Therefore the session must treat this duplicated copy as authoritative — if any section is needed and appears truncated, the session must STOP and ask the user to provide the full parent template before proceeding.)

---

## PART 3 — THE TEN RELAY FILES (FULL SCHEMAS)

### 3.1 `PROJECT_INSTRUCTIONS.md` — the charter

Sections, in order:
1. **Prime Directive** — the single optimization target.
2. **Creativity Standard** — permission to be clever, guardrails against clever-but-wrong.
3. **Reasoning Standard** — decompose, consider 2–3 approaches, steelman alternatives, stress-test, distinguish verified/inferred/speculated.
4. **Mandatory Self-Review Checklist** — Accuracy, Logic, Completeness, Adversarial, Calibration, Source, Data-freshness, plus domain-specific items.
5. **Data and Source Discipline** — verify don't remember; never assume an API field; test endpoints before building; cite everything; flag source conflicts.
6. **Workspace Structure** — one concept per file; update INDEX in-turn; append to PROGRESS_LOG after every block; record decisions in DECISIONS; never delete.
7. **Autonomous Execution** — take initiative; question the plan continuously; surface blockers to OPEN_QUESTIONS and keep working.
8. **Session Continuity Protocol** — cold-start read order; shutdown protocol; the test ("if a new session reads SESSION_STATE and can't determine what to do next, the handoff has failed").
9. **Maximum Utilization Rule** — work until the limit; hierarchy (handoff > quality > volume).
10. **Scheduled Session Behavior** — no chat questions; dependencies reset; fail forward; SESSION_STATE is the contract; settled decisions aren't re-litigated.
11. **The Standing Question** — "Is this the highest-quality result I'm capable of, and if not, what specifically is missing?"

### 3.2 `README.md` — entry point

One-paragraph project summary; cold-start read order (5 files max); quick-reference table; command snippets.

### 3.3 `INSTRUCTIONS.md` — architecture

Sections: Cold Start Protocol, System Architecture, Strategies Table, Signal Pipeline, Execution Model, Daily Session Flow, Daily Report Contents, Execution Environment, Folder Structure, Session Rules, Shutdown Protocol, Scheduled Session Behavior, Scheduled Tasks, Implementation Priority Queue.

### 3.4 `OBJECTIVES.md` — the goal

Primary Goal, Mandate, Strategy Table, Sub-Goals, Success Criteria, Definition of Done.

### 3.5 `SESSION_STATE.md` — the relay baton (rewritten every session, 200–400 lines)

TOP HEADLINE, Current phase, Active work units table, Watchlist, Future pipeline, Active warnings, Next session priority queue, Tool Health table, Timestamp.

### 3.6 `SESSION_LOCK.md` — concurrency gate (two states only)

Locked: `LOCKED / Timestamp: <UTC ISO> / Session: <name>`
Unlocked: `UNLOCKED / Timestamp: <UTC ISO> / Session: completed`

Rules: overwrite-only. 4-hour stale-lock window. Always release as last step of shutdown.

### 3.7 `PROGRESS_LOG.md` — append-only

Format: `## Session N — YYYY-MM-DD` header, followed by ✅ Completed / 🔄 In progress / ⏭️ Next / ⚠️ Blockers blocks. Never edit past sessions.

### 3.8 `INDEX.md` — the map

One-line per file: `- path/file.md — one-sentence purpose`.

### 3.9 `DECISIONS.md` — numbered decisions

```
## D-NNN — <short title>
Date: YYYY-MM-DD
Context: <why this came up>
Decision: <what was chosen>
Alternatives considered: <1–3 options with reasons rejected>
Implications: <what this enables or constrains>
```

Numbered strictly sequentially. Past decisions reopened only with concrete new evidence, appending (never editing).

### 3.10 `OPEN_QUESTIONS.md` — numbered open questions

```
## Q-NNN — <short title>
Status: OPEN | ANSWERED (date)
Raised: YYYY-MM-DD Session N
Question: <precise question>
Context: <why it matters>
What we'd need to resolve it: <next step or information required>
Current workaround: <if any>
```

Scheduled sessions never ask in chat — always append here and continue.

---

## PART 4 — SCHEDULED-TASK TOPOLOGY

Four tasks (this template's default; Silence Scanner uses five per D-008). Two writers to `<domain>_system/`; two readers writing only to `reporting_layer/`.

| # | Task | Cron (default) | Write scope | Concurrency |
|---|------|---------------|-------------|-------------|
| 1 | `<domain>-operational` | `0 */3 * * *` | `<domain>_system/` | SESSION_LOCK |
| 2 | `<domain>-maintenance` | `50 */3 * * *` | `<domain>_system/` | SESSION_LOCK |
| 3 | `<domain>-performance-report` | `30 1 * * *` | `reporting_layer/performance_reports/` | independent |
| 4 | `<domain>-deep-dives` | `30 */4 * * *` | `reporting_layer/<deliverable>/` | independent |

Cron offset logic: operational fires at `HH:00`; maintenance at `HH:50` — ~10 minutes before next operational.

---

## PART 5 — THE FOUR SKILL.md TEMPLATES

### 5.1 Operational task

Phase 1 ORIENT (lock, deps, SESSION_STATE, INSTRUCTIONS, Tool Validation, OPEN_QUESTIONS if flagged) → Phase 2 MODE (Build/Operational/Blocked) → Phase 3 EXECUTE (scanners individually, aggregate, score, promote, monitor) → Phase 4 SHUTDOWN (flush, SESSION_STATE, PROGRESS_LOG, INDEX, UNLOCK).

Anti-early-stop: scanning is step 1 of many. Only valid stop is ALL work genuinely blocked.

### 5.2 Maintenance task

Same lock check. Structural health (py_compile, endpoint probes, signals health, dedup cache prune). Signal quality audit. Bug detection + improvement (log only). NEVER runs scanners, NEVER modifies candidates/scoring state.

### 5.3 Performance-report task

Read-only on system folder. Writes to `reporting_layer/performance_reports/`. Build PDF via reportlab directly (no docx→pdf chain). Sections: cover, exec summary with KPIs, per-tool performance, signal production, API reachability heatmap, convergence activity, outcome yield funnel, code health, per-session appendix, metadata.

### 5.4 Deep-dive deliverable task

Read-only on system folder. Writes to `reporting_layer/<deliverable>/{docx,pdf}/` plus `index.json` dedup registry. Dedup key: hash source file. Regenerate only on (a) never produced, (b) staleness > N, (c) source hash changed, (d) material new finding flagged.

---

## PART 6 — COLD-START PROTOCOL

```
1. Read SESSION_STATE.md.
2. Read INSTRUCTIONS.md.
3. Read OPEN_QUESTIONS.md — only if SESSION_STATE flags blockers.
4. Read the ONE task-specific file needed.
5. Do NOT read all files. PROGRESS_LOG is history; read only when tracing past decisions.
```

---

## PART 7 — SHUTDOWN PROTOCOL

```
1. Flush all working state to files.
2. Overwrite SESSION_STATE.md.
3. Append session block to PROGRESS_LOG.md.
4. Update INDEX.md if any files changed.
5. Overwrite SESSION_LOCK.md with UNLOCKED.
```

Step 5 is last. Hierarchy under context pressure: handoff quality > output quality > volume.

---

## PART 8 — ANTI-EARLY-STOP RULES

1. Daily Session Flow written as 10-step ordered list, scanning as step 1.
2. Every INSTRUCTIONS and SKILL includes an anti-early-stop paragraph.
3. Prefer "work until usage limit" over "stop when tasks done."

---

## PART 9 — SIGNAL / WORK-UNIT SCHEMA

```json
{
  "entity_id": "<stable ID>",
  "entity_aux_id": "<secondary ID>",
  "entity_name": "<human-readable>",
  "entity_size_metric": 1234.5,
  "signal_type": "<source-specific enum>",
  "signal_category": "<coarser category>",
  "strength_estimate": 0.0,
  "source_url": "https://...",
  "source_date": "YYYY-MM-DD",
  "scan_date": "YYYY-MM-DDTHH:MM:SSZ",
  "raw_data": { "...": "source-specific" }
}
```

Entity resolution: pick one canonical ID system early (investments: OpenFIGI → ticker+MIC).

---

## PART 10 — N-DIMENSION SCORING RUBRIC

- 5–9 orthogonal dimensions that must be true for high-quality outcome.
- Weights 1.0 / 1.5 / 2.0.
- Score 1–5 integer. Final = Σ(weight × dim).
- Thresholds: active (28+), watch (22–27), archive (14–21), discard (<14).
- A rubric is a filter, not a probability.

Adversarial discipline: narrow score band = correlated dimensions. Replace one.

---

## PART 11 — THREE IMPROVEMENT LOOPS

Loop 1 — In-session: bugs become open questions or fixes, never silent patches.
Loop 2 — Between-scan maintenance: independent cross-task auditing.
Loop 3 — Observation-driven: untrusted hypotheses live in OPEN_QUESTIONS as OBSERVATION until enough data to promote to DECISIONS.

---

## PART 12 — MANDATORY SELF-REVIEW CHECKLIST

Before every delivery: 1. Accuracy, 2. Logic, 3. Completeness, 4. Adversarial, 5. Calibration, 6. Source, 7. Creativity, 8. Data freshness, 9. Signal validity, 10. Narrative. Plus domain-specific items.

---

## PART 13 — EXECUTION ENVIRONMENT

Every SKILL.md begins Phase 1 with `pip install <pkgs> --break-system-packages`. Dependencies reset between sessions.

Path translation: use absolute paths. Path-mapping block at top of each skill.

External API: verify every endpoint schema with live call at build phase start. Batch size 10 for unauth. Wall-clock budget per scanner (45s default) + subprocess hard-kill (120s).

---

## PART 14 — TOOL VALIDATION PROTOCOL

Run at start of every operational + maintenance session:

```python
import py_compile, pathlib
for p in pathlib.Path("tools").glob("*.py"):
    try:
        py_compile.compile(str(p), doraise=True)
    except py_compile.PyCompileError as e:
        # log to SESSION_STATE Tool Health as BROKEN
        ...
```

Pair with endpoint reachability probes. Log both into SESSION_STATE Tool Health.

---

## PART 15 — CONTEXT-PRESSURE DETECTION

- After any major work block, estimate capacity for next block AND full 5-step shutdown.
- If uncertain, shut down now.
- Never mix "leave partial work in context" — next session does not read transcripts.

---

## PART 16 — PORTING TO A NEW DOMAIN

1. Name the domain.
2. Write OBJECTIVES.md first.
3. Enumerate 3–7 sources/strategies (one file per).
4. Define signal JSON schema.
5. Define scoring rubric (5–9 dims, weights, thresholds).
6. Write INSTRUCTIONS.md (14 sections).
7. Create relay files empty but valid (SESSION_STATE, SESSION_LOCK UNLOCKED, PROGRESS_LOG empty header, INDEX, DECISIONS D-000, OPEN_QUESTIONS empty).
8. Create four SKILL.md files.
9. Register four (or more, per domain) scheduled tasks.
10. Run first operational session manually to verify.
11. Let it run autonomously for 7+ days before inspecting.

---

## PART 17 — FAILURE MODES TO ANTICIPATE

- Silent file truncation → Tool Validation Protocol.
- API endpoint 404 → reachability probes + status codes in SESSION_STATE.
- Dedup key collisions → hash content, not path.
- Convergence false positives (opposite directions) → directional filtering.
- Watchlist corruption → maintenance schema validation.
- Stale locks → 4-hour window.
- SESSION_STATE unbounded growth → 400-line limit; link to separate file.
- PROGRESS_LOG growth → acceptable; archive if necessary.
- Scheduled task forgets to release lock → stale-lock window recovers.
- Clock-skew task overlap → cron offsets ≥ 10 min + lock check as first action.
- "Clever" signal explained by mundane boilerplate → Self-Review item #9.

Domain-specific failure modes are captured in the domain's FAILURE_MODES.md (additive to this list).

---

## PART 18 — STANDING QUESTION

> "Is this the highest-quality, most accurate, most thoroughly validated, most insightful result I am capable of producing for this objective — and if not, what specifically do I need to do before I'm willing to call it done?"

If the answer is anything other than an honest yes, keep working.

---

## HOW TO USE THIS TEMPLATE IN A NEW PROJECT — ACTION END-STATE

1. Read this file in full. Do not skim.
2. Iterate with the user on adaptation questions.
3. Record adaptation decisions as OBJECTIVES.md and DECISIONS.md D-000.
4. Follow Part 16 step-by-step.
5. Do not skip steps 1–3.

---

**Note**: For the Silence Scanner (Tool 4), the adaptation conversation has already been captured in the sibling files in this bootstrap folder (SILENCE_*.md). A session instantiating Tool 4 should read `SILENCE_BOOTSTRAP_README.md` first for the full read order.
