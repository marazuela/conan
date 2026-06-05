-- Security (project-wide): revoke TRUNCATE from anon on all public tables.
--
-- Root cause: the public-schema default privileges granted `anon` the full table ACL
-- (arwdDxtm) on every new table. RLS (enabled on all 99 anon-writable tables) blocks
-- anon SELECT/INSERT/UPDATE/DELETE — BUT TRUNCATE is NOT subject to RLS, so any holder
-- of the public anon key could TRUNCATE any public table (orchestrator_runs, signals,
-- documents, fda_assets, …) — a system-wide availability P0.
--
-- Fix: revoke TRUNCATE from anon on all existing public tables, and from the default
-- privileges so future tables don't re-acquire it. TRUNCATE-from-anon is never a
-- legitimate operation, so this cannot break any read or RLS-gated write. Other write
-- privileges are left intact project-wide (they are RLS-gated, and some tables may have
-- legitimate anon write policies); only the bc_* scoring tables had ALL writes revoked
-- (see 20260619000010 — verified no anon policies there).
--
-- Applied live via MCP 2026-06-05. Idempotent.
--
-- RESIDUAL (cannot be done in-migration): the `supabase_admin`-grantor default ACL still
-- grants anon TRUNCATE on tables created BY supabase_admin (e.g. the Supabase dashboard
-- table editor). `ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin …` requires the
-- supabase_admin role (permission denied for the migration role). Run once via the
-- Supabase dashboard SQL editor (elevated):
--   ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public
--     REVOKE TRUNCATE ON TABLES FROM anon;
-- Practically low-risk here (this project is migration-driven, not dashboard-driven).

REVOKE TRUNCATE ON ALL TABLES IN SCHEMA public FROM anon;

ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE TRUNCATE ON TABLES FROM anon;
