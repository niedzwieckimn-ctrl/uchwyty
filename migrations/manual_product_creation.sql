-- Atomic catalogue creation. No existing rows or prices are modified.
begin;
create table if not exists public.product_creation_receipts (
  request_id uuid primary key,
  payload jsonb not null,
  result jsonb not null,
  created_at timestamptz not null default now()
);
alter table public.product_creation_receipts enable row level security;
revoke all on public.product_creation_receipts from public, anon, authenticated;
grant select, insert on public.product_creation_receipts to service_role;

create or replace function public.create_catalog_product_v1(p_request_id uuid, p_data jsonb)
returns jsonb language plpgsql security invoker set search_path = public, pg_temp
set lock_timeout = '5s' set statement_timeout = '20s' as $$
declare
  previous public.product_creation_receipts%rowtype;
  pid bigint;
  iid bigint;
  v_sku text := btrim(p_data->>'sku');
  model_name text := btrim(p_data->>'model');
  ean_value text := coalesce(p_data->>'ean','');
  net numeric := (p_data->>'net_price')::numeric;
  gross numeric := (p_data->>'gross_price')::numeric;
  eur numeric;
  uvp numeric := (p_data->>'uvp_eur')::numeric;
  stamp text := now()::text;
  receipt jsonb;
  price_key text;
begin
  if p_request_id is null or v_sku is null or v_sku !~ '^[A-Za-z0-9][A-Za-z0-9._/-]{0,79}$'
     or coalesce(length(model_name),0) not between 1 and 120
     or coalesce(length(p_data->>'name'),0) > 300
     or net is null or gross is null or uvp is null
     or net not between 0 and 99999999 or gross not between 0 and 99999999 or uvp not between 0 and 99999999 then
    return jsonb_build_object('error','Nieprawidłowe dane produktu lub ceny.');
  end if;
  eur := round(net / 4.30 * 1.45, 2);
  -- Existing imports use explicit IDs, without sequences. Lock allocation and
  -- unique checks together, including conflicts with simultaneous imports.
  lock table public.products, public.stock, public.pricing, public.pricing_eur,
             public.product_images, public.product_image_assignments,
             public.product_creation_receipts in share row exclusive mode;
  select * into previous from public.product_creation_receipts where request_id=p_request_id;
  if found then
    if previous.payload <> p_data then
      return jsonb_build_object('error','Ten formularz został już zapisany z innymi danymi. Otwórz nowy formularz.');
    end if;
    return previous.result;
  end if;
  if exists(select 1 from public.products where lower(btrim(products.sku))=lower(v_sku)) then
    return jsonb_build_object('error','Produkt o tym SKU już istnieje (również w archiwum).');
  end if;
  if ean_value <> '' and exists(select 1 from public.products where ean=ean_value) then
    return jsonb_build_object('error','Ten EAN jest już przypisany do produktu.');
  end if;
  if exists(select 1 from public.pricing where lower(btrim(model))=lower(v_sku))
     or exists(select 1 from public.pricing_eur pe where lower(btrim(pe.sku))=lower(v_sku)) then
    return jsonb_build_object('error','SKU ma już zapisany cennik. Użyj importu katalogu lub innego SKU.');
  end if;
  if exists(select 1 from public.pricing where lower(btrim(model))=lower(model_name)
            and (round(net_price::numeric,2)<>net or round(gross_price::numeric,2)<>gross)) then
    return jsonb_build_object('error','Ten model ma wspólny cennik z inną ceną. Podaj osobny model wariantu lub cenę zgodną z cennikiem.');
  end if;
  select coalesce(max(id),0)+1 into pid from public.products;
  insert into public.products(id,sku,model,ean,name,created_at,archived)
    values(pid,v_sku,model_name,ean_value,p_data->>'name',stamp,false);
  insert into public.stock(product_id,qty) values(pid,0);
  select model into price_key from public.pricing where lower(btrim(model))=lower(model_name) limit 1;
  if not found then
    price_key := v_sku;
    insert into public.pricing(model,net_price,gross_price,created_at) values(price_key,net,gross,stamp);
  end if;
  insert into public.pricing_eur(sku,ean,price_eur,uvp_eur) values(v_sku,ean_value,eur,uvp);
  if p_data->'image' is not null then
    if coalesce(p_data->'image'->>'stored_path','')='' then
      raise exception 'Missing durable image reference';
    end if;
    select id into iid from public.product_images where stored_path=p_data->'image'->>'stored_path';
    if not found then
      select coalesce(max(id),0)+1 into iid from public.product_images;
      insert into public.product_images(id,stored_path,filename)
        values(iid,p_data->'image'->>'stored_path',p_data->'image'->>'filename');
    end if;
    insert into public.product_image_assignments(product_id,image_id) values(pid,iid);
  end if;
  receipt := jsonb_build_object('product_id',pid,'sku',v_sku,
    'products',(select to_jsonb(p) from public.products p where id=pid),
    'stock',(select to_jsonb(s) from public.stock s where product_id=pid),
    'pricing',(select to_jsonb(pr) from public.pricing pr where model=price_key),
    'pricing_eur',(select to_jsonb(pe) from public.pricing_eur pe where pe.sku=v_sku),
    'product_images',(select to_jsonb(i) from public.product_images i where id=iid),
    'product_image_assignments',(select to_jsonb(a) from public.product_image_assignments a where product_id=pid));
  insert into public.product_creation_receipts(request_id,payload,result) values(p_request_id,p_data,receipt);
  return receipt;
end;
$$;
revoke all on function public.create_catalog_product_v1(uuid,jsonb) from public, anon, authenticated;
grant execute on function public.create_catalog_product_v1(uuid,jsonb) to service_role;
commit;
