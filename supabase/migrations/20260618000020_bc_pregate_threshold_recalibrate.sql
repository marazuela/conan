-- Recalibrate bc_pregate_threshold 6 -> 4 against the real post-enrichment
-- score distribution over the active universe (80 assets):
--   score 0  : 6 assets  (established sponsor, no designation — the noise)
--   score 4  : 71 assets (first-time sponsor)
--   score 10 : 1 asset   (first-time + breakthrough)
--   score 13 : 2 assets  (first-time + breakthrough + priority)
--
-- The legacy threshold 6 was breakthrough-grade and would decline 96% (all but
-- the 3 breakthrough assets) -> pipeline stall. Threshold 4 declines only the 6
-- zero-signal assets (7.5%: Amgen/Pfizer/Incyte standard-review repeats + a
-- Colgate toothpaste), passing every catalyst with a real structural signal.
-- Matches BC_PREGATE_DEFAULT_THRESHOLD in bc-pregate.ts. Runtime dial — reversible.

INSERT INTO public.internal_config(key, value, updated_at)
VALUES ('bc_pregate_threshold', '4', now())
ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now();
