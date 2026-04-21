-- supabase_realtime publication: register signals, alerts, candidates
-- so dashboard WebSocket subscribers (dashboard/app/(app)/signals/realtime-banner.tsx)
-- receive INSERT events. Without this, commit 9 realtime is silent.
--
-- alerts + candidates added now for spec §6.2 (alerts realtime-broadcast) and
-- commit 12 Kanban (no subscriber yet; future-proofed).
--
-- RLS check: signals_select / alerts_select / candidates_select all USING(true)
-- for authenticated role → Realtime will not filter out subscribers.
-- REPLICA IDENTITY: default suffices; banner reads INSERT payloads only.

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_publication_tables
    WHERE pubname='supabase_realtime' AND schemaname='public' AND tablename='signals'
  ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.signals;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_publication_tables
    WHERE pubname='supabase_realtime' AND schemaname='public' AND tablename='alerts'
  ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.alerts;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_publication_tables
    WHERE pubname='supabase_realtime' AND schemaname='public' AND tablename='candidates'
  ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.candidates;
  END IF;
END $$;
