# Investment Tool — Functional Architecture Diagram

```
═══════════════════════════════════════════════════════════════════════════════
                      INVESTMENT TOOL — SYSTEM ARCHITECTURE
═══════════════════════════════════════════════════════════════════════════════

┌─────────────────────────────────────────────────────────────────────────────┐
│                         LAYER 0 — GOVERNANCE / META                         │
│  (Rules every session must obey before any action is taken)                 │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   ┌────────────────┐   ┌────────────────┐   ┌─────────────────────────┐     │
│   │ Prime Directive│   │  Creativity    │   │ Reasoning Standard      │     │
│   │ Max quality &  │◄─►│ Clever but     │◄─►│ First principles,       │     │
│   │ accuracy       │   │ never wrong    │   │ steelman, stress-test   │     │
│   └────────┬───────┘   └────────┬───────┘   └────────────┬────────────┘     │
│            │                    │                        │                  │
│            └────────────────────┼────────────────────────┘                  │
│                                 ▼                                           │
│                  ┌────────────────────────────────┐                         │
│                  │   10-POINT SELF-REVIEW GATE    │                         │
│                  │ Accuracy · Logic · Completeness│                         │
│                  │ Adversarial · Calibration      │                         │
│                  │ Source · Creativity · Freshness│                         │
│                  │ Signal validity · Narrative    │                         │
│                  └───────────────┬────────────────┘                         │
│                                  ▼                                          │
│                          (no unreviewed output leaves)                      │
└─────────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                   LAYER 1 — SESSION CONTINUITY PROTOCOL                     │
│          (Bridges cold-start sessions; the only memory between runs)        │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   COLD-START READ ORDER:                                                    │
│   ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐        │
│   │ SESSION_STATE.md │──►│ INSTRUCTIONS.md  │──►│ OPEN_QUESTIONS.md│        │
│   │  (relay baton)   │   │ (architecture)   │   │ (only if flagged)│        │
│   └──────────────────┘   └──────────────────┘   └──────────────────┘        │
│                                                                             │
│   SHUTDOWN PROTOCOL:                                                        │
│   Flush state → Overwrite SESSION_STATE → Append PROGRESS_LOG → Update INDEX│
│                                                                             │
│   WORKSPACE FILES:                                                          │
│   SESSION_STATE · INSTRUCTIONS · INDEX · PROGRESS_LOG · DECISIONS           │
│   OPEN_QUESTIONS · research/ · working/ · archive/                          │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                  LAYER 2 — DATA & SOURCE DISCIPLINE                         │
│            (Every fact verifiable; every endpoint tested)                   │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   PRIMARY SOURCES           SECONDARY / NOVEL          VERIFICATION         │
│   ┌──────────────┐          ┌──────────────┐          ┌──────────────┐      │
│   │ Official     │          │ Niche DBs    │          │ Live API call│      │
│   │ filings      │          │ Court filings│          │ before build │      │
│   │ Regulators   │          │ Archives     │          │ Schema check │      │
│   │ Gov DBs      │          │ Alt data     │          │ Citation req.│      │
│   └──────┬───────┘          └──────┬───────┘          └──────┬───────┘      │
│          └─────────────────────────┼─────────────────────────┘              │
│                                    ▼                                        │
│            Label: VERIFIED  |  INFERRED  |  SPECULATED                      │
└─────────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    LAYER 3 — ANALYTICAL PIPELINE                            │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────┐   ┌─────────────┐   ┌─────────────┐   ┌─────────────┐      │
│  │ 1. SOURCING │──►│ 2. STRATEGY │──►│ 3. SCORING  │──►│ 4. NARRATIVE│      │
│  │ Structured  │   │ -specific   │   │ Rubric      │   │ web research│      │
│  │ signal feeds│   │ analysis    │   │ application │   │ (MANDATORY) │      │
│  └─────────────┘   └─────────────┘   └─────────────┘   └──────┬──────┘      │
│                                                                │             │
│                     ┌──────────────────────────────────────────┘            │
│                     ▼                                                       │
│  ┌─────────────┐   ┌─────────────┐   ┌─────────────┐   ┌─────────────┐      │
│  │ 5. THESIS   │──►│ 6. ADVERSAR.│──►│ 7. DEFENSE  │──►│ 8. DELIVERY │      │
│  │ synthesis   │   │ kill-test   │   │ doc + cites │   │ + logging   │      │
│  └─────────────┘   └─────────────┘   └─────────────┘   └─────────────┘      │
│                                                                             │
│  NARRATIVE CHECK (mandatory web layer):                                     │
│  News · Analyst activity · Litigation · Regulatory · Sentiment              │
└─────────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                 LAYER 4 — EXECUTION CAPABILITIES                            │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   FILE TOOLS           SHELL / COMPUTE        WEB / EXTERNAL                │
│   ┌────────────┐       ┌────────────┐         ┌────────────────┐            │
│   │ Read       │       │ Bash       │         │ WebSearch      │            │
│   │ Write      │       │ (Linux VM) │         │ WebFetch       │            │
│   │ Edit       │       │ Python/Node│         │ Chrome browser │            │
│   │ Glob/Grep  │       │ pip/npm    │         │ (interactive)  │            │
│   └────────────┘       └────────────┘         └────────────────┘            │
│                                                                             │
│   ORCHESTRATION        SCHEDULING             MEMORY                        │
│   ┌────────────┐       ┌────────────┐         ┌────────────────┐            │
│   │ TodoWrite  │       │ scheduled- │         │ Auto-memory    │            │
│   │ Agent/Task │       │ tasks MCP  │         │ (persistent    │            │
│   │ AskUserQ.  │       │ cron/fireAt│         │  across runs)  │            │
│   └────────────┘       └────────────┘         └────────────────┘            │
└─────────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                   LAYER 5 — SKILL LIBRARY (invocable)                       │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   DOCUMENT PRODUCTION              DATA / ANALYSIS                          │
│   ┌──────────────────┐              ┌──────────────────┐                    │
│   │ docx             │              │ data:analyze     │                    │
│   │ xlsx             │              │ data:explore-data│                    │
│   │ pptx             │              │ data:sql-queries │                    │
│   │ pdf              │              │ data:write-query │                    │
│   └──────────────────┘              │ data:create-viz  │                    │
│                                     │ data:visualization│                   │
│   META / PLATFORM                   │ data:statistical │                    │
│   ┌──────────────────┐              │ data:validate    │                    │
│   │ skill-creator    │              │ data:dashboard   │                    │
│   │ schedule         │              │ data:context-ext.│                    │
│   │ setup-cowork     │              └──────────────────┘                    │
│   │ plugin-creator   │                                                      │
│   │ plugin-customizer│                                                      │
│   └──────────────────┘                                                      │
└─────────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│               LAYER 6 — AUTONOMOUS OPERATING MODE                           │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   INTERACTIVE SESSIONS           SCHEDULED (UNATTENDED) SESSIONS            │
│   ┌────────────────────┐         ┌────────────────────────────┐             │
│   │ AskUserQuestion OK │         │ NO questions in chat       │             │
│   │ Respectful pushback│         │ Blockers → OPEN_QUESTIONS  │             │
│   │ Plan alignment     │         │ Fail forward, log warnings │             │
│   │ Proactive next-step│         │ Reinstall deps every run   │             │
│   └────────────────────┘         │ Trust SESSION_STATE.md     │             │
│                                  │ Don't re-litigate DECISIONS│             │
│                                  └────────────────────────────┘             │
│                                                                             │
│   MAXIMUM UTILIZATION RULE: work until limit →                              │
│     priority: (1) handoff quality → (2) output quality → (3) volume         │
└─────────────────────────────────────────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════════════════════
  THE STANDING QUESTION (asked before every output):
  "Is this the highest-quality, most accurate, most thoroughly validated,
   most insightful result I am capable of producing — and if not, what
   specifically do I need to do before I'm willing to call it done?"
═══════════════════════════════════════════════════════════════════════════════
```

## How to read it

The six layers stack vertically, each constraining the one below.

- **Layer 0 — Governance:** the filter every output passes through (prime directive, creativity standard, reasoning standard, 10-point self-review gate).
- **Layer 1 — Session Continuity:** the only thing that survives between cold starts (SESSION_STATE, INSTRUCTIONS, INDEX, PROGRESS_LOG, DECISIONS, OPEN_QUESTIONS).
- **Layer 2 — Data & Source Discipline:** gates what facts are admissible; every claim labeled verified / inferred / speculated.
- **Layer 3 — Analytical Pipeline:** the actual investment workflow — sourcing → strategy analysis → scoring → narrative/web research (mandatory) → thesis synthesis → adversarial kill-test → defense doc → delivery.
- **Layer 4 — Execution Capabilities:** file tools, shell/compute, web/external, orchestration, scheduling, memory.
- **Layer 5 — Skill Library:** invocable skills for document production, data/analysis, and meta/platform tasks.
- **Layer 6 — Autonomous Operating Mode:** governs *how* the tool runs depending on whether a human is present (interactive vs scheduled).

## Caveat on scope

This diagram reflects the tool's **design as specified in the project's governing instructions**. It does not reflect the current state of the workspace's `INSTRUCTIONS.md`, `SESSION_STATE.md`, or the specific strategy specs / scoring rubrics / pipeline contents that live in the project folders — those files were not read during the session that produced this diagram.
