-- Additive migration, after 017. Does NOT start delivery or rewrite order data.
begin;
create table architect_portal.email_worker_control (
 singleton boolean primary key default true check(singleton),
 active_worker text not null default 'disabled' check(active_worker in ('disabled','render','edge')),
 updated_at timestamptz not null default now()
);
insert into architect_portal.email_worker_control(singleton) values(true);
alter table architect_portal.email_worker_control enable row level security;
revoke all on architect_portal.email_worker_control from public,anon,authenticated;
alter table public.architect_order_email_outbox
 add column first_attempt_at timestamptz,
 add column provider_request jsonb,
 add column provider_status text,
 add column provider_checked_at timestamptz;
create table architect_portal.email_attempts (
 id bigint generated always as identity primary key,
 job_id uuid not null references public.architect_order_email_outbox(id),
 attempt integer not null, owner uuid not null,
 started_at timestamptz not null default now(), finished_at timestamptz,
 outcome text, error_code text, provider_id text,
 unique(job_id,attempt)
);
alter table architect_portal.email_attempts enable row level security;
revoke all on architect_portal.email_attempts from public,anon,authenticated;
-- Database gate also stops old, already running Render processes.
alter function public.architect_email_claim(uuid) rename to architect_email_claim_legacy;
revoke all on function public.architect_email_claim_legacy(uuid) from public,anon,authenticated,service_role;
create function public.architect_email_claim(p_owner uuid)
returns jsonb language plpgsql security definer set search_path='' as $$
begin
 perform 1 from architect_portal.email_worker_control where singleton and active_worker='render' for share;
 if not found then return null; end if;
 return public.architect_email_claim_legacy(p_owner);
end $$;

create function public.portal_email_claim(p_owner uuid,p_sender text,p_test_recipient text default '')
returns jsonb language plpgsql security definer set search_path='' as $$
declare j public.architect_order_email_outbox%rowtype; body text; subject text; dest text;
begin
 if p_owner is null or length(p_sender)<5 then raise exception 'Invalid worker configuration'; end if;
 perform 1 from architect_portal.email_worker_control where singleton and active_worker='edge' for share;
 if not found then return null; end if;
 -- Retry ambiguous outcomes only within the provider's 24-hour idempotency window.
 update architect_portal.email_attempts a set finished_at=now(),outcome='LEASE_EXPIRED',error_code='LEASE_EXPIRED'
 from public.architect_order_email_outbox q where q.id=a.job_id and a.owner=q.lease_owner
 and a.finished_at is null and q.status='DELIVERING' and q.lease_until<now();
 update public.architect_order_email_outbox set
 status=case when provider_request is not null and first_attempt_at>now()-interval '23 hours' then 'RETRY' else 'UNKNOWN' end,
 last_error_code='LEASE_EXPIRED',lease_owner=null,lease_until=null,updated_at=now()
 where status='DELIVERING' and lease_until<now();
 update public.architect_order_email_outbox set status='UNKNOWN',last_error_code='IDEMPOTENCY_WINDOW_EXPIRED',updated_at=now()
 where status='RETRY' and first_attempt_at<=now()-interval '23 hours';
 select * into j from public.architect_order_email_outbox
 where status in ('PENDING','RETRY') and next_attempt_at<=now()
 order by next_attempt_at,id limit 1 for update skip locked;
 if not found then return null; end if;
 if j.provider_request is null then
   dest:=coalesce(nullif(btrim(p_test_recipient),''),j.recipient);
   if dest !~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$' then raise exception 'Invalid recipient'; end if;
   subject:=case when p_test_recipient<>'' then '[STAGING] ' else '' end||'Niedźwieccy — potwierdzenie '||coalesce(j.payload->'order'->>'order_no',j.order_id::text);
   select string_agg(coalesce(x->>'name',x->>'sku')||' | SKU '||coalesce(x->>'sku','')||' | '||coalesce(x->>'qty','')||' szt. | '||coalesce(x->>'net_price','')||' '||coalesce(x->>'currency','PLN')||' netto/szt.',E'\n') into body from jsonb_array_elements(j.payload->'items') x;
   body:='Potwierdzamy zapisanie zamówienia '||coalesce(j.payload->'order'->>'order_no',j.order_id::text)||E'.\n\n'||coalesce(body,'')||E'\n\nCeny i pozycje pochodzą z zapisanego zamówienia. To potwierdzenie nie jest fakturą.';
   j.provider_request:=jsonb_build_object('from',p_sender,'to',jsonb_build_array(dest),'subject',subject,'text',body);
 end if;
 update public.architect_order_email_outbox set status='DELIVERING',lease_owner=p_owner,
 lease_until=now()+interval '2 minutes',attempt_count=attempt_count+1,
 first_attempt_at=coalesce(first_attempt_at,now()),provider_request=j.provider_request,updated_at=now()
 where id=j.id returning * into j;
 insert into architect_portal.email_attempts(job_id,attempt,owner) values(j.id,j.attempt_count,p_owner);
 return jsonb_build_object('id',j.id,'idempotency_key',j.idempotency_key,'request',j.provider_request);
end $$;

create function public.portal_email_finish(p_id uuid,p_owner uuid,p_status text,p_error_code text default '',p_provider_id text default '')
returns boolean language plpgsql security definer set search_path='' as $$
declare ok boolean;
begin
 ok:=public.architect_email_finish(p_id,p_owner,p_status,p_error_code,p_provider_id);
 if ok then
 update architect_portal.email_attempts set finished_at=now(),outcome=p_status,error_code=left(p_error_code,80),provider_id=nullif(left(p_provider_id,128),'') where job_id=p_id and owner=p_owner and finished_at is null;
 update public.architect_order_email_outbox set provider_status=case when p_status='DELIVERED' then 'accepted' else provider_status end where id=p_id;
 end if;
 return ok;
end $$;
create function public.portal_email_receipts()
returns jsonb language sql security definer set search_path='' as $$
 select coalesce(jsonb_agg(to_jsonb(x)),'[]') from (
 select q.id,q.provider_id from public.architect_order_email_outbox q
 where q.provider_id is not null and q.provider_status in ('accepted','sent','delivery_delayed')
 and (q.provider_checked_at is null or q.provider_checked_at<now()-interval '5 minutes')
 order by q.provider_checked_at nulls first limit 2) x
$$;
create function public.portal_email_receipt(p_id uuid,p_provider_id text,p_status text)
returns boolean language plpgsql security definer set search_path='' as $$
begin
 if p_status not in ('accepted','sent','delivered','delivery_delayed','bounced','complained','failed','suppressed') then raise exception 'Invalid provider status'; end if;
 update public.architect_order_email_outbox set provider_status=p_status,provider_checked_at=now()
 where id=p_id and provider_id=p_provider_id;
 return found;
end $$;
revoke all on function public.architect_email_claim(uuid),public.portal_email_claim(uuid,text,text),public.portal_email_finish(uuid,uuid,text,text,text),public.portal_email_receipts(),public.portal_email_receipt(uuid,text,text) from public,anon,authenticated;
grant execute on function public.architect_email_claim(uuid),public.portal_email_claim(uuid,text,text),public.portal_email_finish(uuid,uuid,text,text,text),public.portal_email_receipts(),public.portal_email_receipt(uuid,text,text) to service_role;
notify pgrst,'reload schema';
commit;
