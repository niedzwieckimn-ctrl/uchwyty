-- Match retail confirmation choices to the actual invoice allocations.
begin;
create or replace function architect_portal.dispatch(p_action text,p_data jsonb)
returns jsonb language plpgsql security definer set search_path='' as $$
declare uid uuid=auth.uid(); cid bigint; profile public.architect_profiles%rowtype;
  customer public.customers%rowtype; account auth.users%rowtype; data jsonb; extra jsonb;
  result jsonb; items jsonb; order_record public.orders%rowtype; issued_code public.architect_codes%rowtype;
  prefix text; candidate text; attempt integer; product_ids bigint[]; sid uuid;
  oid bigint; iid bigint; plist text; program_active boolean; net numeric; gross numeric; retail numeric;
begin
  if uid is null then raise exception 'Zaloguj się ponownie' using errcode='42501'; end if;
  if p_action like 'architect.%' then
    select * into profile from public.architect_profiles where id=uid;
    if p_action='architect.me' then
      select to_jsonb(x) into data from (select id,first_name,last_name,studio_name,email,phone,instagram,website,
        shipping_address,nip,status,sample_entitlements,created_at from public.architect_profiles where id=uid) x;
      return jsonb_build_object('ok',true,'profile',data);
    elsif p_action='architect.register' then
      if profile.id is not null then return architect_portal.dispatch('architect.me','{}'); end if;
      if exists(select 1 from public.client_accounts where user_id=uid and is_active) then raise exception 'To konto należy do dystrybutora' using errcode='42501'; end if;
      select * into strict account from auth.users where id=uid and email_confirmed_at is not null;
      data:=account.raw_user_meta_data;
      if exists(select 1 from unnest(array['first_name','last_name','studio_name','phone','instagram','shipping_address']) k
        where length(btrim(coalesce(data->>k,'')))=0 or length(data->>k)>250) or length(btrim(data->>'shipping_address'))<8 then raise exception 'Uzupełnij wymagane dane rejestracji'; end if;
      insert into public.architect_profiles(id,first_name,last_name,studio_name,email,phone,instagram,website,shipping_address,nip)
      values(uid,btrim(data->>'first_name'),btrim(data->>'last_name'),btrim(data->>'studio_name'),account.email,btrim(data->>'phone'),btrim(data->>'instagram'),left(data->>'website',250),btrim(data->>'shipping_address'),left(data->>'nip',250)) on conflict(id) do nothing;
      return architect_portal.dispatch('architect.me','{}');
    end if;
    if profile.id is null then raise exception 'Profil nie istnieje' using errcode='42501'; end if;
    if p_action='architect.earnings' then
      select coalesce(jsonb_agg(to_jsonb(x)),'[]') into data from (select id,amount_net,entry_type,status,reason,sale_date,created_at
        from public.architect_commission_ledger where architect_id=uid order by created_at desc limit 500) x;
      select coalesce(jsonb_agg(to_jsonb(x)),'[]') into extra from (select id,period_month,batch_no,status,total_net
        from public.architect_monthly_settlements where architect_id=uid order by period_month desc,batch_no limit 100) x;
      return jsonb_build_object('ok',true,'ledger',data,'settlements',extra);
    elsif p_action='architect.dashboard' then
      return jsonb_build_object('ok',true,'summary',jsonb_build_object(
        'active_codes',(select count(*) from public.architect_codes c where c.architect_id=uid and status='active' and expires_at>now() and exists(select 1 from public.distributor_program_memberships d where d.customer_id=c.distributor_id and active)),
        'redeemed_codes',(select count(*) from public.architect_codes where architect_id=uid and status='redeemed'),
        'confirmed_sale_lines',(select count(distinct sale_line_id) from public.architect_commission_ledger where architect_id=uid and entry_type='accrual'),
        'retail_net',(select coalesce(sum(retail_net_after),0) from public.architect_commission_ledger where architect_id=uid and entry_type='accrual'),
        'pending',(select coalesce(sum(amount_net),0) from public.architect_commission_ledger where architect_id=uid and status='pending'),
        'approved',(select coalesce(sum(amount_net),0) from public.architect_commission_ledger where architect_id=uid and status in ('approved','settled')),
        'paid',(select coalesce(sum(amount_net),0) from public.architect_commission_ledger where architect_id=uid and status='paid')));
    end if;
    if profile.status<>'approved' then raise exception 'Konto oczekuje na zatwierdzenie' using errcode='42501'; end if;
    case p_action
      when 'architect.catalog' then return jsonb_build_object('ok',true,'products',architect_portal.catalog('pln',true));
      when 'architect.distributors' then
        select coalesce(jsonb_agg(to_jsonb(x)),'[]') into data from (select customer_id,store_name,city,store_address,contact,latitude,longitude
          from public.distributor_program_memberships where active order by store_name) x;
        return jsonb_build_object('ok',true,'distributors',data);
      when 'architect.codes' then
        select coalesce(jsonb_agg(to_jsonb(x)),'[]') into data from (select id,code,distributor_id,status,created_at,expires_at
          from public.architect_codes where architect_id=uid order by created_at desc limit 200) x;
        return jsonb_build_object('ok',true,'codes',data);
      when 'architect.create_code' then
        cid:=(p_data->>'distributor_id')::bigint;
        perform 1 from public.distributor_program_memberships where customer_id=cid and active for share;
        if not found then raise exception 'Dystrybutor nie uczestniczy w programie'; end if;
        prefix:=left(trim(both '_' from regexp_replace(translate(upper(profile.last_name),'ĄĆĘŁŃÓŚŹŻ','ACELNOSZZ'),'[^A-Z0-9]+','_','g')),28);
        if length(prefix)=0 then raise exception 'Uzupełnij nazwisko architekta'; end if;
        for attempt in 1..30 loop
          candidate:=prefix||'-'||lpad(((('x'||substr(replace(gen_random_uuid()::text,'-',''),1,8))::bit(32)::bigint)%10000)::text,4,'0');
          begin
            issued_code:=public.architect_issue_code(uid,cid,candidate);
            return jsonb_build_object('ok',true,'code',jsonb_build_object('id',issued_code.id,'code',issued_code.code,'distributor_id',cid,'status',issued_code.status,'expires_at',issued_code.expires_at));
          exception when unique_violation then null;
          end;
        end loop;
        raise exception 'Nie udało się przydzielić kodu';
      when 'architect.samples' then
        select coalesce(jsonb_agg(to_jsonb(x)),'[]') into data from (
          select s.id,s.entitlement_no,s.status,s.tracking_no,s.carrier,s.created_at,s.shipped_at,
            (select coalesce(jsonb_agg(jsonb_build_object('product_id',i.product_id,'qty',i.qty,'sku',p.sku,'name',p.name)),'[]')
              from public.architect_sample_items i join public.products p on p.id=i.product_id where i.sample_order_id=s.id) items
          from public.architect_sample_orders s where architect_id=uid order by created_at desc limit 30) x;
        return jsonb_build_object('ok',true,'samples',data);
      when 'architect.create_samples' then
        if jsonb_typeof(p_data->'product_ids') is distinct from 'array' then raise exception 'Wybierz 1–3 różne uchwyty'; end if;
        select array_agg(value::bigint) into product_ids from jsonb_array_elements_text(p_data->'product_ids');
        sid:=public.architect_submit_samples(uid,product_ids,profile.shipping_address);
        return jsonb_build_object('ok',true,'sample_order_id',sid);
      else raise exception 'Nieznana operacja' using errcode='42501';
    end case;
  end if;
  -- One unambiguous active B2B membership, never an email or customer ID from JS.
  if (select count(*) from public.client_accounts where user_id=uid and is_active)<>1 then raise exception 'Brak dostępu dystrybutora' using errcode='42501'; end if;
  select customer_id into cid from public.client_accounts where user_id=uid and is_active;
  select * into strict customer from public.customers where id=cid;
  plist:=case when coalesce(customer.language,'pl')='pl' then 'pln' else 'eu_eur' end;
  program_active:=exists(select 1 from public.distributor_program_memberships where customer_id=cid and active);
  case p_action
    when 'b2b.profile' then return jsonb_build_object('ok',true,'customer',jsonb_build_object(
      'id',cid,'name',customer.name,'address',customer.address,'phone',customer.phone,'email',customer.email,'nip',customer.nip,
      'language',coalesce(customer.language,'pl'),'price_list',plist,'currency',case when plist='pln' then 'PLN' else 'EUR' end,'architect_program_active',program_active));
    when 'b2b.catalog' then return jsonb_build_object('ok',true,'rows',architect_portal.catalog(plist,false),'source','supabase','architect_program_active',program_active,
      'price_list',plist,'currency',case when plist='pln' then 'PLN' else 'EUR' end);
    when 'b2b.quote' then return architect_portal.quote(cid,p_data->>'code',p_data->'items');
    when 'b2b.create_order' then return architect_portal.create_order(uid,cid,p_data);
    when 'b2b.orders' then
      select coalesce(jsonb_agg(to_jsonb(x)),'[]') into data from (select id,order_no,qr_data_url,status,created_at,customer_name,customer_email,note,currency,price_list,tracking_no,carrier,packed_at,shipped_at
        from public.orders where customer_id=cid order by id desc limit 200) x;
      return jsonb_build_object('ok',true,'orders',data);
    when 'b2b.order' then
      select * into order_record from public.orders where customer_id=cid and order_no=p_data->>'token';
      if not found then raise exception 'Brak dostępu do zamówienia' using errcode='42501'; end if;
      data:=architect_portal.catalog(plist,false);
      select coalesce(jsonb_agg(to_jsonb(x)),'[]') into items from (
        select oi.id,oi.order_id,oi.product_id,oi.sku,oi.qty,oi.created_at,oi.currency,p.model,p.name,
          coalesce(oi.unit_net_price,(c->>'net_price')::numeric) net_price,
          coalesce(oi.unit_gross_price,(c->>'gross_price')::numeric) gross_price,
          coalesce(oi.unit_retail_price,(c->>'retail_price')::numeric) retail_price,
          round(coalesce(oi.unit_net_price,(c->>'net_price')::numeric)*oi.qty,2) line_value_net,
          round(coalesce(oi.unit_net_price,(c->>'net_price')::numeric)*oi.qty*(case when order_record.currency='EUR' then 1 else 1.23 end),2) line_value_gross,
          round(coalesce(oi.unit_retail_price,(c->>'retail_price')::numeric)*oi.qty,2) line_value_retail,
          coalesce(a.qty,0) invoiced_qty,greatest(0,oi.qty-coalesce(a.qty,0)) remaining_qty,
          coalesce((c->>'qty_physical')::integer,0) stock_qty,
          case when coalesce(a.qty,0)>=oi.qty then 'w całości' else coalesce(a.qty,0)::text||'/'||oi.qty::text||' szt.' end realization_label
        from public.order_items oi left join public.products p on p.id=oi.product_id
        left join jsonb_array_elements(data) c on (c->>'product_id')::bigint=oi.product_id
        left join (select order_item_id,sum(qty) qty from public.invoice_allocations group by order_item_id) a on a.order_item_id=oi.id
        where oi.order_id=order_record.id order by oi.id) x;
      select coalesce(sum((i->>'line_value_net')::numeric),0),coalesce(sum((i->>'line_value_gross')::numeric),0),coalesce(sum((i->>'line_value_retail')::numeric),0)
        into net,gross,retail from jsonb_array_elements(items) i;
      return jsonb_build_object('ok',true,'order',jsonb_build_object('id',order_record.id,'order_no',order_record.order_no,'status',order_record.status,'created_at',order_record.created_at,'currency',order_record.currency,'note',order_record.note,
        'customer_name',order_record.customer_name,'total_net',net,'total_gross',gross,'total_retail',retail),'items',items);
    when 'b2b.sales' then
      oid:=(p_data->>'order_id')::bigint;
      select r.id into sid from public.architect_code_redemptions r where r.order_id=oid and r.distributor_id=cid;
      if not found then return jsonb_build_object('ok',false,'status',404,'error','Brak kodu dla tego zamówienia'); end if;
      select coalesce(jsonb_agg(to_jsonb(x)),'[]') into data from (
        select l.id,l.order_item_id,l.product_id,l.qty,l.retail_gross_before,l.retail_gross_after,l.vat_rate,
          (select coalesce(sum(sold_qty),0) from public.architect_commission_ledger where sale_line_id=l.id and entry_type='accrual') sold_qty,
          coalesce((select jsonb_agg(to_jsonb(available)) from (
            select i.id,i.invoice_no,greatest(0,sum(a.qty)-coalesce((
              select sum(cl.sold_qty) from public.architect_commission_ledger cl
              where cl.sale_line_id=l.id and cl.invoice_id=i.id and cl.entry_type='accrual'),0)) remaining_qty
            from public.invoice_allocations a join public.invoices i on i.id=a.invoice_id
            where a.order_id=oid and a.order_item_id=l.order_item_id and i.publication_state='complete'
            group by i.id,i.invoice_no) available where available.remaining_qty>0),'[]') invoices
        from public.architect_sale_lines l where redemption_id=sid order by id) x;
      select coalesce(jsonb_agg(to_jsonb(x)),'[]') into extra from (select distinct i.id,i.invoice_no from public.invoices i
        join public.invoice_allocations a on a.invoice_id=i.id where a.order_id=oid and a.qty>0 and i.publication_state='complete') x;
      return jsonb_build_object('ok',true,'lines',data,'invoices',extra);
    when 'b2b.confirm_sale' then
      sid:=public.architect_record_verified_sale((p_data->>'sale_line_id')::bigint,(p_data->>'invoice_id')::bigint,cid,
        (p_data->>'sold_qty')::integer,(p_data->>'sale_date')::date,p_data->>'sale_reference',
        (p_data->>'retail_gross_before')::numeric,(p_data->>'retail_gross_after')::numeric,uid::text);
      return jsonb_build_object('ok',true,'commission_id',sid);
    when 'b2b.invoices' then
      select coalesce(jsonb_agg(to_jsonb(x)),'[]') into data from (
        select i.id,i.order_id,i.invoice_no,i.issue_date,i.payment_type,i.payment_to,i.total_net,i.total_gross,i.currency,i.created_at,
          o.order_no,o.order_no order_display,m.sent_to_client,m.seen_by_client,m.seen_at,m.payment_reminder,m.paid,m.paid_at,
          case when coalesce(m.pdf_path,'')<>'' then 1 else 0 end pdf_exists,m.pdf_path pdf_ref
        from public.invoices i join public.orders o on o.id=i.order_id left join public.invoice_meta m on m.invoice_id=i.id
        where o.customer_id=cid and i.publication_state='complete' and (m.sent_to_client=1 or m.invoice_id is null) order by i.id desc limit 500) x;
      return jsonb_build_object('ok',true,'invoices',data);
    when 'b2b.invoice_file' then
      select m.pdf_path into prefix from public.invoice_meta m join public.invoices i on i.id=m.invoice_id join public.orders o on o.id=i.order_id
        where i.id=(p_data->>'invoice_id')::bigint and o.customer_id=cid and m.sent_to_client=1 and i.publication_state='complete';
      if not found then raise exception 'Brak dostępu do faktury' using errcode='42501'; end if;
      return jsonb_build_object('ok',true,'file_ref',prefix);
    when 'b2b.image' then
      select i.stored_path into prefix from public.product_images i join public.product_image_assignments a on a.image_id=i.id
        join public.products p on p.id=a.product_id and not coalesce(p.archived,false) where i.id=(p_data->>'image_id')::bigint limit 1;
      if not found then raise exception 'Zdjęcie niedostępne' using errcode='42501'; end if;
      return jsonb_build_object('ok',true,'file_ref',prefix);
    when 'b2b.invoice_seen' then
      iid:=(p_data->>'invoice_id')::bigint;
      update public.invoice_meta m set seen_by_client=1,seen_at=to_char(now() at time zone 'Europe/Warsaw','YYYY-MM-DD HH24:MI:SS')
        from public.invoices i join public.orders o on o.id=i.order_id
        where m.invoice_id=i.id and i.id=iid and o.customer_id=cid and i.publication_state='complete' and m.sent_to_client=1;
      if not found then raise exception 'Brak dostępu do faktury' using errcode='42501'; end if;
      return jsonb_build_object('ok',true);
    when 'b2b.search' then
      if length(btrim(coalesce(p_data->>'query','')))<2 then return jsonb_build_object('ok',true,'skipped',true); end if;
      sid:=(p_data->>'event_id')::uuid;
      if sid is null then raise exception 'Event ID required'; end if;
      prefix:=encode(sha256(convert_to(uid::text||sid::text,'UTF8')),'hex');
      insert into public.search_analytics_records(id,kind,created_at,payload)
      values(prefix,'event',now(),jsonb_build_object('id',prefix,'customer_id',uid::text,'customer_name',customer.name,
        'query',left(btrim(p_data->>'query'),120),'edit_id',(p_data->>'edit_id')::uuid,
        'sequence',greatest(0,coalesce((p_data->>'sequence')::integer,0)),
        'results_count',greatest(0,least(1000000,coalesce((p_data->>'results_count')::integer,0))),
        'selected_sku',(select sku from public.products where id=(p_data->>'selected_product_id')::bigint and not coalesce(archived,false)),
        'model_id',null,'model_name','','resolution','unresolved','created_at',now())) on conflict(id) do nothing;
      return jsonb_build_object('ok',true,'cloud',true);
    when 'b2b.aliases' then
      with families as (
        select distinct left(encode(sha256(convert_to(lower(regexp_replace(btrim(v),'\s+',' ','g')),'UTF8')),'hex'),24) fid,
          coalesce(nullif(p.name,''),p.model) label
        from public.products p cross join lateral (values (p.model),(p.name)) names(v)
        where not coalesce(p.archived,false) and coalesce(v,'')<>''
      ) select coalesce(jsonb_object_agg(r.payload->>'phrase',f.label),'{}') into data from public.search_analytics_records r
        join (select fid,min(label) label from families group by fid having count(distinct label)=1) f on f.fid=r.payload->>'model_id'
        where r.kind='rule' and r.payload->>'phrase' is not null;
      return jsonb_build_object('ok',true,'aliases',data);
    else raise exception 'Nieznana operacja' using errcode='42501';
  end case;
end $$;
commit;
