begin;
select set_config('request.jwt.claim.sub','0062dcbf-717e-40a7-a820-3f79aefaa33f',true);
do $$ declare r jsonb; begin
  r:=public.portal_request('b2b.invoices');
  assert r->>'ok'='true','own invoice history readable';
  assert not exists(select 1 from jsonb_array_elements(r->'invoices') i where i->>'id' in ('5','7','8')),'unreleased or incomplete metadata never shows a download notice';
  assert (public.portal_request('b2b.invoice_file','{"invoice_id":8}')->>'status')='403','unreleased PDF protected';
end $$;
select 'PASS: unreleased invoices hidden and PDF access denied' as result;
rollback;
