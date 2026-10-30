-- Uses the existing outbox and Resend worker. No historical mail sends.
begin;
create function architect_portal.queue_registration_email()
returns trigger language plpgsql security definer set search_path='' as $$
begin
 if not exists(select 1 from auth.users where id=new.id and email_confirmed_at is not null) then return new; end if;
 perform 1 from architect_portal.email_worker_control where singleton and welcome_email_enabled and active_worker='edge' for share;
 if not found then return new; end if;
 insert into public.architect_order_email_outbox(order_id,architect_id,notification_type,recipient,idempotency_key,payload)
 values(null,new.id,'architect_registered','biuro@niedzwieccy.com','architect-registered:'||new.id::text||':v1',
   jsonb_build_object('studio_name',new.studio_name,'name',new.first_name||' '||new.last_name,'email',new.email))
 on conflict(architect_id) where notification_type='architect_registered' do nothing;
 return new;
end $$;
revoke all on function architect_portal.queue_registration_email() from public,anon,authenticated;
create trigger architect_registration_notification after insert on public.architect_profiles
 for each row execute function architect_portal.queue_registration_email();

-- Explicit administrator resend. The profile lock serializes concurrent clicks;
-- retrying the same POST reuses its UUID and cannot queue another message.
create function public.architect_resend_welcome(p_architect_id uuid,p_request_id uuid,p_actor text)
returns uuid language plpgsql security definer set search_path='' as $$
declare a public.architect_profiles%rowtype; job uuid; recent timestamptz;
begin
 if p_request_id is null or nullif(btrim(p_actor),'') is null then raise exception 'Nieprawidłowe żądanie'; end if;
 select * into a from public.architect_profiles where id=p_architect_id for update;
 if not found or a.status<>'approved' then raise exception 'Wiadomość jest dostępna tylko dla zatwierdzonego konta'; end if;
 if not exists(select 1 from auth.users where id=a.id and email_confirmed_at is not null) then raise exception 'Adres e-mail nie został potwierdzony'; end if;
 perform 1 from architect_portal.email_worker_control where singleton and welcome_email_enabled and active_worker='edge' for share;
 if not found then raise exception 'Wysyłka wiadomości powitalnych nie jest jeszcze uruchomiona'; end if;
 select id into job from public.architect_order_email_outbox where idempotency_key='architect-welcome-resend:'||p_request_id::text and architect_id=a.id;
 if found then return job; end if;
 select id into job from public.architect_order_email_outbox where architect_id=a.id
   and notification_type in ('architect_approved','architect_welcome_resend')
   and status in ('PENDING','RETRY','DELIVERING') order by created_at desc limit 1;
 if found then return job; end if;
 if exists(select 1 from public.architect_order_email_outbox where architect_id=a.id
   and notification_type in ('architect_approved','architect_welcome_resend') and status='UNKNOWN') then
   raise exception 'Poprzednia wysyłka wymaga sprawdzenia statusu przed ponowieniem';
 end if;
 select max(created_at) into recent from public.architect_order_email_outbox where architect_id=a.id
   and notification_type in ('architect_approved','architect_welcome_resend');
 if recent>now()-interval '60 seconds' then raise exception 'Odczekaj minutę przed ponownym wysłaniem'; end if;
 insert into public.architect_order_email_outbox(order_id,architect_id,notification_type,recipient,idempotency_key,payload)
 values(null,a.id,'architect_welcome_resend',lower(btrim(a.email)),
   'architect-welcome-resend:'||p_request_id::text,jsonb_build_object('template_version','architect-approved-v1')) returning id into job;
 insert into public.architect_audit_events(actor_id,action,entity_type,entity_id,details)
 values(p_actor,'welcome_email_resent','architect_profile',a.id::text,jsonb_build_object('email_job_id',job));
 return job;
end $$;
revoke all on function public.architect_resend_welcome(uuid,uuid,text) from public,anon,authenticated;
grant execute on function public.architect_resend_welcome(uuid,uuid,text) to service_role;
notify pgrst,'reload schema';
commit;
