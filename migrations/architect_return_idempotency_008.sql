-- Idempotent return evidence and calendar dates in the program timezone.
begin;
create unique index if not exists architect_return_reference_unique
  on public.architect_commission_ledger(reversal_of,sale_reference)
  where entry_type='return_adjustment';
create or replace function public.architect_record_return(
  p_accrual_id uuid, p_return_qty integer, p_return_date date,
  p_reference text, p_reason text, p_actor text
) returns uuid language plpgsql security definer
set search_path = public, pg_temp set timezone = 'Europe/Warsaw' as $$
declare
  v_original public.architect_commission_ledger%rowtype;
  v_existing public.architect_commission_ledger%rowtype;
  v_refunded_qty integer;
  v_refunded_amount numeric;
  v_amount numeric;
  v_new_id uuid;
begin
  if p_return_qty is null or p_return_qty <= 0
     or p_return_date is null or p_return_date > current_date
     or length(btrim(coalesce(p_reference,''))) < 3
     or length(btrim(coalesce(p_reason,''))) < 5
     or length(btrim(coalesce(p_actor,''))) < 2 then
    raise exception 'Return evidence required';
  end if;
  select * into v_original from public.architect_commission_ledger
    where id=p_accrual_id and entry_type='accrual' for update;
  if not found or p_return_date < v_original.sale_date then
    raise exception 'Original sale not found or return predates sale';
  end if;
  -- The original row lock serializes concurrent retries of the same return.
  select * into v_existing from public.architect_commission_ledger
    where reversal_of=p_accrual_id and entry_type='return_adjustment'
      and sale_reference=btrim(p_reference);
  if found then
    if v_existing.sold_qty=p_return_qty and v_existing.sale_date=p_return_date
       and v_existing.reason=btrim(p_reason) then
      return v_existing.id;
    end if;
    raise exception 'Return reference already used with different evidence';
  end if;
  select coalesce(sum(sold_qty),0),coalesce(sum(amount_net),0)
    into v_refunded_qty,v_refunded_amount
    from public.architect_commission_ledger
    where reversal_of=p_accrual_id and entry_type='return_adjustment';
  if v_refunded_qty+p_return_qty > v_original.sold_qty then
    raise exception 'Returned units exceed sold units';
  end if;
  v_amount := case when v_refunded_qty+p_return_qty=v_original.sold_qty
    then -(v_original.amount_net+v_refunded_amount)
    else -round(v_original.amount_net/v_original.sold_qty*p_return_qty,2) end;
  insert into public.architect_commission_ledger(
    architect_id,sale_line_id,invoice_id,sold_qty,sale_reference,sale_date,
    amount_net,entry_type,status,reason,reversal_of,created_by)
  values(v_original.architect_id,v_original.sale_line_id,v_original.invoice_id,
    p_return_qty,btrim(p_reference),p_return_date,v_amount,
    'return_adjustment','pending',btrim(p_reason),p_accrual_id,p_actor)
  returning id into v_new_id;
  insert into public.architect_audit_events(actor_id,action,entity_type,entity_id,details)
    values(p_actor,'return_recorded','architect_commission_ledger',v_new_id::text,
           jsonb_build_object('original_id',p_accrual_id,'qty',p_return_qty,
                              'reference',p_reference));
  return v_new_id;
end $$;
revoke all on function public.architect_record_return(uuid,integer,date,text,text,text)
  from public, anon, authenticated;
grant execute on function public.architect_record_return(uuid,integer,date,text,text,text)
  to service_role;

alter function public.architect_create_monthly_settlement(uuid,date,text)
  set timezone='Europe/Warsaw';
commit;