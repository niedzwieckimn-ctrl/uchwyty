-- Enable only after migration 018, function deployment, verified secrets; verify the first provider receipt afterwards.
-- No secret literal belongs in this script or the SQL editor query history.
-- Vault secret portal_email_worker_bearer and Edge secret PORTAL_EMAIL_WORKER_BEARER must contain the SAME existing staging legacy JWT.
-- Its value is the existing staging legacy service_role JWT; never publish it.
begin;
create extension if not exists pg_cron with schema pg_catalog;
create extension if not exists pg_net with schema extensions;
do $$ begin
 if exists(select 1 from public.architect_order_email_outbox where status='DELIVERING') then raise exception 'Wait for all previous leases to finish'; end if;
 if not exists(select 1 from vault.secrets where name='portal_email_worker_bearer') then raise exception 'Worker bearer absent from Vault'; end if;
end $$;
-- Existing Render claim RPC is gated by this singleton. Do not start another mail runner.
update architect_portal.email_worker_control set active_worker='edge',updated_at=now() where singleton;
select cron.schedule('portal-email-staging','* * * * *',$job$
select net.http_post(url:='https://irhrzdsjnwjyqfvhiclk.supabase.co/functions/v1/portal-email',
 headers:=jsonb_build_object('Content-Type','application/json','Authorization','Bearer '||(select decrypted_secret from vault.decrypted_secrets where name='portal_email_worker_bearer')),
 body:='{}'::jsonb,timeout_milliseconds:=55000);
$job$);
commit;
-- Rollback (preserves queue, attempts, orders and receipts):
-- select cron.unschedule('portal-email-staging');
-- update architect_portal.email_worker_control set active_worker='disabled' where singleton;
