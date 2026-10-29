-- Read models only: no ledger, settlement, payout or commission rule changes.
begin;
create or replace function architect_portal.earnings_view(p_history boolean default false)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare uid uuid=auth.uid(); balance numeric; waiting numeric; corrections numeric;
  paid numeric; codes bigint; result jsonb; entries jsonb; settlements jsonb; payouts jsonb;
begin
  if uid is null or not exists(select 1 from public.architect_profiles where id=uid) then
    raise exception 'Profil architekta nie istnieje' using errcode='42501';
  end if;
  -- Preserve the existing approved/settled balance, including negative batches.
  select coalesce(sum(amount_net) filter(where status in ('approved','settled')),0),
    coalesce(sum(amount_net) filter(where status='pending' and amount_net>0),0),
    -coalesce(sum(amount_net) filter(where status='pending' and amount_net<0),0)
    into balance,waiting,corrections from public.architect_commission_ledger where architect_id=uid;
  -- Payment evidence is authoritative; a later return never rewrites a payment.
  select coalesce(sum(p.amount_net),0) into paid from public.architect_payouts p
    join public.architect_monthly_settlements s on s.id=p.settlement_id
    where s.architect_id=uid and p.paid_at is not null;
  -- Redemption at checkout is not a completed retail purchase. Count a code once
  -- only after a confirmed sale; returns retain its historical use.
  select count(distinct r.code_id) into codes from public.architect_commission_ledger l
    join public.architect_sale_lines sl on sl.id=l.sale_line_id
    join public.architect_code_redemptions r on r.id=sl.redemption_id
    join public.architect_codes c on c.id=r.code_id and c.architect_id=uid
    where l.architect_id=uid and l.entry_type='accrual' and l.sold_qty>0 and l.invoice_id is not null;
  result:=jsonb_build_object('ok',true,'summary',jsonb_build_object(
    'presentation_version',1,'payable',greatest(balance,0)::numeric(14,2)::text,
    'future_deduction',greatest(-balance,0)::numeric(14,2)::text,
    'pending',waiting::numeric(14,2)::text,'pending_corrections',corrections::numeric(14,2)::text,
    'paid',paid::numeric(14,2)::text,'used_codes',codes));
  if not p_history then return result; end if;
  select coalesce(jsonb_agg(to_jsonb(x) order by x.created_at desc,x.id desc),'[]') into entries from (
    select l.id,l.amount_net,l.entry_type,l.status,l.reason,l.sale_date,l.created_at,
      exists(select 1 from public.architect_commission_ledger original
        join public.architect_monthly_settlements s on s.id=original.settlement_id and s.architect_id=uid
        join public.architect_payouts p on p.settlement_id=s.id
        where original.id=l.reversal_of and original.architect_id=uid
          and p.paid_at is not null and p.paid_at<=l.created_at) as after_payout
    from public.architect_commission_ledger l where l.architect_id=uid
    order by l.created_at desc,l.id desc limit 500) x;
  select coalesce(jsonb_agg(to_jsonb(x) order by x.period_month desc,x.batch_no desc),'[]') into settlements from (
    select id,period_month,batch_no,status,total_net from public.architect_monthly_settlements
    where architect_id=uid order by period_month desc,batch_no desc limit 100) x;
  select coalesce(jsonb_agg(to_jsonb(x) order by x.paid_at desc,x.id desc),'[]') into payouts from (
    select p.id,p.amount_net,p.paid_at,s.period_month,s.batch_no
    from public.architect_payouts p join public.architect_monthly_settlements s on s.id=p.settlement_id
    where s.architect_id=uid and p.paid_at is not null order by p.paid_at desc,p.id desc limit 100) x;
  return result || jsonb_build_object('ledger',entries,'settlements',settlements,'payouts',payouts);
end $$;
revoke all on function architect_portal.earnings_view(boolean) from public,anon,authenticated;

-- The existing portal dispatcher and its rate limits continue to protect reads.
-- Only the two presentation responses below change; all writes use dispatch.
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
