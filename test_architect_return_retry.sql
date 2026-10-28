begin;
do $$
declare
  original_id uuid := 'fcdc408f-3fe2-4c5d-b448-0b407b930511';
  first_id uuid;
  retry_id uuid;
  rejected boolean := false;
begin
  first_id := public.architect_record_return(original_id,1,'2026-10-28',
    'STAGING-IDEMPOTENT-RETURN-20261028','Test repeated return','staging-qa');
  retry_id := public.architect_record_return(original_id,1,'2026-10-28',
    'STAGING-IDEMPOTENT-RETURN-20261028','Test repeated return','staging-qa');
  if first_id<>retry_id then raise exception 'Retry duplicated return'; end if;
  if (select count(*) from public.architect_commission_ledger
      where reversal_of=original_id)<>1 then raise exception 'Incorrect return count'; end if;
  if (select amount_net from public.architect_commission_ledger where id=first_id)<>-9.14
    then raise exception 'Wrong correction amount'; end if;
  begin
    perform public.architect_record_return(original_id,1,'2026-10-28',
      'STAGING-IDEMPOTENT-RETURN-20261028','Changed evidence','staging-qa');
  exception when others then
    if sqlerrm not like '%reference already used with different evidence%' then raise; end if;
    rejected := true;
  end;
  if not rejected then raise exception 'Conflicting evidence accepted'; end if;
end $$;
rollback;
select 'PASS: retry returned original ID; changed evidence rejected; test rolled back' as result,
  (select count(*) from public.architect_commission_ledger
   where sale_reference='STAGING-IDEMPOTENT-RETURN-20261028') as remaining_test_returns;
