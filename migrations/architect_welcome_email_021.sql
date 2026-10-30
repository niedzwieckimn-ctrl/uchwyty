-- Welcome email only: extends the existing outbox/worker/Resend integration.
-- No send, backfill, new messaging system, webhook or Auth setting change.
-- Delivery remains disabled until explicitly approved and configured.
begin;
alter table public.architect_order_email_outbox alter column order_id drop not null;
alter table public.architect_order_email_outbox
 add column notification_type text not null default 'order_confirmation'
   check(notification_type in ('order_confirmation','architect_approved','architect_welcome_resend','architect_registered')),
 add column architect_id uuid references public.architect_profiles(id) on delete restrict,
 add constraint architect_email_job_target check(
   (notification_type='order_confirmation' and order_id is not null and architect_id is null) or
   (notification_type in ('architect_approved','architect_welcome_resend','architect_registered') and order_id is null and architect_id is not null));
create unique index architect_welcome_once on public.architect_order_email_outbox(architect_id)
 where notification_type='architect_approved';
create unique index architect_registration_once on public.architect_order_email_outbox(architect_id)
 where notification_type='architect_registered';
alter table architect_portal.email_worker_control
 add column welcome_email_enabled boolean not null default false,
 add constraint architect_welcome_edge_only check(not welcome_email_enabled or active_worker='edge');

create function architect_portal.queue_welcome_email()
returns trigger language plpgsql security definer set search_path='' as $$
begin
 if new.status<>'approved' or old.status is not distinct from new.status then return new; end if;
 if not exists(select 1 from auth.users where id=new.id and email_confirmed_at is not null) then return new; end if;
 perform 1 from architect_portal.email_worker_control where singleton and welcome_email_enabled and active_worker='edge' for share;
 if not found then return new; end if;
 insert into public.architect_order_email_outbox(
   order_id,architect_id,notification_type,recipient,idempotency_key,payload,status,last_error_code)
 values(null,new.id,'architect_approved',lower(btrim(new.email)),
   'architect-approved:'||new.id::text||':v1',jsonb_build_object('template_version','architect-approved-v1'),
   case when new.email ~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$' then 'PENDING' else 'BLOCKED' end,
   case when new.email ~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$' then null else 'MISSING_RECIPIENT' end)
 on conflict(architect_id) where notification_type='architect_approved' do nothing;
 return new;
end $$;
revoke all on function architect_portal.queue_welcome_email() from public,anon,authenticated;
create trigger architect_welcome_on_approval after update of status on public.architect_profiles
 for each row execute function architect_portal.queue_welcome_email();

create or replace function public.architect_email_immutable_snapshot()
returns trigger language plpgsql set search_path=public,pg_temp as $$
begin
 if new.order_id is distinct from old.order_id or new.recipient is distinct from old.recipient
    or new.idempotency_key is distinct from old.idempotency_key or new.payload is distinct from old.payload
    or new.notification_type is distinct from old.notification_type or new.architect_id is distinct from old.architect_id then
   raise exception 'Email delivery snapshot is immutable';
 end if;
 return new;
end $$;

create function public.portal_email_freeze_welcome(p_id uuid,p_owner uuid,p_html text,p_text text,p_attachments jsonb)
returns jsonb language plpgsql security definer set search_path='' as $$
declare j public.architect_order_email_outbox%rowtype;
begin
 select * into j from public.architect_order_email_outbox
 where id=p_id and lease_owner=p_owner and status='DELIVERING' and lease_until>now()
   and notification_type in ('architect_approved','architect_welcome_resend') for update;
 if not found then raise exception 'Welcome email lease unavailable'; end if;
 if j.provider_request ? 'html' then return j.provider_request; end if;
 if j.provider_request->>'_template' is distinct from 'architect-approved-v1'
   or p_html is null or length(p_html)<100 or length(p_html)>100000
   or p_text is null or length(p_text)<50 or length(p_text)>20000
   or jsonb_typeof(p_attachments) is distinct from 'array' or jsonb_array_length(p_attachments)<>4 then
   raise exception 'Invalid welcome email content';
 end if;
 update public.architect_order_email_outbox set
   provider_request=(j.provider_request-'_template')||jsonb_build_object('html',p_html,'text',p_text,'attachments',p_attachments)
   where id=p_id returning provider_request into j.provider_request;
 return j.provider_request;
end $$;
revoke all on function public.portal_email_freeze_welcome(uuid,uuid,text,text,jsonb) from public,anon,authenticated;
grant execute on function public.portal_email_freeze_welcome(uuid,uuid,text,text,jsonb) to service_role;

-- Keep legacy order delivery unchanged and exclude this new notification kind.
create or replace function public.architect_email_claim_legacy(p_owner uuid)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare v_job public.architect_order_email_outbox%rowtype;
begin
  if p_owner is null then raise exception 'Lease owner required'; end if;
  -- A crashed worker may have submitted its email. Do not resend an expired
  -- claim merely because local acknowledgement was lost.
  update public.architect_order_email_outbox set status='UNKNOWN',last_error_code='LEASE_EXPIRED',
    lease_owner=null,lease_until=null,updated_at=now()
    where status='DELIVERING' and lease_until<now() and notification_type='order_confirmation';
  select * into v_job from public.architect_order_email_outbox
    where status in ('PENDING','RETRY') and next_attempt_at<=now() and notification_type='order_confirmation'
    order by next_attempt_at,id limit 1 for update skip locked;
  if not found then return null; end if;
  update public.architect_order_email_outbox set status='DELIVERING',lease_owner=p_owner,
    lease_until=now()+interval '5 minutes',attempt_count=attempt_count+1,updated_at=now()
    where id=v_job.id returning * into v_job;
  return to_jsonb(v_job);
end $$;


-- Existing edge claim; its order branch is unchanged.
create or replace function public.portal_email_claim(p_owner uuid,p_sender text,p_test_recipient text default '')
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
   if j.notification_type in ('architect_approved','architect_welcome_resend') then
     j.provider_request:=jsonb_build_object('from',p_sender,'to',jsonb_build_array(dest),
       'subject',case when p_test_recipient<>'' then '[STAGING] ' else '' end||'Witamy w programie Niedźwieccy dla Architektów!',
       '_template','architect-approved-v1');
   elsif j.notification_type='architect_registered' then
     j.provider_request:=jsonb_build_object('from',p_sender,'to',jsonb_build_array(dest),
       'subject',case when p_test_recipient<>'' then '[STAGING] ' else '' end||'Nowy architekt do zatwierdzenia — Niedźwieccy',
       'text','Architekt potwierdził adres e-mail i oczekuje na zatwierdzenie pracowni.'||E'\n\n'||
          'Pracownia: '||coalesce(j.payload->>'studio_name','')||E'\n'||
          'Architekt: '||coalesce(j.payload->>'name','')||E'\n'||
          'E-mail: '||coalesce(j.payload->>'email','')||E'\n\n'||
          'Otwórz zgłoszenie: https://uchwyty.onrender.com/architects/'||j.architect_id::text);
   else
   subject:=case when p_test_recipient<>'' then '[STAGING] ' else '' end||'Niedźwieccy — potwierdzenie '||coalesce(j.payload->'order'->>'order_no',j.order_id::text);
   select string_agg(coalesce(x->>'name',x->>'sku')||' | SKU '||coalesce(x->>'sku','')||' | '||coalesce(x->>'qty','')||' szt. | '||coalesce(x->>'net_price','')||' '||coalesce(x->>'currency','PLN')||' netto/szt.',E'\n') into body from jsonb_array_elements(j.payload->'items') x;
   body:='Potwierdzamy zapisanie zamówienia '||coalesce(j.payload->'order'->>'order_no',j.order_id::text)||E'.\n\n'||coalesce(body,'')||E'\n\nCeny i pozycje pochodzą z zapisanego zamówienia. To potwierdzenie nie jest fakturą.';
   j.provider_request:=jsonb_build_object('from',p_sender,'to',jsonb_build_array(dest),'subject',subject,'text',body);
   end if;
 end if;
 update public.architect_order_email_outbox set status='DELIVERING',lease_owner=p_owner,
 lease_until=now()+interval '2 minutes',attempt_count=attempt_count+1,
 first_attempt_at=coalesce(first_attempt_at,now()),provider_request=j.provider_request,updated_at=now()
 where id=j.id returning * into j;
 insert into architect_portal.email_attempts(job_id,attempt,owner) values(j.id,j.attempt_count,p_owner);
 return jsonb_build_object('id',j.id,'idempotency_key',j.idempotency_key,'request',j.provider_request);
end $$;


notify pgrst,'reload schema';
commit;
