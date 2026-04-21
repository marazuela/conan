# INDEX — Non-US Primary-Source Discovery System (Tool 2)

Live file inventory. Grep here first before spelunking subfolders. Updated in the same turn any file is created or substantially changed.

## Root

- `PROJECT_INSTRUCTIONS.md` — project charter, reasoning standard, session discipline.
- `README.md` — entry point, cold-start read order, common commands.
- `OBJECTIVES.md` — primary goal, mandate, 9-strategy table, success criteria, definition of done.
- `INSTRUCTIONS.md` — architecture, common signal schema, pipeline, session rules, scheduled tasks, priority queue.
- `CONTEXT.md` — strategy rationale, API endpoint planning + validation status, scoring quick reference, entity resolution protocol, translation confidence convention.
- `DECISIONS.md` — numbered decisions; D-000 through D-004 as of Phase 0.
- `OPEN_QUESTIONS.md` — numbered open questions log; empty at Phase 0.
- `SESSION_STATE.md` — relay baton; current phase, active work units, next queue, tool health, settled decisions.
- `SESSION_LOCK.md` — concurrency gate; UNLOCKED / LOCKED + timestamp + session-id.
- `PROGRESS_LOG.md` — append-only session log.
- `INDEX.md` — this file.

## framework/

- _(pending)_ `framework/scoring_system.md` — 7-dimension rubric, triage gates, worked example.
- _(pending)_ `framework/candidate_template.md` — per-candidate writeup template.

## strategies/

- _(pending)_ `strategies/strategy_uk_lse_rns.md` — UK LSE RNS scanner spec.
- _(pending)_ `strategies/strategy_jp_tdnet.md` — Japan TDnet scanner spec.
- _(pending)_ `strategies/strategy_au_asx.md` — Australia ASX scanner spec.
- _(pending)_ `strategies/strategy_ca_sedar_plus.md` — Canada SEDAR+ scanner spec.
- _(pending)_ `strategies/strategy_hk_hkex.md` — Hong Kong HKEx scanner spec.
- _(pending)_ `strategies/strategy_kr_kind.md` — Korea KIND scanner spec.
- _(pending)_ `strategies/strategy_in_bse_nse.md` — India BSE/NSE scanner spec.
- _(pending)_ `strategies/strategy_br_cvm.md` — Brazil CVM scanner spec.
- _(pending)_ `strategies/strategy_mx_bmv.md` — Mexico BMV scanner spec.

## tools/

- _(pending)_ `tools/openfigi_resolver.py` — (ticker, MIC) → FIGI → issuer_figi.
- _(pending)_ `tools/convergence_engine.py` — 14-day rolling convergence + cross-listing content-hash dedup.
- _(pending)_ `tools/boilerplate_filters.py` — per-exchange boilerplate regex lists for content-similarity dedup.
- _(pending)_ `tools/pipeline_runner.py` — orchestrator dispatching scanners, entity resolution, convergence, scoring.
- _(pending)_ `tools/run_scanner.py` — single-scanner dispatcher (subprocess-isolated).
- _(pending)_ `tools/run_post_scan.py` — post-scan aggregation + convergence.
- _(pending)_ `tools/lse_rns_scanner.py` — Phase 1 deliverable.
- _(pending)_ `tools/tdnet_scanner.py` — Phase 2 deliverable.
- _(pending)_ `tools/asx_scanner.py` — Phase 3 deliverable.
- _(pending)_ `tools/sedar_plus_scanner.py` — Phase 4 deliverable.
- _(pending)_ `tools/hkex_scanner.py` — Phase 5 deliverable.
- _(pending)_ `tools/kind_scanner.py` — Phase 6 deliverable.
- _(pending)_ `tools/india_bse_nse_scanner.py` — Phase 7 deliverable.
- _(pending)_ `tools/cvm_scanner.py` — Phase 8 deliverable.
- _(pending)_ `tools/bmv_scanner.py` — Phase 9 deliverable.

## .claude/skills/

- _(pending)_ `.claude/skills/non-us-operational/SKILL.md` — operational scheduled task.
- _(pending)_ `.claude/skills/non-us-maintenance/SKILL.md` — maintenance scheduled task.
- _(pending)_ `.claude/skills/non-us-performance-report/SKILL.md` — performance-report reader task.
- _(pending)_ `.claude/skills/non-us-deep-dives/SKILL.md` — deep-dive deliverable reader task.

## signals/

- _(empty — populated at first scanner run)_

## candidates/

- _(empty — populated at first 28+ score)_

## reports/

- _(empty — populated at first daily report)_

## working/ and archive/

- _(empty at Phase 0)_
