# Skills layout

Where each Conan skill lives, why, and where new ones go.

## Two live homes

### `conan-cowork-skills/skills/` — Cowork-scheduled tasks

Separate git repo (`marazuela/conan-cowork-skills`). Pedro's Windows Cowork session clones it and runs each `.md` as a recurring scheduled task by filename.

A skill belongs here iff its frontmatter has:

- `host: pedro`
- `host_enrollment:` with a cron string
- `trigger:` describing a recurring schedule
- usually a `quota:` field (per-UTC-day soft cap)

Current residents (10): `signal_resolver`, `thesis_writer`, `candidate_aging`, `bulk_orchestrator_run`, `thesis_challenger`, `coverage_auditor`, `challenger_retro`, `fda_medical_review`, `fda_microstructure_review`, `fda_regulatory_review`.

### `conan-fda-orchestrator-plugin/skills/` — Claude Code plugin

Inside the `marazuela/conan` repo. Bundled with `.claude-plugin/plugin.json`, 8 MCP servers under `mcp_servers/`, and `hooks/`. Loaded by the v3 orchestrator runtime (`orchestrator_runtime/tier2.py`, `orchestrator_runtime/rag_handle.py`) and the Modal sub-agent workers (`modal_workers/sub_agents/*.py`).

A skill belongs here iff its frontmatter has:

- `model:` (typically `claude-sonnet-4-6`)
- `effort:`
- `allowed-tools:` (MCP-server tool list)
- no `host:` field

Current residents (6): `bulk_orchestrator` (inner Tier-2 synthesis), `ic_memo_polish`, `sub_agent_competitive_landscape`, `sub_agent_literature_reviewer`, `sub_agent_options_microstructure`, `sub_agent_regulatory_history`.

## `.claude/skills` is a symlink

`Conan/.claude/skills` is a directory symlink → `conan-cowork-skills/skills/` (created `2026-04-21 17:22`). Do not add files to `.claude/skills/` directly — edit them in the canonical `conan-cowork-skills` repo. Edits via the symlink path reach the same file but commit to the wrong repo if you `git add` from the wrong working tree.

(Historical note: an older memory entry called this "hardlinked" — that was true when `cp -al` was used; it is now a symlink.)

## Where a new skill goes

One rule: **if it has a Pedro cron and runs on Cowork, it goes in `conan-cowork-skills/`. Otherwise it goes in `conan-fda-orchestrator-plugin/skills/`.**

If a skill needs both a Cowork-scheduled trigger AND plugin-context tools, that's a sign you actually have two skills — write a thin runner for Cowork that wraps the plugin skill, or escalate to Pedro.

## Stale homes (deleted / ignored)

- `unified_system/_ARCHIVED_Investment_tool_Delta_2026-04-16/litigation_system/skills/` — removed in v2 teardown phase 1 follow-up (2026-05-11).
- `unified_system/_ARCHIVED_Investmet_tool_Beta_2026-04-16/non_us_discovery_system/.claude/skills/` — removed in same pass (was untracked).
- `Conan/.claude/worktrees/**/skills/` — ephemeral worktree snapshots. Never authoritative.
