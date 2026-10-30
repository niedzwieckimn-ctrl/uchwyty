-- Apply to staging after 001..009. No historical backfill and no email send.
-- A deferred trigger captures the completed order and queues its confirmation
-- atomically. The Python worker is inactive without server-side email config.
begin;

create table if not exists public.architect_order_email_outbox (
  id uuid primary key default gen_random_uuid(),
  order_id bigint not null references public.orders(id) on delete restrict,
  recipient text not null,
  idempotency_key text not null unique,
  payload jsonb not null check (jsonb_typeof(payload)='object'),
  status text not null default 'PENDING'
    check (status in ('PENDING','DELIVERING','RETRY','DELIVERED','UNKNOWN','BLOCKED')),
  attempt_count integer not null default 0 check (attempt_count >= 0),
  next_attempt_at timestamptz not null default now(),
  lease_owner uuid,
  lease_until timestamptz,
  provider_id text,
  last_error_code text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  delivered_at timestamptz,
  unique(order_id,recipient)
);
create index if not exists architect_order_email_due
  on public.architect_order_email_outbox(next_attempt_at)
  where status in ('PENDING','RETRY');
create index if not exists architect_order_email_expired
  on public.architect_order_email_outbox(lease_until) where status='DELIVERING';
alter table public.architect_order_email_outbox enable row level security;
revoke all on public.architect_order_email_outbox from public, anon, authenticated, service_role;
grant select on public.architect_order_email_outbox to service_role;

create or replace function public.architect_queue_order_email()
returns trigger language plpgsql security definer
set search_path=public,pg_temp as $$
declare v_order public.orders%rowtype; v_recipient text; v_items jsonb; v_language text;
begin
  select * into strict v_order from public.orders where id=new.order_id;
  v_recipient := lower(btrim(coalesce(v_order.customer_email,'')));
  select coalesce(language,'pl') into v_language from public.customers where id=v_order.customer_id;
  select coalesce(jsonb_agg(jsonb_build_object(
    'sku',oi.sku,'name',coalesce(p.name,oi.sku),'qty',oi.qty,
    'net_price',oi.unit_net_price,'currency',coalesce(oi.currency,'PLN')
  ) order by oi.id),'[]'::jsonb) into v_items
    from public.order_items oi left join public.products p on p.id=oi.product_id
    where oi.order_id=new.order_id;
  insert into public.architect_order_email_outbox(order_id,recipient,idempotency_key,payload,status,last_error_code)
  values(new.order_id,v_recipient,'architect-order:'||new.order_id::text||':'||md5(v_recipient),
    jsonb_build_object('order',jsonb_build_object(
      'id',v_order.id,'order_no',v_order.order_no,'customer_name',v_order.customer_name,
      'customer_email',v_recipient,'note',v_order.note,'language',coalesce(v_language,'pl'),
      'currency',coalesce(v_order.currency,'PLN')),'items',v_items),
    case when v_recipient ~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$'
         and jsonb_array_length(v_items)>0 then 'PENDING' else 'BLOCKED' end,
    case when v_recipient !~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$' then 'MISSING_RECIPIENT'
         when jsonb_array_length(v_items)=0 then 'EMPTY_ORDER' else null end)
  on conflict(order_id,recipient) do nothing;
  return new;
end $$;
drop trigger if exists architect_queue_order_email_after_commit on public.architect_code_redemptions;
create constraint trigger architect_queue_order_email_after_commit
  after insert on public.architect_code_redemptions deferrable initially deferred
  for each row execute function public.architect_queue_order_email();

create or replace function public.architect_email_immutable_snapshot()
returns trigger language plpgsql set search_path=public,pg_temp as $$
begin
  if new.order_id is distinct from old.order_id or new.recipient is distinct from old.recipient
     or new.idempotency_key is distinct from old.idempotency_key or new.payload is distinct from old.payload then
    raise exception 'Email delivery snapshot is immutable';
  end if;
  return new;
end $$;
drop trigger if exists architect_email_snapshot_immutable on public.architect_order_email_outbox;
create trigger architect_email_snapshot_immutable before update on public.architect_order_email_outbox
  for each row execute function public.architect_email_immutable_snapshot();

create or replace function public.architect_email_claim(p_owner uuid)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare v_job public.architect_order_email_outbox%rowtype;
begin
  if p_owner is null then raise exception 'Lease owner required'; end if;
  -- A crashed worker may have submitted its email. Do not resend an expired
  -- claim merely because local acknowledgement was lost.
  update public.architect_order_email_outbox set status='UNKNOWN',last_error_code='LEASE_EXPIRED',
    lease_owner=null,lease_until=null,updated_at=now()
    where status='DELIVERING' and lease_until<now();
  select * into v_job from public.architect_order_email_outbox
    where status in ('PENDING','RETRY') and next_attempt_at<=now()
    order by next_attempt_at,id limit 1 for update skip locked;
  if not found then return null; end if;
  update public.architect_order_email_outbox set status='DELIVERING',lease_owner=p_owner,
    lease_until=now()+interval '5 minutes',attempt_count=attempt_count+1,updated_at=now()
    where id=v_job.id returning * into v_job;
  return to_jsonb(v_job);
end $$;

create or replace function public.architect_email_finish(
  p_id uuid,p_owner uuid,p_status text,p_error_code text default '',p_provider_id text default ''
) returns boolean language plpgsql security definer set search_path=public,pg_temp as $$
declare v_job public.architect_order_email_outbox%rowtype; v_status text;
begin
  if p_status not in ('DELIVERED','RETRY','UNKNOWN','BLOCKED') then raise exception 'Invalid email outcome'; end if;
  select * into v_job from public.architect_order_email_outbox
    where id=p_id and status='DELIVERING' and lease_owner=p_owner for update;
  if not found then return false; end if;
  v_status := case when p_status='RETRY' and v_job.attempt_count>=8 then 'BLOCKED' else p_status end;
  update public.architect_order_email_outbox set status=v_status,
    last_error_code=left(coalesce(p_error_code,''),80),provider_id=nullif(left(coalesce(p_provider_id,''),128),''),
    delivered_at=case when v_status='DELIVERED' then now() else null end,
    next_attempt_at=now()+make_interval(secs=>least(3600,30*power(2,least(v_job.attempt_count,7))::integer)),
    lease_owner=null,lease_until=null,updated_at=now()
    where id=p_id;
  return true;
end $$;

revoke all on function public.architect_queue_order_email() from public,anon,authenticated;
revoke all on function public.architect_email_immutable_snapshot() from public,anon,authenticated;
revoke all on function public.architect_email_claim(uuid) from public,anon,authenticated;
revoke all on function public.architect_email_finish(uuid,uuid,text,text,text) from public,anon,authenticated;
grant execute on function public.architect_email_claim(uuid) to service_role;
grant execute on function public.architect_email_finish(uuid,uuid,text,text,text) to service_role;
notify pgrst,'reload schema';
commit;
