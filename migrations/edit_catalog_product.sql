-- Change the current catalogue record and its price keys in one transaction.
begin;
create or replace function public.edit_catalog_product_v1(p_id bigint, p_expected jsonb, p_new jsonb)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public
set lock_timeout = '5s' as $$
declare
    item public.products%rowtype;
    current_data jsonb;
    sku_new text := p_new->>'sku';
    model_new text := p_new->>'model';
    ean_new text := p_new->>'ean';
    old_key text;
    new_key text := lower(btrim(sku_new));
    old_rate public.pricing%rowtype;
    effective public.pricing%rowtype;
    shared_rate public.pricing%rowtype;
    dest public.pricing%rowtype;
begin
    if jsonb_typeof(p_new) is distinct from 'object' or exists (
        select 1 from unnest(array['sku','model','ean','name']) field
        where jsonb_typeof(p_new->field) is distinct from 'string'
    ) then
        raise exception 'Niepełne dane uchwytu.';
    end if;
    lock table public.products, public.pricing, public.pricing_eur in share row exclusive mode;
    select * into item from public.products where id=p_id and not coalesce(archived,false);
    if not found then raise exception 'Produkt usunięto lub przeniesiono do archiwum.'; end if;
    current_data := jsonb_build_object('sku',coalesce(item.sku,''),'model',coalesce(item.model,''),
        'ean',coalesce(item.ean,''),'name',coalesce(item.name,''));
    old_key := lower(btrim(p_expected->>'sku'));
    if current_data is distinct from p_new and current_data is distinct from p_expected then
        raise exception 'Dane uchwytu zmieniły się. Otwórz aktualne dane przed zapisem.';
    end if;
    if (sku_new <> current_data->>'sku' and sku_new !~ '^[A-Za-z0-9][A-Za-z0-9._/-]{0,79}$')
       or (model_new <> current_data->>'model' and length(btrim(model_new)) not between 1 and 120)
       or (p_new->>'name' <> current_data->>'name' and length(p_new->>'name') > 300) then
        raise exception 'Nieprawidłowy SKU, model lub nazwa uchwytu.';
    end if;
    if ean_new <> current_data->>'ean' and ean_new <> '' then
        if ean_new !~ '^([0-9]{8}|[0-9]{12,14})$' then raise exception 'Niepoprawny EAN.'; end if;
        if (select sum(substr(ean_new,n,1)::int * case when (length(ean_new)-n)%2=1 then 3 else 1 end)
            from generate_series(1,length(ean_new)) n)%10 <> 0 then raise exception 'Niepoprawny EAN.'; end if;
        if exists(select 1 from public.products where id<>p_id and btrim(ean)=ean_new) then
            raise exception 'EAN jest już przypisany do innego produktu.';
        end if;
    end if;
    if sku_new <> current_data->>'sku' and exists (
        select 1 from public.products where id<>p_id and lower(btrim(sku))=new_key
    ) then raise exception 'SKU jest już przypisany do innego produktu.'; end if;
    if sku_new <> current_data->>'sku' or model_new <> current_data->>'model' then
        if exists(select 1 from public.pricing
            where lower(btrim(model)) in (old_key,new_key,lower(btrim(item.model)),lower(btrim(model_new)))
            group by lower(btrim(model)) having count(*)>1) then
            raise exception 'Cennik ma kilka wpisów dla tego kodu. Sprawdź cennik.';
        end if;
        select * into old_rate from public.pricing where lower(btrim(model))=old_key;
        select * into effective from public.pricing where lower(btrim(model))=lower(btrim(item.model));
        if not found then effective := old_rate; end if;
        select * into shared_rate from public.pricing where lower(btrim(model))=lower(btrim(model_new));
        select * into dest from public.pricing where lower(btrim(model))=new_key;
        if (old_key<>new_key and dest.model is not null)
            or (shared_rate.model is not null and (effective.model is null or
                (shared_rate.net_price,shared_rate.gross_price) is distinct from (effective.net_price,effective.gross_price)))
            or (shared_rate.model is null and dest.model is not null and
                (dest.net_price,dest.gross_price) is distinct from (effective.net_price,effective.gross_price)) then
            raise exception 'Nowy kod lub model koliduje z cennikiem PLN.';
        end if;
        if effective.model is not null and shared_rate.model is null and dest.model is null then
            if exists(select 1 from public.products where id<>p_id and lower(btrim(model))=new_key) then
                raise exception 'Ten kod jest modelem innych produktów. Wybierz osobny SKU.';
            end if;
            insert into public.pricing(model,net_price,gross_price,created_at)
                values(sku_new,effective.net_price,effective.gross_price,effective.created_at);
        end if;
        if old_key<>new_key and old_key<>lower(btrim(model_new)) and old_rate.model is not null
            and not exists(select 1 from public.products where id<>p_id and
                (lower(btrim(model))=old_key or lower(btrim(sku))=old_key)) then
            delete from public.pricing where model=old_rate.model;
        end if;
    end if;
    if sku_new <> current_data->>'sku' or ean_new <> current_data->>'ean' then
        if exists(select 1 from public.pricing_eur where lower(btrim(sku))=old_key having count(*)>1)
            or (old_key<>new_key and exists(select 1 from public.pricing_eur where lower(btrim(sku))=new_key)) then
            raise exception 'Konflikt wpisów w cenniku EUR. Sprawdź cennik.';
        end if;
        update public.pricing_eur set sku=sku_new,ean=ean_new where lower(btrim(sku))=old_key;
    end if;
    update public.products set sku=sku_new,
        model=case when model_new=current_data->>'model' then item.model else model_new end,
        name=case when p_new->>'name'=current_data->>'name' then item.name else p_new->>'name' end,
        ean=case when ean_new=current_data->>'ean' then item.ean else ean_new end
        where id=p_id and current_data is distinct from p_new;
    return jsonb_build_object('id',p_id,'changed',current_data is distinct from p_new,
        'products',(select to_jsonb(p) from public.products p where p.id=p_id),
        'pricing',(select coalesce(jsonb_agg(pr),'[]') from public.pricing pr
            where lower(btrim(pr.model)) in (old_key,new_key)),
        'pricing_eur',(select coalesce(jsonb_agg(pe),'[]') from public.pricing_eur pe
            where lower(btrim(pe.sku)) in (old_key,new_key)));
exception when raise_exception then
    -- Roll back every preceding change, including a PLN move on a later EUR conflict.
    return jsonb_build_object('error',sqlerrm);
end;
$$;
revoke all on function public.edit_catalog_product_v1(bigint,jsonb,jsonb) from public,anon,authenticated;
grant execute on function public.edit_catalog_product_v1(bigint,jsonb,jsonb) to service_role;
notify pgrst,'reload schema';
commit;
