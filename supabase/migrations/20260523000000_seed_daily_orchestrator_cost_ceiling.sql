-- =============================================================================
-- Seed the daily orchestrator cost ceiling into internal_config.
--
-- Replaces the hardcoded $200/day constant that lived in two dashboard files
-- (lib/api/operator/kpis.ts and app/operator/cost/page.tsx). Both files now
-- read this value at request time via internal_config.
--
-- Idempotent: ON CONFLICT (key) DO NOTHING.
-- =============================================================================
INSERT INTO public.internal_config (key, value, updated_at)
VALUES ('daily_orchestrator_cost_ceiling_usd', '200', now())
ON CONFLICT (key) DO NOTHING;
