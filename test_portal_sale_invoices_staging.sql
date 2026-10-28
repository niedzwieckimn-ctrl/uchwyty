begin;
select set_config('request.jwt.claim.sub','0062dcbf-717e-40a7-a820-3f79aefaa33f',true);
do $$ declare r jsonb; item jsonb; begin
  r:=public.portal_request('b2b.sales','{"order_id":13}');
  assert r->>'ok'='true','history readable while program is OFF';
  select value into item from jsonb_array_elements(r->'lines') where value->>'product_id'='502';
  assert jsonb_array_length(item->'invoices')=0,'sold partial invoice cannot be reused';
  select value into item from jsonb_array_elements(r->'lines') where value->>'product_id'='503';
  assert jsonb_array_length(item->'invoices')=1,'only actual allocated invoice';
  assert item#>>'{invoices,0,id}'='8' and item#>>'{invoices,0,remaining_qty}'='2','combined invoice retains source order quantity';
end $$;
select 'PASS: partial and combined invoice selection, sold quantities, history with program OFF' as result;
rollback;
