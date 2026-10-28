-- Run after migration statements within ONE transaction, always ROLLBACK.
-- Uses already-existing staging users and products. No auth mutation.
-- Before running, SET LOCAL portal_test.architect_id / distributor_id to test UUIDs.
-- Run migrations and this suite in the SAME transaction; fixtures 101/102/501.
do $$
declare a uuid=current_setting('portal_test.architect_id')::uuid; d uuid=current_setting('portal_test.distributor_id')::uuid;
  r jsonb; r2 jsonb; c text; k uuid=gen_random_uuid(); oid bigint; n integer; before_stock integer;
begin
  assert not has_function_privilege('anon','public.portal_request(text,jsonb)','execute'),'anon RPC blocked';
  assert not has_function_privilege('authenticated','architect_portal.create_order(uuid,bigint,jsonb)','execute'),'private create blocked';
  assert not has_function_privilege('authenticated','public.architect_create_discounted_order(uuid,bigint,text,uuid,jsonb,text)','execute'),'privileged routine remains blocked';
  assert not exists(select 1 from public.distributor_program_memberships where active),'existing memberships default OFF';
  perform set_config('request.jwt.claim.sub',a::text,true);
  r:=public.portal_request('architect.catalog');
  assert r->>'ok'='true' and jsonb_array_length(r->'products')>0,'architect catalog';
  assert not exists(select 1 from jsonb_array_elements(r->'products') p where p ?| array['net_price','qty_physical','qty_reserved','qty_on_stock','wholesale_net_before']),'architect field isolation';
  r:=public.portal_request('b2b.profile'); assert r->>'status'='403','architect cannot access B2B';
  r:=public.portal_request('architect.create_code','{"distributor_id":101}'); assert r->>'ok'='false','disabled issuance rejected';
  perform public.architect_set_distributor(101,true,'STAGING Salon A','Warszawa',null,null,'test:rollback');
  assert not (select active from public.distributor_program_memberships where customer_id=102),'activation isolated';
  r:=public.portal_request('architect.create_code','{"distributor_id":101}');
  assert r->>'ok'='true','code issued'; c:=r#>>'{code,code}';
  assert c ~ '^[A-Z0-9_]+-[0-9]{4}$','code format';
  perform set_config('request.jwt.claim.sub',d::text,true);
  r:=public.portal_request('b2b.profile'); assert r#>>'{customer,architect_program_active}'='true','active profile';
  r:=public.portal_request('architect.catalog'); assert r->>'status'='403','distributor cannot access architect catalog';
  select qty into before_stock from public.stock where product_id=501;
  r:=public.portal_request('b2b.quote',jsonb_build_object('code',c,'items','[{"product_id":501,"qty":2}]'::jsonb));
  assert r->>'ok'='true','quote';
  r:=public.portal_request('b2b.create_order',jsonb_build_object('idempotency_key',k,'architect_code',c,'note','ROLLBACK-ONLY',
    'customer_id',102,'program_active',false,'items','[{"product_id":501,"qty":2,"unit_net_price":0.01}]'::jsonb));
  assert r->>'ok'='true','atomic discounted order'; oid:=(r#>>'{order,id}')::bigint;
  assert (select customer_id from public.orders where id=oid)=101,'ignore forged customer';
  assert (select unit_net_price from public.order_items where order_id=oid)=92.75,'ignore forged price and correct discount';
  assert (select qty from public.stock where product_id=501)=before_stock,'order does not issue stock';
  assert (select count(*) from public.architect_commission_ledger where sale_line_id in (select id from public.architect_sale_lines where order_item_id in(select id from public.order_items where order_id=oid)))=0,'no wholesale commission';
  r2:=public.portal_request('b2b.create_order',jsonb_build_object('idempotency_key',k,'architect_code',c,'note','ROLLBACK-ONLY','items','[{"product_id":501,"qty":2}]'::jsonb));
  assert r2#>>'{order,id}'=oid::text and r2->>'duplicate'='true','same key same order';
  r2:=public.portal_request('b2b.create_order',jsonb_build_object('idempotency_key',k,'architect_code',c,'note','changed','items','[{"product_id":501,"qty":2}]'::jsonb));
  assert r2->>'ok'='false','changed payload conflicts';
  r2:=public.portal_request('b2b.create_order',jsonb_build_object('idempotency_key',gen_random_uuid(),'architect_code',c,'items','[{"product_id":501,"qty":2}]'::jsonb));
  assert r2->>'ok'='false','single-use code';
  perform public.architect_set_distributor(101,false,'STAGING Salon A','Warszawa',null,null,'test:rollback');
  r2:=public.portal_request('b2b.create_order',jsonb_build_object('idempotency_key',k,'architect_code',c,'note','ROLLBACK-ONLY','items','[{"product_id":501,"qty":2}]'::jsonb));
  assert r2#>>'{order,id}'=oid::text,'deactivation preserves accepted replay';
  r2:=public.portal_request('b2b.order',jsonb_build_object('token',r#>>'{order,order_no}'));
  assert r2->>'ok'='true','history and snapshot read after deactivation';
  r2:=public.portal_request('b2b.create_order',jsonb_build_object('idempotency_key',gen_random_uuid(),'items','[{"product_id":501,"qty":1}]'::jsonb));
  assert r2->>'ok'='true','ordinary order while program OFF';
  assert (select unit_net_price from public.order_items where order_id=(r2#>>'{order,id}')::bigint)=100,'ordinary price unchanged';
  set constraints all immediate;
  assert (select count(*) from public.architect_order_email_outbox where order_id=oid)=1,'one durable email snapshot including deferred trigger';
  assert (select jsonb_array_length(payload->'items') from public.architect_order_email_outbox where order_id=oid)=1,'complete email line snapshot';
  -- A bad second line rolls the whole order back (including its first line).
  select count(*) into n from public.orders;
  r2:=public.portal_request('b2b.create_order',jsonb_build_object('idempotency_key',gen_random_uuid(),'items','[{"product_id":501,"qty":1},{"product_id":99999999,"qty":1}]'::jsonb));
  assert r2->>'ok'='false' and (select count(*) from public.orders)=n,'invalid second line leaves no partial order';
  -- Financial example is temporary and all catalog changes are rolled back.
  update public.staging_product_prices set net_price=1121.39 where product_id=501;
  insert into public.architect_product_details(product_id,retail_gross_override) values(501,2000)
    on conflict(product_id) do update set retail_gross_override=excluded.retail_gross_override;
  perform public.architect_set_distributor(101,true,'STAGING Salon A','Warszawa',null,null,'test:rollback');
  perform set_config('request.jwt.claim.sub',a::text,true);
  r2:=public.portal_request('architect.create_code','{"distributor_id":101}'); c:=r2#>>'{code,code}';
  perform set_config('request.jwt.claim.sub',d::text,true);
  r2:=public.portal_request('b2b.quote',jsonb_build_object('code',c,'items','[{"product_id":501,"qty":1}]'::jsonb));
  assert (r2#>>'{lines,0,retail_gross_after}')::numeric=1800 and (r2->>'manufacturer_discount_net')::numeric=81.30 and (r2->>'total_net')::numeric=1040.09,'2000 gross example';
  assert round(1800::numeric/1.23*0.07,2)=102.44,'retail commission basis';
  r2:=public.portal_request('b2b.quote',jsonb_build_object('code',c,'items','[{"product_id":501,"qty":3}]'::jsonb));
  assert (r2->>'total_net')::numeric=3120.27 and (r2->>'manufacturer_discount_net')::numeric=243.90,'quantity change recalculates quote';
  r2:=public.portal_request('b2b.create_order',jsonb_build_object('idempotency_key',gen_random_uuid(),'architect_code',c,'items','[{"product_id":501,"qty":3}]'::jsonb));
  assert r2->>'ok'='true' and (select unit_net_price from public.order_items where order_id=(r2#>>'{order,id}')::bigint)=1040.09,'committed snapshot agrees with quote';
  update public.orders set status='cancelled' where id=(r2#>>'{order,id}')::bigint;
  r2:=public.portal_request('b2b.create_order',jsonb_build_object('idempotency_key',gen_random_uuid(),'architect_code',c,'items','[{"product_id":501,"qty":3}]'::jsonb));
  assert r2->>'ok'='false','cancelled order does not recycle consumed code';
  for n in 1..13 loop r2:=public.portal_request('b2b.quote',jsonb_build_object('code','BAD-0000','items','[{"product_id":501,"qty":1}]'::jsonb)); end loop;
  assert r2->>'status'='429','invalid codes consume persistent quota';
  assert not architect_portal.can_read_object('invoice-pdfs','foreign.pdf'),'foreign storage denied';
  assert (public.portal_request('admin.set_distributor','{}')->>'status')='403','no browser admin operation';
end $$;
set local role authenticated;
select set_config('request.jwt.claim.sub',current_setting('portal_test.architect_id'),true);
do $$ declare r jsonb; begin
  r:=public.portal_request('architect.me'); assert r#>>'{profile,id}'=current_setting('portal_test.architect_id'),'actual authenticated role executes own RPC';
  begin perform architect_portal.catalog('pln',false); raise exception 'private helper leaked'; exception when insufficient_privilege then null; end;
  begin perform * from public.stock; raise exception 'stock leaked'; exception when insufficient_privilege then null; end;
  begin perform * from public.client_stock_catalog_v; raise exception 'wholesale leaked'; exception when insufficient_privilege then null; end;
  assert not exists(select 1 from public.architect_commission_ledger where architect_id<>auth.uid()),'foreign commission RLS';
end $$;
select set_config('request.jwt.claim.sub',current_setting('portal_test.distributor_id'),true);
do $$ declare r jsonb; begin
  r:=public.portal_request('b2b.profile'); assert r#>>'{customer,id}'='101','actual B2B role';
  r:=public.portal_request('b2b.order','{"token":"FOREIGN-NONEXISTENT"}'); assert r->>'status'='403','foreign order denied';
  assert not exists(select 1 from public.architect_commission_ledger),'distributor cannot read architect commissions';
end $$;
set local role anon;
do $$ begin
  begin perform public.portal_request('b2b.catalog'); raise exception 'anon leaked'; exception when insufficient_privilege then null; end;
end $$;
reset role;
select 'PASS: authentication, field isolation, opt-in, code, prices, order, replay, deactivation, ordinary order, rate limit, storage and admin boundaries; transaction rolled back' as result;
rollback;
