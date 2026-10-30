-- Local file only. Apply after 021..023. Same queue, leases and provider keys.
begin;
create or replace function architect_portal.claim_email(p_owner uuid,p_sender text,p_test_recipient text default '',p_job_id uuid default null)
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
 where status in ('PENDING','RETRY') and (p_job_id is null or id=p_job_id) and (next_attempt_at<=now() or p_job_id is not null)
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
revoke all on function architect_portal.claim_email(uuid,text,text,uuid) from public,anon,authenticated;
create or replace function public.portal_email_claim(p_owner uuid,p_sender text,p_test_recipient text default '')
returns jsonb language sql security definer set search_path='' as $$select architect_portal.claim_email(p_owner,p_sender,p_test_recipient,null)$$;
create function public.portal_email_claim_target(p_owner uuid,p_sender text,p_job_id uuid,p_test_recipient text default '')
returns jsonb language sql security definer set search_path='' as $$select architect_portal.claim_email(p_owner,p_sender,p_test_recipient,p_job_id)$$;
revoke all on function public.portal_email_claim_target(uuid,text,uuid,text) from public,anon,authenticated;
grant execute on function public.portal_email_claim_target(uuid,text,uuid,text) to service_role;
create function public.portal_email_dispatch_error(p_id uuid)
returns void language sql security definer set search_path='' as $$
 update public.architect_order_email_outbox set last_error_code='WORKER_UNAVAILABLE',updated_at=now()
 where id=p_id and status in ('PENDING','RETRY');
$$;
revoke all on function public.portal_email_dispatch_error(uuid) from public,anon,authenticated;
grant execute on function public.portal_email_dispatch_error(uuid) to service_role;
notify pgrst,'reload schema';
commit;
