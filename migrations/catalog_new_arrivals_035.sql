begin;
create table if not exists public.catalog_new_arrivals (
    product_id bigint primary key references public.products(id) on delete cascade,
    marked_at timestamptz not null default now()
);
alter table public.catalog_new_arrivals enable row level security;
revoke all on public.catalog_new_arrivals from public, anon, authenticated;
grant select on public.catalog_new_arrivals to service_role;

create or replace function public.warehouse_set_new_arrival(p_product_id bigint, p_sku text, p_enabled boolean)
returns boolean language plpgsql security definer set search_path='' as $$
begin
    if p_enabled is null or not exists (
        select 1 from public.products where id=p_product_id and sku=p_sku and not coalesce(archived,false)
        for update
    ) then raise exception 'Produkt uległ zmianie. Odśwież listę.'; end if;
    if p_enabled then
        insert into public.catalog_new_arrivals(product_id) values (p_product_id) on conflict do nothing;
    else
        delete from public.catalog_new_arrivals where product_id=p_product_id;
    end if;
    return p_enabled;
end $$;
revoke all on function public.warehouse_set_new_arrival(bigint,text,boolean) from public,anon,authenticated;
grant execute on function public.warehouse_set_new_arrival(bigint,text,boolean) to service_role;

create or replace function public.portal_new_arrivals(p_panel text)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare uid uuid := auth.uid();
begin
    if uid is null or not exists(select 1 from auth.users where id=uid and email_confirmed_at is not null)
        then raise insufficient_privilege; end if;
    if p_panel='architect' then
        if not exists(select 1 from public.architect_profiles where id=uid and status='approved')
            then raise insufficient_privilege; end if;
    elsif p_panel='distributor' then
        if (select count(*) from public.client_accounts where user_id=uid and is_active)<>1
            then raise insufficient_privilege; end if;
    else raise insufficient_privilege; end if;
    return (select coalesce(jsonb_agg(jsonb_build_object('sku',c.sku,'name',c.name,'model',c.model)
        order by n.marked_at desc,c.sku),'[]'::jsonb)
        from public.catalog_new_arrivals n
        join public.products p on p.id=n.product_id and not coalesce(p.archived,false)
        join public.client_stock_catalog_v c on c.product_id=p.id
        where p_panel='distributor' or coalesce(c.net_price,0)>0);
end $$;
revoke all on function public.portal_new_arrivals(text) from public,anon,authenticated;
grant execute on function public.portal_new_arrivals(text) to authenticated;
notify pgrst,'reload schema';
commit;
