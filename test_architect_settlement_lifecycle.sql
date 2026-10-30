-- Component test against the existing synthetic staging ledger.
-- No invoice is inserted and no transfer is initiated. All writes roll back.
begin;
set local statement_timeout = '15s';
do $$
declare
  original_id uuid := 'fcdc408f-3fe2-4c5d-b448-0b407b930511';
  architect_id uuid;
  settlement_id uuid;
  adjustment_id uuid;
  correction_batch uuid;
  payment_id uuid;
  period date := date_trunc('month', current_date)::date;
begin
  select l.architect_id into architect_id
    from public.architect_commission_ledger l
    where l.id=original_id and l.status='pending'
      and l.sold_qty=1 and l.amount_net=9.14 and l.settlement_id is null;
  if architect_id is null then raise exception 'Unexpected fixture state'; end if;
  perform public.architect_approve_commission(original_id,'STAGING-QA-SETTLEMENT');
  settlement_id := public.architect_create_monthly_settlement(architect_id,period,'STAGING-QA-SETTLEMENT');
  if not exists(select 1 from public.architect_monthly_settlements s
                where s.id=settlement_id and s.status='draft' and s.total_net=9.14) then
    raise exception 'Draft statement total mismatch';
  end if;
  perform public.architect_approve_monthly_settlement(settlement_id,'STAGING-QA-SETTLEMENT');
  payment_id := public.architect_record_payout(settlement_id,'STAGING-SIMULATED-PAYMENT',now()-interval '1 second','STAGING-QA-SETTLEMENT');
  if not exists(select 1 from public.architect_payouts p where p.id=payment_id and p.amount_net=9.14)
     or not exists(select 1 from public.architect_commission_ledger l where l.id=original_id and l.status='paid') then
    raise exception 'Payment record mismatch';
  end if;
  begin
    perform public.architect_record_payout(settlement_id,'STAGING-DUPLICATE-PAYMENT',now()-interval '1 second','STAGING-QA-SETTLEMENT');
    raise exception 'Duplicate payout unexpectedly accepted';
  exception when others then
    if sqlerrm <> 'Approved positive settlement required' then raise; end if;
  end;
  adjustment_id := public.architect_record_return(original_id,1,current_date,'STAGING-RETURN-QA','Zwrot testowy po oznaczeniu wypłaty','STAGING-QA-SETTLEMENT');
  if not exists(select 1 from public.architect_commission_ledger l
                where l.id=adjustment_id and l.amount_net=-9.14
                  and l.status='pending' and l.reversal_of=original_id)
     or not exists(select 1 from public.architect_monthly_settlements s
                   where s.id=settlement_id and s.status='paid' and s.total_net=9.14) then
    raise exception 'Return changed paid history or has wrong value';
  end if;
  perform public.architect_approve_commission(adjustment_id,'STAGING-QA-SETTLEMENT');
  correction_batch := public.architect_create_monthly_settlement(architect_id,period,'STAGING-QA-SETTLEMENT');
  if not exists(select 1 from public.architect_monthly_settlements s
                where s.id=correction_batch and s.status='draft' and s.total_net=-9.14) then
    raise exception 'Correction statement mismatch';
  end if;
  perform public.architect_approve_monthly_settlement(correction_batch,'STAGING-QA-SETTLEMENT');
  begin
    perform public.architect_record_payout(correction_batch,'STAGING-NEGATIVE-PAYMENT',now()-interval '1 second','STAGING-QA-SETTLEMENT');
    raise exception 'Negative payout unexpectedly accepted';
  exception when others then
    if sqlerrm <> 'Approved positive settlement required' then raise; end if;
  end;
  if (select sum(l.amount_net) from public.architect_commission_ledger l
       where l.id=original_id or l.reversal_of=original_id) <> 0 then
    raise exception 'Return does not balance original commission';
  end if;
end $$;
rollback;
select 'PASS: approval, statement, simulated payout, return; transaction rolled back' as test_result,
       status as original_status, amount_net as original_amount
  from public.architect_commission_ledger
  where id='fcdc408f-3fe2-4c5d-b448-0b407b930511';
