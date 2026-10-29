-- Local synthetic database only. No provider calls or production data.
\set ON_ERROR_STOP on
select current_database() like 'test_portal_webhook_%' as safe_db \gset
\if :safe_db
\else
\quit 1
\endif
create schema architect_portal;
do $$begin
 if not exists(select from pg_roles where rolname='anon') then create role anon; end if;
 if not exists(select from pg_roles where rolname='authenticated') then create role authenticated; end if;
 if not exists(select from pg_roles where rolname='service_role') then create role service_role; end if;
end$$;
create table public.architect_order_email_outbox(
 id uuid primary key default gen_random_uuid(),provider_id text,provider_status text,
 created_at timestamptz default now(),provider_checked_at timestamptz,
 status text default 'DELIVERED',attempt_count int default 1,next_attempt_at timestamptz default now());
create function public.portal_email_receipt(uuid,text,text) returns boolean language sql as $$select false$$;
revoke all on function public.portal_email_receipt(uuid,text,text) from public,anon,authenticated;
grant execute on function public.portal_email_receipt(uuid,text,text) to service_role;
\ir migrations/portal_email_webhook_019.sql
begin;
insert into public.architect_order_email_outbox(id,provider_id,provider_status) values
 ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','11111111-1111-1111-1111-111111111111','accepted');
create temp table before_queue as select id,status,attempt_count,next_attempt_at from public.architect_order_email_outbox;
select public.portal_email_webhook('evt-delivery','11111111-1111-1111-1111-111111111111','email.delivered','2026-01-01');
select public.portal_email_webhook('evt-delivery','11111111-1111-1111-1111-111111111111','email.delivered','2026-01-01');
do $$begin
 assert (select count(*)=1 from architect_portal.email_provider_events);
 assert (select provider_status='delivered' from public.architect_order_email_outbox);
 begin
  perform public.portal_email_webhook('evt-delivery','11111111-1111-1111-1111-111111111111','email.failed','2026-01-01');
  raise exception 'Conflicting duplicate was accepted';
 exception when raise_exception then assert sqlerrm='Event identity conflict'; end;
end$$;
-- Late transport events and the older receipt poller cannot undo delivery.
select public.portal_email_webhook('evt-sent','11111111-1111-1111-1111-111111111111','email.sent','2026-01-02');
select public.portal_email_receipt('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','11111111-1111-1111-1111-111111111111','accepted');
select public.portal_email_webhook('evt-open','11111111-1111-1111-1111-111111111111','email.opened','2026-01-03');
do $$begin assert (select provider_status='delivered' from public.architect_order_email_outbox); end$$;
-- Complaints/bounces are retained separately from queue acceptance; no resend.
select public.portal_email_webhook('evt-bounce','11111111-1111-1111-1111-111111111111','email.bounced','2026-01-04');
select public.portal_email_webhook('evt-complaint','11111111-1111-1111-1111-111111111111','email.complained','2026-01-05');
select public.portal_email_webhook('evt-old','11111111-1111-1111-1111-111111111111','email.delivered','2026-01-01');
do $$begin
 assert (select provider_status='complained' from public.architect_order_email_outbox);
 assert not exists((select * from before_queue) except (select id,status,attempt_count,next_attempt_at from public.architect_order_email_outbox));
end$$;
-- Webhook arrives before the worker commits its provider ID.
select public.portal_email_webhook('evt-early','22222222-2222-2222-2222-222222222222','email.delivered','2026-01-01');
do $$begin assert exists(select from architect_portal.email_provider_events where event_id='evt-early' and processed_at is null); end$$;
insert into public.architect_order_email_outbox(provider_id,provider_status) values('22222222-2222-2222-2222-222222222222','accepted');
select public.portal_email_sync_events();
do $$begin
 assert exists(select from public.architect_order_email_outbox where provider_id='22222222-2222-2222-2222-222222222222' and provider_status='delivered');
 assert not exists(select from architect_portal.email_provider_events where processed_at is null);
 assert not has_function_privilege('authenticated','public.portal_email_webhook(text,text,text,timestamptz)','EXECUTE');
 assert not has_function_privilege('anon','public.portal_email_sync_events()','EXECUTE');
 assert has_function_privilege('service_role','public.portal_email_webhook(text,text,text,timestamptz)','EXECUTE');
 assert not has_table_privilege('service_role','architect_portal.email_provider_events','SELECT');
 assert not has_function_privilege('authenticated','public.portal_email_receipt(uuid,text,text)','EXECUTE');
end$$;
rollback;
select 'PASS: duplicate/conflict, delivery ordering, race recovery, no resend, service-only access' as result;
