-- 20260529000000_signal_entity_resolver_sources.sql
-- Add 'signal_entity_resolver_hard_halt' and 'signal_entity_resolver_run' to
-- the operator_flags.source CHECK whitelist.
--
-- Why: the signal_entity_resolver Cowork skill (conan-cowork-skills repo)
-- drains source='bridge_signal_to_v3' flags, seeds fda_assets, and emits its
-- own operator_flags rows: an operator kill-switch ('..._hard_halt', step 0)
-- and a per-run audit summary ('..._run', step 6). Without these whitelist
-- entries those INSERTs raise 23514. Applied out-of-band to the live DB on
-- 2026-05-18 (via execute_sql, because `supabase db push` is blocked by the
-- known migration-ledger drift); this file backfills the tracked migration so
-- a fresh DB / branch reproduces the same constraint. Idempotent — a re-apply
-- on the already-extended live DB is a no-op.
--
-- DRIFT-PROOF BY DESIGN. The live operator_flags_source_check is known to
-- diverge from committed migrations (e.g. it allows 'bridge_signal_to_v3',
-- 'memory_writeback', 'tier2_quality', 'orphan_sweeper' which never reached a
-- committed CHECK migration on main). The earlier codebase idiom rebuilds the
-- CHECK from a hardcoded array, which SILENTLY DROPS any live-only value not
-- in that array. This migration instead reads the *current* constraint
-- definition and appends only the two new tokens to whatever is already
-- allowed — so it can never regress an out-of-band source value.

DO $$
DECLARE
  v_def    text;
  v_tokens text[];
BEGIN
  SELECT pg_get_constraintdef(oid) INTO v_def
  FROM pg_constraint
  WHERE conrelid = 'public.operator_flags'::regclass
    AND conname  = 'operator_flags_source_check';

  IF v_def IS NULL THEN
    RAISE NOTICE 'operator_flags_source_check absent — skipping (nothing to extend)';
    RETURN;
  END IF;

  IF v_def LIKE '%signal_entity_resolver_run%' THEN
    RAISE NOTICE 'signal_entity_resolver sources already present — no-op';
    RETURN;
  END IF;

  -- Extract every single-quoted literal the constraint currently allows.
  -- Preserves all live-only values (drift-proof); '::text' casts and SQL
  -- keywords are unquoted and therefore not captured.
  SELECT array_agg(DISTINCT m[1] ORDER BY m[1])
    INTO v_tokens
  FROM regexp_matches(v_def, '''([^'']+)''', 'g') AS m;

  IF v_tokens IS NULL OR array_length(v_tokens, 1) IS NULL THEN
    RAISE EXCEPTION 'could not parse any source literals from constraintdef: %', v_def;
  END IF;

  v_tokens := v_tokens
              || 'signal_entity_resolver_hard_halt'
              || 'signal_entity_resolver_run';

  EXECUTE 'ALTER TABLE public.operator_flags '
       || 'DROP CONSTRAINT operator_flags_source_check';
  EXECUTE format(
    'ALTER TABLE public.operator_flags '
    'ADD CONSTRAINT operator_flags_source_check '
    'CHECK (source = ANY (%L::text[]))',
    v_tokens
  );

  RAISE NOTICE 'operator_flags_source_check extended (+2): now % allowed values',
               array_length(v_tokens, 1);
END $$;
