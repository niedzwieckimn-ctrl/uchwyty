-- Additive receipt processing after 018. No new mail, queue retry or order writes.
begin;
alter table public.architect_order_email_outbox add column provider_event_at timestamptz;
create table architect_portal.email_provider_events (
 event_id text primary key check(length(event_id) between 1 and 128),
 provider_id text not null check(length(provider_id) between 1 and 128),
 event_type text not null check(event_type in ('email.sent','email.delivered','email.delivery_delayed','email.bounced','email.failed','email.complained','email.suppressed','email.scheduled','email.opened','email.clicked')),
 occurred_at timestamptz not null, received_at timestamptz not null default now(), processed_at timestamptz,
 job_id uuid references public.architect_order_email_outbox(id)
);
create index email_provider_events_unprocessed on architect_portal.email_provider_events(provider_id,occurred_at) where processed_at is null;
alter table architect_portal.email_provider_events enable row level security;
revoke all on architect_portal.email_provider_events from public,anon,authenticated,service_role;

create function public.portal_email_sync_events() returns integer
language plpgsql security definer set search_path='' as $$
declare e record; q public.architect_order_email_outbox%rowtype; target text; old_rank integer; new_rank integer; n integer:=0;
begin
 for e in select v.* from architect_portal.email_provider_events v
   where processed_at is null and exists(select 1 from public.architect_order_email_outbox o where o.provider_id=v.provider_id)
   order by occurred_at,event_id limit 100 for update skip locked
 loop
   select * into q from public.architect_order_email_outbox where provider_id=e.provider_id order by created_at limit 1 for update;
   if not found then continue; end if;
   target:=substring(e.event_type from 7);
   old_rank:=case q.provider_status when 'complained' then 6 when 'bounced' then 5 when 'failed' then 5 when 'suppressed' then 5 when 'delivered' then 4 when 'delivery_delayed' then 3 when 'sent' then 2 when 'scheduled' then 1 else 0 end;
   new_rank:=case target when 'complained' then 6 when 'bounced' then 5 when 'failed' then 5 when 'suppressed' then 5 when 'delivered' then 4 when 'delivery_delayed' then 3 when 'sent' then 2 when 'scheduled' then 1 else -1 end;
   if new_rank>old_rank or (new_rank=old_rank and e.occurred_at>=coalesce(q.provider_event_at,'-infinity'::timestamptz)) then
     update public.architect_order_email_outbox set provider_status=target,provider_event_at=e.occurred_at,provider_checked_at=now() where id=q.id;
   end if;
   update architect_portal.email_provider_events set processed_at=now(),job_id=q.id where event_id=e.event_id;
   n:=n+1;
 end loop;
 return n;
end $$;

create function public.portal_email_webhook(p_event_id text,p_provider_id text,p_event_type text,p_occurred_at timestamptz)
returns boolean language plpgsql security definer set search_path='' as $$
declare existing architect_portal.email_provider_events%rowtype;
begin
 if p_occurred_at is null or p_occurred_at>now()+interval '5 minutes' then raise exception 'Invalid event time'; end if;
 insert into architect_portal.email_provider_events(event_id,provider_id,event_type,occurred_at)
 values(p_event_id,p_provider_id,p_event_type,p_occurred_at) on conflict(event_id) do nothing;
 select * into existing from architect_portal.email_provider_events where event_id=p_event_id;
 if existing.provider_id is distinct from p_provider_id or existing.event_type is distinct from p_event_type or existing.occurred_at is distinct from p_occurred_at then raise exception 'Event identity conflict'; end if;
 perform public.portal_email_sync_events();
 return true;
end $$;

-- The older receipt poller may remain running; it cannot downgrade a signed event.
create or replace function public.portal_email_receipt(p_id uuid,p_provider_id text,p_status text)
returns boolean language plpgsql security definer set search_path='' as $$
begin
 if p_status not in ('accepted','sent','delivered','delivery_delayed','bounced','complained','failed','suppressed') then raise exception 'Invalid provider status'; end if;
 update public.architect_order_email_outbox set provider_status=p_status,provider_checked_at=now()
 where id=p_id and provider_id=p_provider_id and provider_event_at is null;
 return found;
end $$;
revoke all on function public.portal_email_sync_events(),public.portal_email_webhook(text,text,text,timestamptz) from public,anon,authenticated;
grant execute on function public.portal_email_sync_events(),public.portal_email_webhook(text,text,text,timestamptz) to service_role;
notify pgrst,'reload schema';
commit;
