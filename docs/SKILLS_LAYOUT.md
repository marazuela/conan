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

Current residents exclude the retired Tier-2 `bulk_orchestrator_run`. Cron-backed skills should now be limited to operational follow-up work such as thesis writing, aging, coverage audit, and feedback retrospectives.

### `conan-fda-orchestrator-plugin/skills/` — Claude Code plugin

Inside the `marazuela/conan` repo. Bundled with `.claude-plugin/plugin.json`, MCP servers under `mcp_servers/`, and `hooks/`. The live v4 orchestrator no longer loads plugin skills as production sub-agent stages; these files are retained for diagnostics, IC memo polish, and eval-only sidecar experiments.

A skill belongs here iff its frontmatter has:

- `model:` (typically `claude-sonnet-4-6`)
- `effort:`
- `allowed-tools:` (MCP-server tool list)
- no `host:` field

Current residents: `ic_memo_polish`, `sub_agent_competitive_landscape`, `sub_agent_literature_reviewer`, `sub_agent_options_microstructure`, `sub_agent_regulatory_history`. The old `bulk_orchestrator` Tier-2 skill is retired and should not be scheduled.

## `.claude/skills` is a symlink

`Conan/.claude/skills` is a directory symlink → `conan-cowork-skills/skills/` (created `2026-04-21 17:22`). Do not add files to `.claude/skills/` directly — edit them in the canonical `conan-cowork-skills` repo. Edits via the symlink path reach the same file but commit to the wrong repo if you `git add` from the wrong working tree.

(Historical note: an older memory entry called this "hardlinked" — that was true when `cp -al` was used; it is now a symlink.)

## Where a new skill goes

One rule: **if it has a Pedro cron and runs on Cowork, it goes in `conan-cowork-skills/`. Otherwise it goes in `conan-fda-orchestrator-plugin/skills/`.**

If a skill needs both a Cowork-scheduled trigger AND plugin-context tools, that's a sign you actually have two skills — write a thin runner for Cowork that wraps the plugin skill, or escalate to Pedro.

## Run tracking

Every operational skill should write `public.skill_runs` when it starts,
heartbeat while processing work, and finish with a terminal status. Configure
`public.skill_run_expectations` only for skills that are expected to run on a
cadence; `_skill_run_watchdog()` raises `operator_flags` under
`source='skill_watchdog'` when a configured skill is silent or stuck running.

## Stale homes (deleted / ignored)

- `unified_system/_ARCHIVED_Investment_tool_Delta_2026-04-16/litigation_system/skills/` — removed in v2 teardown phase 1 follow-up (2026-05-11).
- `unified_system/_ARCHIVED_Investmet_tool_Beta_2026-04-16/non_us_discovery_system/.claude/skills/` — removed in same pass (was untracked).
- `Conan/.claude/worktrees/**/skills/` — ephemeral worktree snapshots. Never authoritative.
