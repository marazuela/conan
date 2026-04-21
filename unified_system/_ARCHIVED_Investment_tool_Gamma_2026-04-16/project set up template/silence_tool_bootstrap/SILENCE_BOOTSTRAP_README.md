# SILENCE SCANNER — Bootstrap Folder (Tool 4 / "Option 1")

This folder is the full instantiation package for **Tool 4 — the Silence Scanner**. It is self-contained and portable. Cut-paste this folder into a target location, open a fresh Cowork session pointed at it, and the session will have everything it needs to stand up the project without re-doing the adaptation conversation.

This folder **is** the adaptation conversation made durable. Do not re-litigate the decisions inside it; append later-numbered decisions when new evidence arrives.

---

## What Tool 4 is (one paragraph)

Tools 1, 2, and 3 scan firehoses for *events* — filings, dockets, enforcement releases. Their epistemology is "something happened; was it material?" Tool 4 inverts the question: *nothing happened, and something should have.* It maintains a behavioral baseline for every in-universe issuer across multiple observable dimensions (filings cadence, press-release cadence, social/news mention volume, conference presence, insider-transaction cadence, analyst-note cadence). When an issuer's observed activity drops meaningfully below its own historical baseline, the silence itself is emitted as a signal. Silence precedes disclosure far more often than chance; quiet periods cluster around pre-announcement windows, pre-restatement reviews, legal investigation, board consolidation, M&A dark periods. No other tool in the family captures this class of signal.

---

## Read order for a fresh session (day one)

1. `SILENCE_BOOTSTRAP_README.md` — this file
2. `PROJECT_TEMPLATE.md` — the founding non-negotiable template (duplicated from parent folder for portability)
3. `SILENCE_OBJECTIVES.md` — primary goal, mandate, universe scope, sub-goals, done-definition
4. `SILENCE_CONTEXT.md` — why silence has asymmetry, endpoints, baseline data model, schema extensions
5. `SILENCE_DIMENSIONS.md` — the six observable-activity dimensions with operational definitions
6. `SILENCE_SCORING.md` — 7-dimension scoring rubric adapted for probabilistic anomaly signals
7. `SILENCE_DECISIONS_SEED.md` — D-000 through D-0XX to copy verbatim into `DECISIONS.md` on day one
8. `SILENCE_PHASING.md` — Phase 0 through Phase 8+ with success criteria and gating milestones
9. `SILENCE_FAILURE_MODES.md` — domain-specific failure modes (silence-tool specific on top of template catalog)

After reading all nine, the session is ready to execute Phase 0 (project instantiation) per `SILENCE_PHASING.md`.

---

## Day-one instantiation protocol (9 steps)

1. **Create folders**: `silence_system/` and `reporting_layer/` per `PROJECT_TEMPLATE.md` Part 2.
2. **Populate the ten relay files** per PROJECT_TEMPLATE Part 3: README, PROJECT_INSTRUCTIONS, INSTRUCTIONS, OBJECTIVES (copy SILENCE_OBJECTIVES verbatim), CONTEXT (copy SILENCE_CONTEXT verbatim), SESSION_STATE, SESSION_LOCK (init UNLOCKED), PROGRESS_LOG, INDEX, DECISIONS (copy D-000 through D-0XX from SILENCE_DECISIONS_SEED verbatim), OPEN_QUESTIONS.
3. **Create `framework/scoring_system.md`** from SILENCE_SCORING.md verbatim.
4. **Create `framework/candidate_template.md`** — adapted from Tool 1/2/3 candidate template with silence-specific deep-dive sections (alternative hypotheses, seasonality check, baseline-validity check).
5. **Create `framework/dimensions_specification.md`** from SILENCE_DIMENSIONS.md verbatim.
6. **Create `baselines/`** directory for per-issuer baseline JSONs and the SQLite index (empty at instantiation; populated during Phase 1 warm-up).
7. **Create empty `tools/`, `signals/`, `candidates/`** (with `delivered/` and `archive/` subfolders), **`reports/`, `working/`, `research/`, `archive/`, `universe/`** (for universe-definition artifacts).
8. **Create `reporting_layer/`** substructure: `performance_reports/`, `silence_briefs/docx/`, `silence_briefs/pdf/`, `silence_briefs/index.json` (empty array), `working/`, `archive/`.
9. **Register scheduled tasks** per D-012 in SILENCE_DECISIONS_SEED. **Do not enable them yet** — Phase 0 is skeleton-only; scanners do not exist. Leave tasks scheduled but disabled.

Day-one exit criteria: INDEX.md enumerates every created file; SESSION_STATE.md correctly reports "Phase 0 complete; next block: Phase 1 universe definition and baseline warm-up"; SESSION_LOCK.md reads UNLOCKED; DECISIONS.md contains D-000 through D-0XX verbatim.

---

## Non-negotiable entry points from PROJECT_TEMPLATE

Every discipline from the parent template applies verbatim unless overridden by a numbered decision in `SILENCE_DECISIONS_SEED.md`. Specifically:

- **Overwrite-only lock semantics** (Part 3.6, 4-hour stale-lock window).
- **Write-scope isolation** between `silence_system/` and `reporting_layer/`.
- **Tool Validation Protocol** — every `tools/*.py` file must pass `py_compile` and a smoke-call test before being committed to operational use.
- **No chat questions in scheduled sessions** — scheduled tasks never block on user input; unresolved issues go to `OPEN_QUESTIONS.md`.
- **Shutdown protocol** — sessions must complete a checkpoint write to SESSION_STATE before any risk of context exhaustion.
- **Archive, never delete** — superseded files move to `archive/YYYY-MM-DD_<reason>/`.
- **Verify, don't remember** — scheduled sessions re-read SESSION_STATE at startup, never assume continuity from memory.
- **Cold-start protocol** — a fresh session reads SESSION_STATE + INSTRUCTIONS and proceeds without user re-priming.

---

## Founding deviations from PROJECT_TEMPLATE (summarized; detailed in D-000 through D-014)

Silence is a fundamentally different signal shape than the event-driven firehoses Tools 1/2/3 scan. Four deviations are load-bearing:

1. **Baseline persistence is a first-class dataset** (not an incidental cache). Every issuer has a per-issuer JSON in `baselines/issuer_<cik>.json` plus a SQLite index for cross-issuer queries. The baseline IS the tool's memory of normal.
2. **Mandatory warm-up phase** before any signals are emitted. Minimum 90 calendar days of observation per issuer before that issuer's baseline is eligible to produce signals. Shorter history is statistically indistinguishable from noise.
3. **Signals are probabilistic, not binary.** Every silence signal carries a z-score, a p-value (one-sided, observed ≤ expected), and an explicit alternative-hypothesis field. The scoring rubric treats low-confidence anomalies differently than Tools 1/2/3 treat low-strength events.
4. **Convergence window extended to 60 days** (vs. Tool 1/2's 14, Tool 3's 30). Silence signals precede disclosures by longer lead times; the window must accommodate that lead.

See `SILENCE_DECISIONS_SEED.md` for the full decisions with alternatives-considered and implications.

---

## Relationship to Tools 1, 2, and 3

Tool 4 is **structurally independent**. Separate working folder, separate SESSION_LOCK, separate candidate files, separate scheduled tasks. Cross-tool convergence (a Tool 1 event + a Tool 4 preceding silence signal on the same entity) happens only in a future cross-tool analyzer project that reads from all tools' candidate folders and never writes back.

This discipline exists to prevent cascade failure: a Tool 1 crash cannot block Tool 4, and vice versa. See D-001.

---

## What this folder does NOT contain

- Running code. All Python is written during Phase 1+ in the target working project, not here.
- Validated endpoints. Every endpoint in SILENCE_CONTEXT is marked `⚠️ UNVERIFIED` until Phase 1 live-probes it.
- Baseline data. Baselines are built during Phase 1 warm-up against live sources.
- Candidate outputs. Candidates are produced during Phase 2+ operational runs.

This folder is the *specification* and *founding decisions* only. The working project is what the instantiated session builds from these specs.

---

## Relationship to the bootstrap pattern established by the litigation tool

This folder deliberately mirrors the structure of `litigation_tool_bootstrap/` (9 files, same naming convention, same relay-file layout). The pattern is now established as the family's bootstrap idiom:

```
<tool>_bootstrap/
  PROJECT_TEMPLATE.md              (duplicated for portability)
  <TOOL>_BOOTSTRAP_README.md       (entry point + read order)
  <TOOL>_OBJECTIVES.md             (what and why)
  <TOOL>_CONTEXT.md                (data sources, resolution, schema)
  <TOOL>_<DOMAIN_SPEC>.md          (domain-specific core — strategies for T3, dimensions for T4)
  <TOOL>_SCORING.md                (7-dim rubric adapted)
  <TOOL>_DECISIONS_SEED.md         (D-000 through D-0XX verbatim)
  <TOOL>_PHASING.md                (Phase 0 through Phase 8+)
  <TOOL>_FAILURE_MODES.md          (domain-specific failure modes)
```

Any future tool (Tool 5, Tool 6, ...) should follow this pattern.

---

## Reporting (external) — added 2026-04-15

When this tool reaches operational status, it will be producer-only. Reporting is handled by the project-root `Reporting Hub/` (tasks `reporting-hub-performance`, `reporting-hub-deep-dives`). This tool does not need its own `reporting_layer/` folder. Register read paths in `Reporting Hub/SOURCES.md` under the `silence` section when Phase 1 begins.
