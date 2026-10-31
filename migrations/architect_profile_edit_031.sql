-- Run manually after 028..030. No account is changed when this file is run.
begin;
create or replace function public.architect_admin_edit_profile(
 p_architect_id uuid,p_data jsonb,p_expected jsonb,p_actor text,p_check_only boolean default false)
returns void language plpgsql security definer set search_path='' as $$
declare p public.architect_profiles%rowtype; before_data jsonb; k text; v text; maxlen integer;
 keys text[]:=array['first_name','last_name','studio_name','email','phone','instagram','website','shipping_address','nip'];
begin
 if length(btrim(coalesce(p_actor,'')))<2 then raise exception 'Brak operatora'; end if;
 if jsonb_typeof(p_data) is distinct from 'object' or jsonb_typeof(p_expected) is distinct from 'object'
 or (p_data - keys)<>'{}'::jsonb or (p_expected - keys)<>'{}'::jsonb
 or (select count(*) from jsonb_object_keys(p_data))<>9
 or (select count(*) from jsonb_object_keys(p_expected))<>9 then raise exception 'Nieprawidłowe dane'; end if;
 select * into strict p from public.architect_profiles where id=p_architect_id for update;
 if p.removed_at is not null then raise exception 'Konto zostało trwale usunięte z programu'; end if;
 select jsonb_object_agg(x,coalesce(to_jsonb(p)->>x,'')) into before_data from unnest(keys) x;
 if before_data is distinct from p_expected then raise exception 'Dane zmieniły się w międzyczasie. Odśwież kartę'; end if;
 foreach k in array keys loop
   v:=p_data->>k;
   maxlen:=case k when 'first_name' then 100 when 'last_name' then 100 when 'studio_name' then 200
     when 'email' then 254 when 'phone' then 60 when 'instagram' then 250
     when 'website' then 500 when 'shipping_address' then 1000 else 30 end;
   if jsonb_typeof(p_data->k) is distinct from 'string' or length(v)>maxlen then raise exception 'Nieprawidłowe dane'; end if;
   if k not in ('website','nip') and length(btrim(v))<1 then raise exception 'Uzupełnij wymagane dane'; end if;
 end loop;
 if length(p_data->>'instagram')<2 or p_data->>'email' !~ '^[^[:space:]@,;<>]+@[^[:space:]@,;<>]+\.[^[:space:]@,;<>]+$'
 then raise exception 'Nieprawidłowe dane'; end if;
 perform pg_advisory_xact_lock(hashtextextended(lower(btrim(p_data->>'email')),28));
 if exists(select 1 from public.architect_profiles a where a.id<>p.id and lower(btrim(a.email))=lower(btrim(p_data->>'email')))
 or exists(select 1 from auth.users u where u.id<>p.id and lower(btrim(u.email))=lower(btrim(p_data->>'email')))
 then raise exception 'Ten adres e-mail jest już przypisany do innego konta'; end if;
 if p_check_only then return; end if;
 -- Auth owns login addresses and is updated through its Admin API, never here.
 if lower(btrim(p_data->>'email')) is distinct from lower(btrim(p.email)) and not exists(
   select 1 from auth.users u where u.id=p.id and lower(btrim(u.email))=lower(btrim(p_data->>'email')))
 then raise exception 'Najpierw popraw adres do logowania'; end if;
 if before_data=p_data then return; end if;
 update public.architect_profiles set first_name=p_data->>'first_name',last_name=p_data->>'last_name',
 studio_name=p_data->>'studio_name',email=p_data->>'email',phone=p_data->>'phone',instagram=p_data->>'instagram',
 website=nullif(p_data->>'website',''),shipping_address=p_data->>'shipping_address',nip=nullif(p_data->>'nip','') where id=p.id;
 insert into public.architect_audit_events(actor_id,action,entity_type,entity_id,details)
 values(p_actor,'profile_edited','architect_profile',p.id::text,jsonb_build_object('before',before_data,'after',p_data));
end $$;
revoke all on function public.architect_admin_edit_profile(uuid,jsonb,jsonb,text,boolean) from public,anon,authenticated;
grant execute on function public.architect_admin_edit_profile(uuid,jsonb,jsonb,text,boolean) to service_role;
-- A manual notification after an email correction gets its own recipient-specific key.
-- Existing provider requests are immutable; same-recipient jobs keep their old key.
create or replace function public.architect_notify_sample_delay(p_sample_id uuid,p_actor text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare s public.architect_sample_orders%rowtype; a public.architect_profiles%rowtype;
 items jsonb; dest text; key text; job public.architect_order_email_outbox%rowtype;
begin
 if length(btrim(coalesce(p_actor,'')))<2 then raise exception 'Brak operatora'; end if;
 select * into strict s from public.architect_sample_orders where id=p_sample_id for update;
 if s.status not in ('requested','preparing') or s.fulfillment_order_id is not null then
   raise exception 'Ten pakiet nie oczekuje na przygotowanie'; end if;
 select * into strict a from public.architect_profiles where id=s.architect_id for update;
 if a.status<>'approved' or a.removed_at is not null then raise exception 'Konto architekta nie jest aktywne'; end if;
 select lower(btrim(u.email)) into dest from auth.users u where u.id=a.id and u.email_confirmed_at is not null;
 if dest is null or dest is distinct from lower(btrim(a.email)) or dest !~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$' then
   raise exception 'Nie potwierdzono aktualnego adresu e-mail architekta'; end if;
 perform 1 from architect_portal.email_worker_control where singleton and active_worker='edge' for share;
 if not found then raise exception 'Wysyłka wiadomości nie jest jeszcze skonfigurowana'; end if;
 if exists(select 1 from public.architect_sample_items si left join public.stock st on st.product_id=si.product_id
           where si.sample_order_id=p_sample_id and si.cancelled_at is null and st.product_id is null) then
   raise exception 'Nie można potwierdzić dostępności próbek'; end if;
 select jsonb_agg(jsonb_build_object('product_id',si.product_id,'sku',p.sku) order by si.product_id)
 into items from public.architect_sample_items si join public.products p on p.id=si.product_id
 join public.stock st on st.product_id=si.product_id
 where si.sample_order_id=p_sample_id and si.cancelled_at is null and st.qty<si.qty;
 if items is null then raise exception 'Wszystkie wybrane próbki są dostępne w magazynie'; end if;
 key:='architect-sample-delay:v3:'||p_sample_id::text||':'||md5(items::text||':'||dest);
 -- Preserve a sent, pending, ambiguous or otherwise rejected original job.
 -- Only the confirmed blocked/null-subject failure gets a corrected new job.
 select * into job from public.architect_order_email_outbox
 where lower(btrim(recipient))=dest and idempotency_key in (key,
   'architect-sample-delay:v2:'||p_sample_id::text||':'||md5(items::text),
   'architect-sample-delay:'||p_sample_id::text||':'||md5(items::text))
 order by created_at desc limit 1;
 if found then
   if job.idempotency_key=key or job.status<>'BLOCKED' or job.provider_id is not null
      or coalesce(job.last_error_code,'') not in ('PROVIDER_REJECTED','INVALID_MESSAGE_SUBJECT')
      or (jsonb_typeof(job.provider_request->'subject')='string' and length(btrim(job.provider_request->>'subject'))>0) then
     return jsonb_build_object('job_id',job.id,'queued',job.status in ('PENDING','RETRY'));
   end if;
 end if;
 insert into public.architect_order_email_outbox(order_id,architect_id,notification_type,recipient,idempotency_key,payload)
 values(null,a.id,'architect_sample_delay',dest,key,
 jsonb_build_object('sample_order_id',p_sample_id,'items',items,'lead_days',14,'template_version','sample-delay-v1'))
 returning * into job;
 insert into public.architect_audit_events(actor_id,action,entity_type,entity_id,details)
 values(p_actor,'sample_delay_email_requested','architect_sample_order',p_sample_id::text,
 jsonb_build_object('job_id',job.id,'lead_days',14));
 return jsonb_build_object('job_id',job.id,'queued',true);
end $$;
revoke all on function public.architect_notify_sample_delay(uuid,text) from public,anon,authenticated;
grant execute on function public.architect_notify_sample_delay(uuid,text) to service_role;


notify pgrst,'reload schema';
commit;
