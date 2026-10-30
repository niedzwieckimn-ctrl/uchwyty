-- Architect-only server gate. B2B actions and all rate limits are preserved.
begin;
create or replace function architect_portal.request(p_action text,p_data jsonb default '{}')
returns jsonb language plpgsql security definer set search_path='' as $$
declare uid uuid=auth.uid(); lim integer=180; secs integer=60;
begin
  if uid is null then return jsonb_build_object('ok',false,'status',401,'error','Zaloguj się ponownie'); end if;
  if jsonb_typeof(p_data) is distinct from 'object' or octet_length(p_data::text)>65536 then return jsonb_build_object('ok',false,'status',400,'error','Nieprawidłowe żądanie'); end if;
  if p_action not in ('architect.me','architect.register','architect.catalog','architect.distributors','architect.codes','architect.create_code','architect.samples','architect.create_samples','architect.dashboard','architect.earnings',
      'b2b.profile','b2b.catalog','b2b.quote','b2b.create_order','b2b.orders','b2b.order','b2b.sales','b2b.confirm_sale','b2b.invoices','b2b.invoice_seen','b2b.invoice_file','b2b.image','b2b.search','b2b.aliases') then
    return jsonb_build_object('ok',false,'status',403,'error','Nieznana operacja');
  end if;
  if p_action like 'architect.%' and not exists(
    select 1 from auth.users where id=uid and email_confirmed_at is not null) then
    return jsonb_build_object('ok',false,'status',403,'error','Potwierdź adres e-mail, aby korzystać z programu.');
  end if;
  if p_action='architect.create_code' then lim:=5; secs:=3600;
  elsif p_action in ('architect.register','architect.create_samples') then lim:=3; secs:=3600;
  elsif p_action='b2b.quote' then lim:=12;
  elsif p_action='b2b.create_order' then lim:=12; secs:=600;
  elsif p_action='b2b.confirm_sale' then lim:=8; secs:=3600; end if;
  -- Attempts are outside the exception subtransaction, so rejected codes also
  -- consume quota. RETURN an error envelope, never roll the rate update back.
  if not architect_portal.take_rate(uid,p_action,lim,secs) then
    return jsonb_build_object('ok',false,'status',429,'error','Zbyt wiele prób. Spróbuj później.');
  end if;
  begin
    if p_action in ('architect.dashboard','architect.earnings') and p_data->>'presentation_version'='1' then
      return architect_portal.earnings_view(p_action='architect.earnings');
    end if;
    return architect_portal.dispatch(p_action,p_data);
  exception when insufficient_privilege then return jsonb_build_object('ok',false,'status',403,'error',sqlerrm);
    when raise_exception then return jsonb_build_object('ok',false,'status',400,'error',sqlerrm);
    when data_exception or integrity_constraint_violation or no_data_found then return jsonb_build_object('ok',false,'status',400,'error','Sprawdź dane, dostępność produktów i uprawnienia konta.');
  end;
end $$;

notify pgrst,'reload schema';
commit;
