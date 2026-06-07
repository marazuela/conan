-- =============================================================================
-- 20260620000010_bc_digest_recipient_allowlist_seed.sql
--
-- Seeds the bc-OWNED digest recipient allowlist, decoupled from the v3
-- notifications_prefs pool. The bc-digest edge fn resolves recipients with this
-- precedence (see functions/bc-digest/index.ts::resolveRecipients):
--   (a) bc_config.l4.digest_recipient_email (this JSON array) if non-empty; else
--   (b) the BC_DIGEST_DEV_RECIPIENTS function secret (you-only warm-up); else
--   (c) notifications_prefs (the v3 pool — explicit future opt-in only).
--
-- Seeded EMPTY ([]) so warm-up falls to BC_DIGEST_DEV_RECIPIENTS (your address,
-- set as a function secret — kept out of this readable table). To widen later:
--   UPDATE public.bc_config
--     SET value = '["a@x.com","b@y.com"]'::jsonb, updated_at = now()
--     WHERE key = 'l4.digest_recipient_email';
--
-- IDEMPOTENT: ON CONFLICT (key) DO NOTHING — never clobbers an operator-set value.
-- =============================================================================

INSERT INTO public.bc_config (key, value, description)
VALUES (
  'l4.digest_recipient_email',
  '[]'::jsonb,
  'BC digest recipient allowlist (JSON array of emails). Empty => fall to BC_DIGEST_DEV_RECIPIENTS env (you-only warm-up). Decoupled from v3 notifications_prefs.'
)
ON CONFLICT (key) DO NOTHING;
