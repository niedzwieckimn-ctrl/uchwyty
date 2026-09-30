-- OPTIONAL. Requires separate production approval; this only installs a DISABLED job.
-- Prerequisites: existing pg_cron, pg_net and Vault, and a Vault secret named
-- orderchamp_sync_trigger_token matching Render ORDERCHAMP_SYNC_TRIGGER_TOKEN.
-- Never use the Supabase service-role or Orderchamp API token as this trigger token.
BEGIN;
SET LOCAL statement_timeout='5s';
DO $checks$
BEGIN
  IF (SELECT count(*) FROM pg_extension WHERE extname IN ('pg_cron','pg_net','supabase_vault'))<>3 THEN
    RAISE EXCEPTION 'Required extensions are missing; stop and review before enabling them';
  END IF;
  IF (SELECT count(*) FROM vault.decrypted_secrets
      WHERE name='orderchamp_sync_trigger_token' AND length(decrypted_secret)>=32)<>1 THEN
    RAISE EXCEPTION 'Configure the dedicated trigger secret in Vault first';
  END IF;
END;
$checks$;
SELECT cron.schedule('orderchamp-sync-v2-wake','* * * * *',$job$
  SELECT net.http_post(
    url:='https://uchwyty.onrender.com/api/internal/orderchamp/tick',
    headers:=jsonb_build_object('Content-Type','application/json','Authorization','Bearer '||
      (SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name='orderchamp_sync_trigger_token')),
    body:='{}'::jsonb,timeout_milliseconds:=5000
  );
$job$);
SELECT cron.alter_job(job_id:=jobid,active:=false) FROM cron.job WHERE jobname='orderchamp-sync-v2-wake';
COMMIT;
-- After approving and checking the deployed endpoint, activate manually:
-- SELECT cron.alter_job(job_id:=jobid,active:=true) FROM cron.job WHERE jobname='orderchamp-sync-v2-wake';
-- Disable again with active:=false. Never delete any integration order/history table.
