-- Schedule the IC memo backlog drainer: every 30 min, up to 5 per tick.
-- Idempotent: unschedule first if it already exists.
-- Kill switch: UPDATE cron.job SET active=false WHERE jobname='ic-memo-backlog-tick';
SELECT cron.unschedule('ic-memo-backlog-tick')
WHERE EXISTS (SELECT 1 FROM cron.job WHERE jobname = 'ic-memo-backlog-tick');

SELECT cron.schedule(
  'ic-memo-backlog-tick',
  '*/30 * * * *',
  $$SELECT public._ic_memo_backlog_tick(5);$$
);
