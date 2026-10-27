-- Correct cumulative commission rounding on staging databases that already ran 001.
begin;
create or replace function public.architect_record_verified_sale(
  p_sale_line_id bigint, p_invoice_id bigint, p_distributor_id bigint,
  p_sold_qty integer, p_sale_date date, p_sale_reference text,
  p_retail_gross_before numeric, p_retail_gross_after numeric, p_actor text
) returns uuid language plpgsql security definer
set search_path = public, pg_temp as $$
declare
  v_line public.architect_sale_lines%rowtype;
  v_architect_id uuid;
  v_invoiced integer;
  v_accrued integer;
  v_previous_amount numeric;
  v_accrued_for_invoice integer;
  v_rate numeric;
  v_ledger_id uuid;
  v_order_id bigint;
  v_distributor_id bigint;
  v_created_at timestamptz;
begin
  if p_sold_qty is null or p_sold_qty <= 0
     or length(btrim(coalesce(p_sale_reference,''))) < 3
     or length(btrim(coalesce(p_actor,''))) < 2
     or p_sale_date is null or p_sale_date > current_date then
    raise exception 'Retail sale evidence required';
  end if;
  select * into v_line from public.architect_sale_lines
    where id = p_sale_line_id for update;
  if not found then raise exception 'Sale line not found'; end if;
  select r.order_id, r.distributor_id, r.created_at
    into v_order_id, v_distributor_id, v_created_at
    from public.architect_code_redemptions r where r.id = v_line.redemption_id;
  if v_distributor_id is distinct from p_distributor_id
     or p_sale_date < v_created_at::date then
    raise exception 'Sale does not belong to this distributor or date';
  end if;
  if p_retail_gross_before is distinct from
       round(v_line.retail_gross_before / v_line.qty * p_sold_qty,2)
     or p_retail_gross_after is distinct from
       round(v_line.retail_gross_after / v_line.qty * p_sold_qty,2) then
    raise exception 'Retail receipt totals differ from the code price snapshot';
  end if;
  if not exists(select 1 from public.invoices i
                where i.id = p_invoice_id and i.order_id = v_order_id
                  and i.publication_state = 'complete') then
    raise exception 'A completed B2B invoice is required';
  end if;
  select coalesce(sum(a.qty),0) into v_invoiced
    from public.invoice_allocations a
    where a.invoice_id = p_invoice_id and a.order_item_id = v_line.order_item_id;
  if v_invoiced <= 0 then raise exception 'Invoice does not cover this order line'; end if;
  select coalesce(sum(l.sold_qty),0), coalesce(sum(l.amount_net),0)
    into v_accrued, v_previous_amount
    from public.architect_commission_ledger l
    where l.sale_line_id = p_sale_line_id and l.entry_type = 'accrual';
  select coalesce(sum(l.sold_qty),0) into v_accrued_for_invoice
    from public.architect_commission_ledger l
    where l.sale_line_id = p_sale_line_id and l.invoice_id = p_invoice_id
      and l.entry_type = 'accrual';
  if v_accrued_for_invoice + p_sold_qty > v_invoiced
     or v_accrued + p_sold_qty > v_line.qty
     or v_accrued + p_sold_qty > (
       select coalesce(sum(a.qty),0) from public.invoice_allocations a
       where a.order_item_id = v_line.order_item_id
         and exists(select 1 from public.invoices i where i.id = a.invoice_id
                    and i.publication_state = 'complete')
     ) then
    raise exception 'Sold units exceed invoiced units';
  end if;
  select c.architect_id into v_architect_id
    from public.architect_code_redemptions r
    join public.architect_codes c on c.id = r.code_id
    where r.id = v_line.redemption_id;
  select commission_rate into v_rate from public.architect_program_settings
    where version = v_line.program_version;
  -- Round the cumulative commission once, then credit only the difference.
  -- Partial invoices therefore sum to the same amount as one full invoice.
  insert into public.architect_commission_ledger(
    architect_id,sale_line_id,invoice_id,sold_qty,sale_reference,sale_date,
    retail_gross_before,retail_gross_after,retail_net_after,amount_net,
    entry_type,status,reason,created_by)
  values(v_architect_id,p_sale_line_id,p_invoice_id,p_sold_qty,btrim(p_sale_reference),
    p_sale_date,p_retail_gross_before,p_retail_gross_after,
    round(p_retail_gross_after / (1 + v_line.vat_rate),2),
    round((v_line.retail_net_after / v_line.qty) * (v_accrued + p_sold_qty) * v_rate,2)
      - v_previous_amount,
    'accrual','pending','Potwierdzona sprzedaż detaliczna',p_actor)
  returning id into v_ledger_id;
  insert into public.architect_audit_events(actor_id,action,entity_type,entity_id,details)
    values(p_actor,'retail_sale_verified','architect_commission_ledger',v_ledger_id::text,
           jsonb_build_object('sale_line_id',p_sale_line_id,'invoice_id',p_invoice_id,
                              'sold_qty',p_sold_qty,'reference',p_sale_reference));
  return v_ledger_id;
end $$;
revoke all on function public.architect_record_verified_sale(bigint,bigint,bigint,integer,date,text,numeric,numeric,text)
  from public, anon, authenticated;
grant execute on function public.architect_record_verified_sale(bigint,bigint,bigint,integer,date,text,numeric,numeric,text)
  to service_role;
commit;
