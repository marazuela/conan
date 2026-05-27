# Supabase migrations — convention + hygiene

Drafted 2026-05-27 alongside the post-PR-#152 migration-drift cleanup.
This document is the authoritative source for how migration files are
named, applied, and reconciled in this repo.

## Why this exists

`supabase migration list` accumulated **173 local files vs 186 remote
applied rows** by 2026-05-27. The mismatch came from two parallel
naming conventions running at the same time:

  - `YYYYMMDD000000` block (manual rounded timestamps, often batched
    per day) — used by `supabase migration new` and by hand.
  - `YYYYMMDDHHMMSS` second precision — used by MCP `apply_migration`
    and by Supabase Studio's "Apply migration" button.

Supabase keys on the version string, not the DDL fingerprint. The
same DDL applied via either path gets a different version, so the
migration tracker treats them as separate migrations. By 2026-05-27
five local files shared the version `20260527000000`, and 70 files
had drifted bodies from their remote counterparts. See
`docs/migration_drift_2026-05-27.md` for the audit.

## Convention (going forward)

### 1. Always use `YYYYMMDDHHMMSS` (UTC, second precision)

```bash
date -u +%Y%m%d%H%M%S
# e.g. 20260527143812
```

Never use `YYYYMMDD000000` blocks, never use `YYYYMMDD0000xx` suffixed
groups. The second-precision timestamp guarantees uniqueness and matches
what Supabase Studio / MCP `apply_migration` stamp on their side.

### 2. New migration via `supabase migration new`

```bash
supabase migration new <descriptive_snake_case_name>
```

The CLI assigns a fresh second-precision timestamp. Don't hand-rename
the file.

### 3. One DDL operation per intent

A migration file should change one thing for one reason. If you're adding
a column AND seeding rows AND adding an index, those are three migrations.
Keeps reverts focused; keeps audits readable.

### 4. Always `idempotent or transactional`

Idempotent guards (`if not exists`, `or replace`) are preferred for new
DDL because they make re-applying the file safe if a deploy mid-fires.
Pure-data migrations (`update ... where ... is null`) should be wrapped
in `begin; … commit;` so a failure rolls back cleanly.

### 5. Apply via `supabase db push` from the xenodochial worktree

Memory: `orchestrator_deploy_topology` — deploys (Modal AND Supabase)
run from the xenodochial worktree gated on `HEAD == origin/main`.

```bash
cd /Users/Pico/Documents/Claude/Projects/Conan/.claude/worktrees/xenodochial-wozniak-e93bf2
git checkout main && git pull
supabase db push
```

### 6. MCP `apply_migration` is for one-shots only

Per memory note `feedback_mcp_apply_migration_discipline`: when the
fix is a single function-body replacement or a single safe ALTER,
MCP `apply_migration` is fine. For anything code-tracked (touched by
the runtime code in the same PR), write a disk file and `db push`.

## Pre-commit hook

`.githooks/pre-commit` rejects new `supabase/migrations/*.sql` files that
violate the convention:

  - Filename version is not `YYYYMMDDHHMMSS` (14 digits)
  - Filename version is `YYYYMMDD000000` (the old block convention)
  - Filename version is already used by another local file
  - Filename version is already in the remote `schema_migrations` table
    *(requires `SUPABASE_DB_URL` env var or `supabase` CLI session;
    skipped silently when neither is available, so local-only commits
    don't get blocked)*

### One-time install

```bash
git config core.hooksPath .githooks
```

The setting is per-clone, not in the repo, so each contributor runs
this once. CI also runs the same script on PRs (see
`.github/workflows/migration-lint.yml`).

## Drift reconciliation

The audit at `docs/migration_drift_2026-05-27.md` classifies every
local file into one of seven buckets and prescribes the cleanup
phases R-1 through R-5. Status as of 2026-05-27:

  - **R-1** (this document + hook) — done in this PR.
  - **R-2** (apply truly-missing v4 + Phase C schema) — done via MCP
    one-shots.
  - **R-3** (delete 66 redundant local files) — PR #155.
  - **R-4** (70 ambiguous body-drift cases) — deferred, needs per-file
    judgment.
  - **R-5** (36 NEW_DDL files, 13 of which need renumbering before
    apply) — deferred.

When R-4 + R-5 land, update memory note `supabase_migrations_drift.md`
to assert local == remote.
