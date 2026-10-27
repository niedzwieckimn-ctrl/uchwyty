-- Run inside a transaction and ROLLBACK. No test products survive this check.
set local role service_role;
do $$
declare
  request_key uuid := gen_random_uuid();
  data jsonb := '{"sku":"TEST-CATALOG-ROLLBACK-20261027","model":"TEST-CATALOG-ROLLBACK-20261027","name":"Rollback test","ean":"","net_price":"100.00","gross_price":"123.00","price_eur":"33.72","uvp_eur":"48.89"}';
  first_result jsonb;
  again jsonb;
  pid bigint;
begin
  first_result := public.create_catalog_product_v1(request_key,data);
  if first_result->>'error' is not null then raise exception 'Create failed: %',first_result; end if;
  pid := (first_result->>'product_id')::bigint;
  if (first_result->'pricing_eur'->>'price_eur')::numeric <> 33.72 then raise exception 'Wrong EUR price'; end if;
  update public.stock set qty=7 where product_id=pid;
  again := public.create_catalog_product_v1(request_key,data);
  if again<>first_result or (select qty from public.stock where product_id=pid)<>7 then raise exception 'Replay changed stock'; end if;
  again := public.create_catalog_product_v1(gen_random_uuid(),data);
  if again->>'error' is null then raise exception 'Duplicate SKU accepted'; end if;
  again := public.create_catalog_product_v1(request_key,data||'{"net_price":"200.00"}'::jsonb);
  if again->>'error' is null then raise exception 'Changed retry accepted'; end if;
  again := public.create_catalog_product_v1(gen_random_uuid(), data||'{"sku":"TEST-CATALOG-ROLLBACK-VARIANT"}'::jsonb);
  if again->>'error' is not null then raise exception 'Shared price creation failed'; end if;
  if (select count(*) from public.products p join public.pricing pr on pr.model=p.model or pr.model=p.sku
      where p.id=(again->>'product_id')::bigint)<>1 then raise exception 'Shared price duplicates product'; end if;
  begin
    perform public.create_catalog_product_v1(gen_random_uuid(),data||'{"sku":"TEST-CATALOG-ROLLBACK-FAIL","model":"TEST-CATALOG-ROLLBACK-FAIL","image":{"stored_path":""}}'::jsonb);
    raise exception 'Expected image failure' using errcode='ZX001';
  exception when others then
    if sqlstate='ZX001' then raise; end if;
  end;
  if exists(select 1 from public.products where sku='TEST-CATALOG-ROLLBACK-FAIL') then raise exception 'Partial product survived'; end if;
end;
$$;
reset role;
select 'PASS: atomic create, EUR, duplicate, replay, rollback' as verification;
