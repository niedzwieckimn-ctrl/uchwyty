-- After 030. Restores the order email layout from ZIP 51 on the existing worker.
-- Does not send historical orders, reset jobs or change invoice/shipping/reminder mail.
begin;
create or replace function architect_portal.order_email_escape(v text)
returns text language sql immutable set search_path='' as $$
 select replace(replace(replace(replace(replace(coalesce(v,''),'&','&amp;'),'<','&lt;'),'>','&gt;'),'"','&quot;'),'''','&#39;');
$$;
revoke all on function architect_portal.order_email_escape(text) from public,anon,authenticated;

create or replace function architect_portal.transaction_email_shell(p_title text,p_content text,p_closing text default 'Pozdrawiamy')
returns text language sql immutable set search_path='' as $$
 select '<div style="margin:0;padding:28px 14px;background:#f3f6fb;font-family:Arial,sans-serif;color:#10203d">'||
 '<div style="max-width:620px;margin:0 auto;background:#fff;border:1px solid #e2e8f2;border-radius:18px;overflow:hidden"><div style="padding:30px 34px 28px">'||
 '<h1 style="margin:0 0 24px;font-size:26px;line-height:1.25">'||architect_portal.order_email_escape(p_title)||'</h1>'||coalesce(p_content,'')||
 '<p style="margin:26px 0 0;line-height:1.6;color:#52617c">'||architect_portal.order_email_escape(p_closing)||',<br><b>Zespół Niedźwieccy</b></p></div></div></div>';
$$;
revoke all on function architect_portal.transaction_email_shell(text,text,text) from public,anon,authenticated;

create or replace function architect_portal.order_email_content(p_payload jsonb)
returns jsonb language plpgsql immutable set search_path='' as $$
declare translations jsonb:='{"pl": {"subject": "Przyjęliśmy zamówienie {order_no}", "title": "Dziękujemy za zamówienie", "intro": "Zamówienie <b>{order_no}</b> zostało przyjęte. Poniżej przesyłamy podsumowanie.", "customer": "Klient", "email": "Email", "note": "Notatka", "name": "Nazwa", "qty": "Ilość", "unit": "Cena/szt.", "value": "Wartość", "total": "Razem", "empty": "Brak pozycji", "reply": "Jeśli coś się nie zgadza, odpowiedz na tę wiadomość — sprawdzimy to od razu.", "regards": "Pozdrawiamy"}, "de": {"subject": "Wir haben Ihre Bestellung {order_no} erhalten", "title": "Vielen Dank für Ihre Bestellung", "intro": "Ihre Bestellung <b>{order_no}</b> ist bei uns eingegangen. Unten finden Sie die Zusammenfassung.", "customer": "Kunde", "email": "E-Mail", "note": "Anmerkung", "name": "Bezeichnung", "qty": "Menge", "unit": "Preis/Stk.", "value": "Wert", "total": "Gesamt", "empty": "Keine Positionen", "reply": "Falls etwas nicht stimmt, antworten Sie bitte auf diese Nachricht — wir prüfen es sofort.", "regards": "Viele Grüße"}, "en": {"subject": "We received your order {order_no}", "title": "Thank you for your order", "intro": "Your order <b>{order_no}</b> has been received. A summary is shown below.", "customer": "Customer", "email": "Email", "note": "Note", "name": "Name", "qty": "Quantity", "unit": "Unit price", "value": "Value", "total": "Total", "empty": "No items", "reply": "If anything is incorrect, reply to this message and we will check it right away.", "regards": "Kind regards"}, "es": {"subject": "Hemos recibido su pedido {order_no}", "title": "Gracias por su pedido", "intro": "Hemos recibido el pedido <b>{order_no}</b>. A continuación encontrará el resumen.", "customer": "Cliente", "email": "Email", "note": "Nota", "name": "Nombre", "qty": "Cantidad", "unit": "Precio/ud.", "value": "Importe", "total": "Total", "empty": "Sin artículos", "reply": "Si algo no es correcto, responda a este mensaje y lo revisaremos de inmediato.", "regards": "Saludos"}, "it": {"subject": "Abbiamo ricevuto l''ordine {order_no}", "title": "Grazie per il suo ordine", "intro": "L''ordine <b>{order_no}</b> è stato ricevuto. Di seguito trova il riepilogo.", "customer": "Cliente", "email": "Email", "note": "Nota", "name": "Nome", "qty": "Quantità", "unit": "Prezzo/pz.", "value": "Valore", "total": "Totale", "empty": "Nessun articolo", "reply": "Se qualcosa non è corretto, risponda a questo messaggio e lo verificheremo subito.", "regards": "Cordiali saluti"}}'::jsonb;
 c jsonb; o jsonb:=p_payload->'order'; item jsonb; lang text; currency text;
 order_no text; customer text; customer_email text; note text; subject text; title text;
 rows text:=''; details text; content text; html text; plain text; amount numeric; total numeric:=0; unit numeric; qty integer;
 company boolean:=coalesce(p_payload->>'notification_audience','')='company';
begin
 if jsonb_typeof(o) is distinct from 'object' or jsonb_typeof(p_payload->'items') is distinct from 'array'
 or jsonb_array_length(p_payload->'items')=0 then raise exception 'Invalid order email snapshot'; end if;
 lang:=case when company then 'pl' else coalesce(o->>'language','pl') end;
 c:=coalesce(translations->lang,translations->'pl');
 currency:=coalesce(nullif(o->>'currency',''),'PLN');
 order_no:=coalesce(nullif(o->>'order_no',''),o->>'id');
 if coalesce(order_no,'')='' then raise exception 'Missing order number'; end if;
 customer:=coalesce(nullif(o->>'customer_name',''),o->>'customer_email','Klient');
 customer_email:=coalesce(o->>'customer_email','');note:=coalesce(nullif(o->>'note',''),'-');
 title:=case when company then 'Nowe zamówienie od klienta' else c->>'title' end;
 subject:=case when company then 'Nowe zamówienie '||order_no||' — Niedźwieccy' else replace(c->>'subject','{order_no}',order_no) end;
 plain:=title||': '||order_no||E'\n'||(c->>'customer')||': '||customer||E'\n'||(c->>'email')||': '||customer_email||E'\n'||(c->>'note')||': '||note||E'\n\n';
 for item in select value from jsonb_array_elements(p_payload->'items') loop
   unit:=(item->>'net_price')::numeric;qty:=(item->>'qty')::integer;
   if unit is null or unit<0 or qty is null or qty<1 then raise exception 'Invalid order item'; end if;
   amount:=round(unit*qty,2);total:=total+amount;
   rows:=rows||'<tr><td style="padding:8px;border-bottom:1px solid #eee">'||architect_portal.order_email_escape(item->>'sku')||
    '</td><td style="padding:8px;border-bottom:1px solid #eee">'||architect_portal.order_email_escape(coalesce(nullif(item->>'name',''),item->>'sku'))||
    '</td><td style="padding:8px;border-bottom:1px solid #eee;text-align:right">'||qty::text||
    '</td><td style="padding:8px;border-bottom:1px solid #eee;text-align:right">'||to_char(unit,'FM999999999999990.00')||' '||architect_portal.order_email_escape(currency)||
    '</td><td style="padding:8px;border-bottom:1px solid #eee;text-align:right">'||to_char(amount,'FM999999999999990.00')||' '||architect_portal.order_email_escape(currency)||'</td></tr>';
   plain:=plain||coalesce(nullif(item->>'name',''),item->>'sku')||' ('||coalesce(item->>'sku','')||') - '||qty::text||' szt. - '||to_char(amount,'FM999999999999990.00')||' '||currency||E'\n';
 end loop;
 details:='<div style="margin:22px 0;padding:20px;background:#f7f9fd;border:1px solid #e3e9f3;border-radius:14px;line-height:1.8">'||
 '<div><span style="color:#62708c">'||architect_portal.order_email_escape(c->>'customer')||':</span> <b>'||architect_portal.order_email_escape(customer)||'</b></div>'||
 '<div><span style="color:#62708c">'||architect_portal.order_email_escape(c->>'email')||':</span> <b>'||architect_portal.order_email_escape(customer_email)||'</b></div>'||
 '<div><span style="color:#62708c">'||architect_portal.order_email_escape(c->>'note')||':</span> '||architect_portal.order_email_escape(note)||'</div></div>';
 content:='<p style="margin:0 0 20px;line-height:1.6">'||replace(c->>'intro','{order_no}',architect_portal.order_email_escape(order_no))||'</p>'||details||
 '<div style="overflow-x:auto"><table style="border-collapse:collapse;width:100%;max-width:760px"><thead><tr>'||
 '<th style="text-align:left;padding:8px;border-bottom:2px solid #111">SKU</th>'||
 '<th style="text-align:left;padding:8px;border-bottom:2px solid #111">'||architect_portal.order_email_escape(c->>'name')||'</th>'||
 '<th style="text-align:right;padding:8px;border-bottom:2px solid #111">'||architect_portal.order_email_escape(c->>'qty')||'</th>'||
 '<th style="text-align:right;padding:8px;border-bottom:2px solid #111">'||architect_portal.order_email_escape(c->>'unit')||'</th>'||
 '<th style="text-align:right;padding:8px;border-bottom:2px solid #111">'||architect_portal.order_email_escape(c->>'value')||'</th>'||
 '</tr></thead><tbody>'||rows||'</tbody><tfoot><tr><th colspan="4" style="padding:10px 8px;text-align:right">'||architect_portal.order_email_escape(c->>'total')||
 '</th><th style="padding:10px 8px;text-align:right">'||to_char(total,'FM999999999999990.00')||' '||architect_portal.order_email_escape(currency)||'</th></tr></tfoot></table></div>'||
 '<p style="margin:22px 0 0;line-height:1.6">'||architect_portal.order_email_escape(c->>'reply')||'</p>';
 html:=architect_portal.transaction_email_shell(title,content,c->>'regards');
 plain:=plain||E'\n'||(c->>'total')||': '||to_char(total,'FM999999999999990.00')||' '||currency||E'\n\n'||(c->>'reply');
 return jsonb_build_object('subject',subject,'html',html,'text',plain);
end $$;
revoke all on function architect_portal.order_email_content(jsonb) from public,anon,authenticated;

create or replace function architect_portal.queue_company_order_email(p_order_id bigint)
returns void language plpgsql security definer set search_path='' as $$
declare o public.orders%rowtype; items jsonb;
begin
 if not exists(select 1 from architect_portal.order_requests where order_id=p_order_id) then raise exception 'Not a portal order'; end if;
 select * into strict o from public.orders where id=p_order_id;
 select coalesce(jsonb_agg(jsonb_build_object('sku',i.sku,'name',coalesce(nullif(p.name,''),nullif(to_jsonb(p)->>'model',''),i.sku),
  'qty',i.qty,'net_price',i.unit_net_price,'currency',coalesce(i.currency,'PLN')) order by i.id),'[]'::jsonb)
 into items from public.order_items i left join public.products p on p.id=i.product_id where i.order_id=o.id;
 insert into public.architect_order_email_outbox(order_id,recipient,idempotency_key,payload,status,last_error_code)
 values(o.id,'biuro@niedzwieccy.com','portal-new-order:'||o.id::text||':biuro:v1',
  jsonb_build_object('notification_audience','company','order',jsonb_build_object('id',o.id,'order_no',o.order_no,
   'customer_name',o.customer_name,'customer_email',coalesce(o.customer_email,''),'note',o.note,'language','pl','currency',coalesce(o.currency,'PLN')),'items',items),
  case when jsonb_array_length(items)>0 then 'PENDING' else 'BLOCKED' end,
  case when jsonb_array_length(items)=0 then 'EMPTY_ORDER' else null end)
 on conflict(order_id,recipient) do nothing;
end $$;
revoke all on function architect_portal.queue_company_order_email(bigint) from public,anon,authenticated,service_role;

create or replace function architect_portal.company_order_email_trigger()
returns trigger language plpgsql security definer set search_path='' as $$
begin
 perform architect_portal.queue_company_order_email(new.order_id);
 return new;
end $$;
revoke all on function architect_portal.company_order_email_trigger() from public,anon,authenticated,service_role;
drop trigger if exists portal_queue_company_order_email on architect_portal.order_requests;
create constraint trigger portal_queue_company_order_email after insert on architect_portal.order_requests
 deferrable initially deferred for each row execute function architect_portal.company_order_email_trigger();

create or replace function architect_portal.claim_email(p_owner uuid,p_sender text,p_test_recipient text default '',p_job_id uuid default null)
returns jsonb language plpgsql security definer set search_path='' as $$
declare j public.architect_order_email_outbox%rowtype; body text; subject text; dest text;
begin
 if p_owner is null or length(p_sender)<5 then raise exception 'Invalid worker configuration'; end if;
 perform 1 from architect_portal.email_worker_control where singleton and active_worker='edge' for share;
 if not found then return null; end if;
 -- Retry ambiguous outcomes only within the provider's 24-hour idempotency window.
 update architect_portal.email_attempts a set finished_at=now(),outcome='LEASE_EXPIRED',error_code='LEASE_EXPIRED'
 from public.architect_order_email_outbox q where q.id=a.job_id and a.owner=q.lease_owner
 and a.finished_at is null and q.status='DELIVERING' and q.lease_until<now();
 update public.architect_order_email_outbox set
 status=case when provider_request is not null and first_attempt_at>now()-interval '23 hours' then 'RETRY' else 'UNKNOWN' end,
 last_error_code='LEASE_EXPIRED',lease_owner=null,lease_until=null,updated_at=now()
 where status='DELIVERING' and lease_until<now();
 update public.architect_order_email_outbox set status='UNKNOWN',last_error_code='IDEMPOTENCY_WINDOW_EXPIRED',updated_at=now()
 where status='RETRY' and first_attempt_at<=now()-interval '23 hours';
 select * into j from public.architect_order_email_outbox
 where status in ('PENDING','RETRY') and (p_job_id is null or id=p_job_id) and (next_attempt_at<=now() or p_job_id is not null)
 order by next_attempt_at,id limit 1 for update skip locked;
 if not found then return null; end if;
 if j.provider_request is null then
   dest:=coalesce(nullif(btrim(p_test_recipient),''),j.recipient);
   if dest !~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$' then raise exception 'Invalid recipient'; end if;
   if j.notification_type in ('architect_approved','architect_welcome_resend') then
     j.provider_request:=jsonb_build_object('from',p_sender,'to',jsonb_build_array(dest),
       'subject',case when p_test_recipient<>'' then '[STAGING] ' else '' end||'Witamy w programie Niedźwieccy dla Architektów!',
       '_template','architect-approved-v1');
   elsif j.notification_type='architect_sample_delay' then
     j.provider_request:=architect_portal.sample_delay_content(j.payload)||jsonb_build_object(
       'from',p_sender,'to',jsonb_build_array(dest),
       'subject',case when p_test_recipient<>'' then '[STAGING] ' else '' end||'Niedźwieccy — informacja o realizacji pakietu próbek');
   elsif j.notification_type='architect_registered' then
     j.provider_request:=jsonb_build_object('from',p_sender,'to',jsonb_build_array(dest),
       'subject',case when p_test_recipient<>'' then '[STAGING] ' else '' end||'Nowy architekt do zatwierdzenia — Niedźwieccy',
       'text','Architekt potwierdził adres e-mail i oczekuje na zatwierdzenie pracowni.'||E'\n\n'||
          'Pracownia: '||coalesce(j.payload->>'studio_name','')||E'\n'||
          'Architekt: '||coalesce(j.payload->>'name','')||E'\n'||
          'E-mail: '||coalesce(j.payload->>'email','')||E'\n\n'||
          'Otwórz zgłoszenie: https://uchwyty.onrender.com/architects/'||j.architect_id::text);
   j.provider_request:=j.provider_request||jsonb_build_object('html',architect_portal.transaction_email_shell(
     'Nowa pracownia do zatwierdzenia',
     '<p>Nowe zgłoszenie do programu Niedźwieccy dla Architektów czeka na Twoją decyzję.</p>'||
     '<div style="margin:22px 0;padding:20px;background:#f7f9fd;border:1px solid #e3e9f3;border-radius:14px;line-height:1.8">'||
     '<p><b>Pracownia:</b> '||architect_portal.order_email_escape(j.payload->>'studio_name')||'</p>'||
     '<p><b>Architekt:</b> '||architect_portal.order_email_escape(j.payload->>'name')||'</p>'||
     '<p><b>E-mail:</b> '||architect_portal.order_email_escape(j.payload->>'email')||'</p></div>'||
     '<p><a style="display:inline-block;padding:13px 22px;background:#4f70eb;color:#fff;text-decoration:none;font-weight:bold;border-radius:10px" href="https://uchwyty.onrender.com/architects/'||j.architect_id::text||'">Zobacz zgłoszenie</a></p>'));
   else
     j.provider_request:=architect_portal.order_email_content(j.payload)||jsonb_build_object('from',p_sender,'to',jsonb_build_array(dest));
     if p_test_recipient<>'' then
       j.provider_request:=j.provider_request||jsonb_build_object('subject','[STAGING] '||(j.provider_request->>'subject'));
     end if;
   end if;
 end if;
 -- Reject invalid cached requests locally; never send a null subject again.
 if jsonb_typeof(j.provider_request->'subject') is distinct from 'string'
    or coalesce(length(btrim(j.provider_request->>'subject')),0)=0 then
   update public.architect_order_email_outbox set status='BLOCKED',last_error_code='INVALID_MESSAGE_SUBJECT',updated_at=now()
   where id=j.id;
   return null;
 end if;
 update public.architect_order_email_outbox set status='DELIVERING',lease_owner=p_owner,
 lease_until=now()+interval '2 minutes',attempt_count=attempt_count+1,
 first_attempt_at=coalesce(first_attempt_at,now()),provider_request=j.provider_request,updated_at=now()
 where id=j.id returning * into j;
 insert into architect_portal.email_attempts(job_id,attempt,owner) values(j.id,j.attempt_count,p_owner);
 return jsonb_build_object('id',j.id,'idempotency_key',j.idempotency_key,'request',j.provider_request);
end $$;
revoke all on function architect_portal.claim_email(uuid,text,text,uuid) from public,anon,authenticated;


-- Both worker entrypoints MUST delegate to the new canonical implementation.
-- Older installations may still have the pre-025 order-only public function.


notify pgrst,'reload schema';
commit;
