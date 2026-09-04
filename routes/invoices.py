"""Mechanically extracted Flask routes; business logic is unchanged."""

from pathlib import Path

INVOICES_LIST_TEMPLATE = (Path(__file__).resolve().parent.parent / "templates" / "invoices_list.html").read_text(encoding="utf-8")

def register_routes(context):
    globals().update(context)


    @app.route("/orders/<int:order_id>/invoice", methods=["GET", "POST"])
    def order_invoice(order_id):
        maybe_pull_shared_from_supabase()
        sent_invoice_id = to_int(request.args.get("invoice_id"), 0) if norm(request.args.get("sent")) == "1" else 0
        if sent_invoice_id:
            meta = load_invoice_meta(sent_invoice_id) or {}
            upsert_invoice_meta(
                sent_invoice_id,
                meta.get("pdf_path", ""),
                meta.get("invoice_items_json", ""),
                sent_to_client=1,
                seen_by_client=int(meta.get("seen_by_client") or 0),
                seen_at=meta.get("seen_at"),
                payment_reminder=int(meta.get("payment_reminder") or 0),
                paid=int(meta.get("paid") or 0),
                paid_at=meta.get("paid_at")
            )
        c = conn()
        cur = c.cursor()
        cur.execute("SELECT * FROM orders WHERE id=?", (order_id,))
        o = cur.fetchone()
        if not o:
            c.close()
            abort(404)
        packing_selection = session.get("latest_packing_selection") or load_open_packing_selection(order_id)
        packing_order_ids = {
            to_int(value, 0) for value in packing_selection.get("order_ids", [])
            if to_int(value, 0) > 0
        } if isinstance(packing_selection, dict) else set()
        # Fakturę można wystawić również po wysyłce lub po ręcznej korekcie
        # statusu. O dostępności pozycji decydują ilości/alokacje, nie status.
        related_orders = [dict(o)]
        customer_email_key = _email_key(o["customer_email"])
        if customer_email_key:
            status_ph = ",".join(["?"] * len(CURRENT_ORDER_STATUSES))
            cur.execute(f"""
              SELECT *
              FROM orders
              WHERE LOWER(COALESCE(customer_email,'')) = ?
                AND LOWER(COALESCE(status,'')) IN ({status_ph})
              ORDER BY created_at DESC, id DESC
            """, (customer_email_key, *sorted(CURRENT_ORDER_STATUSES)))
            related_orders = [dict(r) for r in cur.fetchall()]
            if int(order_id) not in {int(r["id"]) for r in related_orders}:
                related_orders.insert(0, dict(o))

        missing_packing_ids = packing_order_ids - {int(r["id"]) for r in related_orders}
        if missing_packing_ids:
            packing_ph = ",".join(["?"] * len(missing_packing_ids))
            cur.execute(f"SELECT * FROM orders WHERE id IN ({packing_ph})", tuple(sorted(missing_packing_ids)))
            related_orders.extend(dict(r) for r in cur.fetchall())

        related_order_ids = [int(r["id"]) for r in related_orders] or [-1]
        related_order_by_id = {int(r["id"]): r for r in related_orders}
        order_ph = ",".join(["?"] * len(related_order_ids))

        cur.execute(f"""
          SELECT oi.*, p.model, p.name, COALESCE(s.qty, 0) AS stock_qty,
                 oo.order_no AS source_order_no,
                 oo.created_at AS source_order_created_at,
                 oo.note AS source_order_note,
                 COALESCE(oi.unit_net_price, pr.net_price, 0) AS net_price,
                 COALESCE(oi.unit_gross_price, oi.unit_net_price, pr.gross_price, pr.net_price, 0) AS gross_price,
                 COALESCE(oi.currency, oo.currency, 'PLN') AS currency,
                 (oi.qty * COALESCE(oi.unit_net_price, pr.net_price, 0)) AS line_value_net,
                 (oi.qty * COALESCE(oi.unit_gross_price, oi.unit_net_price, pr.gross_price, pr.net_price, 0)) AS line_value_gross
          FROM order_items oi
          JOIN orders oo ON oo.id=oi.order_id
          JOIN products p ON p.id=oi.product_id
          LEFT JOIN stock s ON s.product_id=oi.product_id
          LEFT JOIN pricing pr ON (TRIM(LOWER(pr.model)) = TRIM(LOWER(p.model)) OR TRIM(LOWER(pr.model)) = TRIM(LOWER(p.sku)))
          WHERE oi.order_id IN ({order_ph})
          ORDER BY oo.created_at DESC, oo.id DESC, oi.id
        """, related_order_ids)
        items = [dict(r) for r in cur.fetchall()]
        invoiced_by_item = invoiced_qty_by_order_item_ids([int(it["id"]) for it in items])
        for it in items:
            source_order = related_order_by_id.get(int(it.get("order_id") or 0), {})
            ordered_qty = int(it.get("qty") or 0)
            done_qty = int(invoiced_by_item.get(int(it["id"])) or 0)
            it["source_order_no"] = order_display_no(
                source_order.get("id") or it.get("order_id"),
                source_order.get("created_at") or it.get("source_order_created_at"),
                source_order.get("order_no") or it.get("source_order_no"),
                source_order.get("note") or it.get("source_order_note") or ""
            )
            it["source_order_note"] = source_order.get("note") or it.get("source_order_note") or ""
            it["ordered_qty"] = ordered_qty
            it["invoiced_qty"] = done_qty
            it["remaining_qty"] = max(0, ordered_qty - done_qty)

        packing_qty_by_item = {}
        if int(order_id) in packing_order_ids:
            for pair in packing_selection.get("items", []):
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    item_id = to_int(pair[0], 0)
                    packed_qty = max(0, to_int(pair[1], 0))
                    if item_id > 0 and packed_qty > 0:
                        packing_qty_by_item[item_id] = packing_qty_by_item.get(item_id, 0) + packed_qty
        invoice_from_packing = bool(packing_qty_by_item)

        invoice_stock_pool = {}
        for it in items:
            pid = int(it.get("product_id") or 0)
            invoice_stock_pool.setdefault(pid, max(0, int(it.get("stock_qty") or 0)))
            if invoice_from_packing:
                suggested_qty = min(
                    int(it.get("remaining_qty") or 0),
                    packing_qty_by_item.get(int(it.get("id") or 0), 0),
                )
            else:
                suggested_qty = min(int(it.get("remaining_qty") or 0), invoice_stock_pool.get(pid, 0))
            it["suggested_invoice_qty"] = max(0, suggested_qty)
            invoice_stock_pool[pid] = max(0, invoice_stock_pool.get(pid, 0) - suggested_qty)

        cur.execute("SELECT * FROM company_profile WHERE id=1")
        company = cur.fetchone()

        customer_row = None
        if o["customer_id"]:
            cur.execute("SELECT * FROM customers WHERE id=?", (o["customer_id"],))
            customer_row = cur.fetchone()
        if not customer_row:
            cur.execute("SELECT * FROM customers WHERE name=? ORDER BY id DESC LIMIT 1", (o["customer_name"],))
            customer_row = cur.fetchone()

        cur.execute(f"""
          SELECT
            i.*,
            m.invoice_id AS meta_invoice_id,
            COALESCE(m.pdf_path,'') AS pdf_path,
            COALESCE(m.sent_to_client,0) AS sent_to_client,
            COALESCE(m.seen_by_client,0) AS seen_by_client,
            COALESCE(m.payment_reminder,0) AS payment_reminder,
            COALESCE(m.paid,0) AS paid,
            COALESCE(m.paid_at,'') AS paid_at,
            COALESCE(m.seen_at,'') AS seen_at,
            COALESCE(m.invoice_items_json,'') AS invoice_items_json
          FROM invoices i
          LEFT JOIN invoice_meta m ON m.invoice_id = i.id
          WHERE i.order_id IN ({order_ph})
          ORDER BY i.id DESC
        """, related_order_ids)
        invoice_rows = [dict(r) for r in cur.fetchall()]
        c.close()

        default_issue = app_now().strftime("%Y-%m-%d")
        # Profil w Supabase jest najświeższym źródłem danych klienta. Lokalny
        # rekord albo starsze zamówienie mogą nie zawierać adresu, mimo że klient
        # uzupełnił go później w swoim profilu.
        try:
            client_profile = _client_profile_for_email(o["customer_email"])
        except Exception as exc:
            app.logger.warning("Nie udało się pobrać profilu do faktury order_id=%s: %s", order_id, exc)
            client_profile = {}
        buyer_address_source = (
            norm(client_profile.get("address"))
            or (norm(customer_row["address"]) if customer_row and customer_row["address"] else "")
            or norm(o["customer_address"])
        )
        st, pc, city = split_address(buyer_address_source)
        buyer_tax_no = (
            norm(client_profile.get("nip"))
            or (norm(customer_row["nip"]) if customer_row and customer_row["nip"] else "")
        )
        buyer_address_default = "\n".join([x for x in [st, f"{pc} {city}".strip()] if x]).strip()

        msg = ""
        if request.args.get("generated") == "1":
            msg = "Faktura zostaĹ‚a zapisana."
        if request.args.get("sent") == "1":
            msg = "Faktura zostaĹ‚a udostÄ™pniona klientowi."
        if request.args.get("deleted") == "1":
            msg = "Faktura zostaĹ‚a usuniÄ™ta."
        if request.args.get("deleted") == "1":
            msg = "Faktura zostaĹ‚a usuniÄ™ta."

        if request.method == "GET":
            order_currency = normalize_order_currency(o["currency"])
            auto_type, order_currency, auto_country = automatic_invoice_tax_context(dict(o), buyer_tax_no, "")
            data = {
                "invoice_no": next_invoice_no(default_issue),
                "place": "Kotuszów",
                "issue_date": default_issue,
                "sell_date": default_issue,
                "payment_type": "przelew",
                "payment_to": (app_now() + timedelta(days=7)).strftime("%Y-%m-%d"),
                "buyer_name": norm(client_profile.get("name")) or o["customer_name"] or "",
                "buyer_tax_no": buyer_tax_no,
                "buyer_address": buyer_address_default,
                "buyer_country": auto_country or ("" if order_currency == "EUR" else "PL"),
                "buyer_email": o["customer_email"] or "",
                "buyer_phone": norm(client_profile.get("phone")) or o["customer_phone"] or "",
                "discount_percent": "0",
                "invoice_type": auto_type,
                "currency": order_currency,
            }
        else:
            data = {k: norm(request.form.get(k)) for k in [
                "invoice_no", "place", "issue_date", "sell_date", "payment_type", "payment_to",
                "buyer_name", "buyer_tax_no", "buyer_address", "buyer_country",
                "buyer_email", "buyer_phone", "discount_percent", "invoice_type", "currency"
            ]}
            order_currency = normalize_order_currency(o["currency"])
            auto_type, auto_currency, auto_country = automatic_invoice_tax_context(dict(o), data.get("buyer_tax_no"), data.get("buyer_country"))
            # Zwykły flow jest całkowicie automatyczny: zamówienie z cennika UE
            # zawsze daje WDT/EUR, a kraj pochodzi z prefiksu VAT UE.
            data["currency"] = auto_currency
            data["invoice_type"] = auto_type
            data["buyer_country"] = auto_country
            if not data.get("buyer_address"):
                data["buyer_address"] = buyer_address_default
            st, pc, city = split_address(data.get("buyer_address", ""))
            data["buyer_street"] = st
            data["buyer_post_code"] = pc
            data["buyer_city"] = city
            if not data["invoice_no"]:
                data["invoice_no"] = next_invoice_no(data["issue_date"] or default_issue)
            if not data["issue_date"]:
                data["issue_date"] = default_issue
            if not data["sell_date"]:
                data["sell_date"] = data["issue_date"]
            if not data["payment_to"]:
                try:
                    issue_day = datetime.strptime(data["issue_date"], "%Y-%m-%d")
                except (TypeError, ValueError):
                    issue_day = app_now()
                data["payment_to"] = (issue_day + timedelta(days=7)).strftime("%Y-%m-%d")

            invoice_items = prepare_invoice_items(items, request.form)
            if norm(request.form.get("submit_action")) != "packing" and invoice_from_packing:
                invalid_packing_qty = next((
                    item for item in invoice_items
                    if int(item.get("qty") or 0) > packing_qty_by_item.get(
                        int(item.get("order_item_id") or item.get("id") or 0), 0
                    )
                ), None)
                if invalid_packing_qty:
                    msg = "Ilość na fakturze nie może być większa niż ilość zapisana na ostatniej liście pakowej."
                    invoice_items = []
            if norm(request.form.get("submit_action")) != "packing" and data["invoice_type"] == "wdt":
                vat_eu = re.sub(r"[\s.-]+", "", data.get("buyer_tax_no") or "").upper()
                buyer_country = norm(data.get("buyer_country")).upper()
                eu_vat_prefixes = {
                    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "EL",
                    "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PT", "RO", "SK",
                    "SI", "ES", "SE",
                }
                if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{6,14}", vat_eu):
                    msg = "Dla WDT 0% podaj prawidłowy numer VAT UE nabywcy z dwuliterowym prefiksem kraju, np. DE123456789."
                    invoice_items = []
                elif vat_eu[:2] not in eu_vat_prefixes:
                    msg = "Stawka WDT 0% wymaga numeru VAT UE z kraju Unii Europejskiej innego niż Polska."
                    invoice_items = []
                elif buyer_country in {"", "PL", "POLSKA", "POLAND"}:
                    msg = "Dla WDT 0% podaj kraj nabywcy inny niż Polska."
                    invoice_items = []
                else:
                    data["buyer_tax_no"] = vat_eu
                    data["vat_rate"] = 0
                    for invoice_item in invoice_items:
                        invoice_item["vat_rate"] = 0
                        invoice_item["currency"] = data["currency"]
                        invoice_item["gross_price"] = invoice_item.get("net_price")
                        invoice_item["line_value_vat"] = 0.0
                        invoice_item["line_value_gross"] = invoice_item.get("line_value_net")
            elif norm(request.form.get("submit_action")) != "packing" and data["invoice_type"] == "export":
                buyer_country = norm(data.get("buyer_country")).upper()
                eu_codes = {"AT","BE","BG","HR","CY","CZ","DK","EE","FI","FR","DE","EL","GR","HU","IE","IT","LV","LT","LU","MT","NL","PT","RO","SK","SI","ES","SE","PL"}
                if not buyer_country or buyer_country in eu_codes:
                    msg = "Eksport wymaga kraju nabywcy spoza Unii Europejskiej."
                    invoice_items = []
                elif not norm(data.get("buyer_tax_no")):
                    msg = "Dla eksportu podaj zagraniczny Tax ID nabywcy."
                    invoice_items = []
                else:
                    data["vat_rate"] = 0
                    for invoice_item in invoice_items:
                        invoice_item["vat_rate"] = 0
                        invoice_item["currency"] = data["currency"]
                        invoice_item["gross_price"] = invoice_item.get("net_price")
                        invoice_item["line_value_vat"] = 0.0
                        invoice_item["line_value_gross"] = invoice_item.get("line_value_net")
            elif norm(request.form.get("submit_action")) != "packing" and data["invoice_type"] == "domestic":
                data["vat_rate"] = 23
                for invoice_item in invoice_items:
                    invoice_item["vat_rate"] = 23
                    invoice_item["currency"] = data["currency"]
            if norm(request.form.get("submit_action")) == "packing":
                if not invoice_items:
                    msg = "Lista pakowania musi zawierac co najmniej jedna pozycje."
                else:
                    packed_order_ids = [
                        int(item.get("source_order_id") or item.get("order_id") or order_id)
                        for item in invoice_items
                    ]
                    packing_order_no = canonical_order_no(o["id"], o["created_at"], o["order_no"])
                    packing_meta = {
                        "invoice_no": packing_order_no,
                        "document_label_key": "order",
                        "buyer_name": data.get("buyer_name") or o["customer_name"] or "Klient",
                        "buyer_email": data.get("buyer_email") or o["customer_email"] or "",
                    }
                    packing_path = generate_invoice_packing_list_pdf(o, invoice_items, packing_meta)
                    mark_orders_packed(packed_order_ids, packing_path=packing_path, packing_items=invoice_items)
                    return send_file(
                        packing_path,
                        mimetype="application/pdf",
                        as_attachment=True,
                        download_name=f"{safe_filename(packing_order_no)}_lista_pakowania.pdf",
                    )
            existing_invoice_id = invoice_no_exists(data["invoice_no"])
            if existing_invoice_id:
                msg = f"Faktura o takim numerze już istnieje! Numer: {data['invoice_no']}. Wybierz inny numer faktury."
            elif not invoice_items and not msg:
                msg = "Faktura musi zawieraÄ‡ co najmniej jednÄ… pozycjÄ™."
            elif invoice_items:
                pdf_path, total_net, total_gross = generate_order_invoice_pdf(o, invoice_items, data)
                packing_pdf_path = generate_invoice_packing_list_pdf(o, invoice_items, data, pdf_path)
                c = conn()
                cur = c.cursor()
                cur.execute("""
                  INSERT INTO invoices(order_id, invoice_no, issue_date, sell_date, payment_type, payment_to,
                                       buyer_name, buyer_tax_no, buyer_street, buyer_post_code, buyer_city, buyer_country,
                                       buyer_email, buyer_phone, total_net, total_gross, created_at, invoice_type, currency)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    order_id, data["invoice_no"], data["issue_date"], data["sell_date"], data["payment_type"], data["payment_to"],
                    data["buyer_name"], data["buyer_tax_no"], data["buyer_street"], data["buyer_post_code"], data["buyer_city"], data["buyer_country"],
                    data["buyer_email"], data["buyer_phone"], total_net, total_gross, now_iso(), data["invoice_type"], data["currency"]
                ))
                invoice_id = cur.lastrowid
                if not invoice_id:
                    cur.execute("SELECT id FROM invoices WHERE invoice_no=? LIMIT 1", (data["invoice_no"],))
                    rr = cur.fetchone()
                    invoice_id = int(rr["id"]) if rr else 0
                c.commit()
                c.close()
                stored_pdf_path = upload_invoice_pdfs_to_supabase(invoice_id, data["invoice_no"], pdf_path, packing_pdf_path)
                upsert_invoice_meta(invoice_id, stored_pdf_path, json.dumps(invoice_items, ensure_ascii=False), sent_to_client=None)
                allocation_ids = replace_invoice_allocations(invoice_id, invoice_items)
                touched_order_ids = [int(x.get("source_order_id") or x.get("order_id") or 0) for x in invoice_items]
                completed_order_ids, changed_product_ids = finalize_fully_invoiced_orders(touched_order_ids)
                if supabase_enabled():
                    try:
                        sync_local_rows_to_supabase("invoices", "id", [invoice_id])
                    except Exception:
                        pass
                    try:
                        sync_invoice_meta_to_supabase(invoice_id)
                    except Exception:
                        pass
                    try:
                        sync_local_rows_to_supabase("invoice_allocations", "id", allocation_ids)
                    except Exception:
                        pass
                    if completed_order_ids:
                        try:
                            sync_local_rows_to_supabase("orders", "id", completed_order_ids)
                        except Exception:
                            pass
                    if changed_product_ids:
                        try:
                            sync_local_rows_to_supabase("stock", "product_id", changed_product_ids)
                        except Exception:
                            pass
                # Bezpiecznie: samo wystawienie faktury NIE wysyła już automatycznie e-maila.
                # Wysyłka następuje dopiero po kliknięciu przycisku „Wyślij”.
                email_ok = False
                email_error = ""
                if invoice_from_packing:
                    consume_packing_selection(to_int(packing_selection.get("batch_id"), 0), invoice_id)
                    session.pop("latest_packing_selection", None)
                redirect_args = {
                    "generated": "1",
                    "invoice_id": invoice_id,
                    "email_sent": "0",
                }
                if email_error:
                    redirect_args["email_error"] = email_error[:300]
                return redirect(url_for("invoices", **redirect_args))

        tpl = r"""
        {% extends "base.html" %}
        {% block content %}
          <div class="card">
            <div class="flex">
              <h1 style="margin:0;">Faktura z pozycji klienta: {{ o['customer_name'] or o['customer_email'] }}</h1>
              <a class="btn right" href="{{ url_for('order_view', order_id=o['id']) }}">â† SzczegĂłĹ‚y</a>
            </div>
            {% if msg %}
              <div class="hint" style="margin-top:10px;">{{ msg }}</div>
            {% endif %}
          </div>

          <div class="card">
            <form method="post" class="row">
              <input type="hidden" name="invoice_type" value="{{ d['invoice_type'] }}">
              <input type="hidden" name="currency" value="{{ d['currency'] }}">
              <div><label class="muted small">Rozliczenie</label><div class="hint"><b>{{ 'WDT 0%' if d['invoice_type']=='wdt' else ('Eksport 0%' if d['invoice_type']=='export' else 'Krajowa 23%') }}</b> · {{ d['currency'] }} — ustawione automatycznie z zamówienia</div></div>
              {% if d['invoice_type'] == 'wdt' %}
                <div class="hint" style="grid-column:1/-1;">
                  <b>Faktura WDT 0% w EUR.</b> Przed wystawieniem sprawdź aktywny numer VAT UE nabywcy w VIES.
                  Stawkę 0% stosuj tylko dla dostawy do innego kraju UE i zachowaj dokumenty potwierdzające wywóz oraz dostarczenie towaru.
                </div>
              {% endif %}
              <div><label class="muted small">Numer faktury</label><input name="invoice_no" value="{{ d['invoice_no'] }}" required></div>
              <div><label class="muted small">Miejsce</label><input name="place" value="{{ d['place'] }}"></div>
              <div><label class="muted small">Data wystawienia</label><input id="invoice_issue_date" name="issue_date" type="date" value="{{ d['issue_date'] }}"></div>
              <div><label class="muted small">Data sprzedaĹĽy</label><input name="sell_date" type="date" value="{{ d['sell_date'] }}"></div>
              <div><label class="muted small">Forma pĹ‚atnoĹ›ci</label>
                <select name="payment_type">
                  <option value="przelew" {% if d['payment_type'] in ['transfer','przelew'] %}selected{% endif %}>przelew</option>
                  <option value="gotowka" {% if d['payment_type'] in ['cash','gotowka'] %}selected{% endif %}>gotĂłwka</option>
                  <option value="karta" {% if d['payment_type'] in ['card','karta'] %}selected{% endif %}>karta</option>
                </select>
              </div>
              <div><label class="muted small">Termin pĹ‚atnoĹ›ci</label><input id="invoice_payment_to" name="payment_to" type="date" value="{{ d['payment_to'] }}"></div>
              <div><label class="muted small">Rabat %</label><input name="discount_percent" value="{{ d['discount_percent'] or "0" }}"></div>

              <div><label class="muted small">Nabywca</label><input name="buyer_name" value="{{ d['buyer_name'] }}" required></div>
              <div><label class="muted small">{{ 'VAT UE nabywcy' if d['invoice_type'] == 'wdt' else ('Tax ID nabywcy' if d['invoice_type'] == 'export' else 'NIP nabywcy') }}</label><input name="buyer_tax_no" value="{{ d['buyer_tax_no'] }}" placeholder="{{ 'np. DE123456789' if d['invoice_type'] == 'wdt' else '' }}"></div>
              <div><label class="muted small">Adres nabywcy</label><textarea name="buyer_address" placeholder="Ulica&#10;Kod pocztowy Miasto">{{ d['buyer_address'] }}</textarea></div>
              <div><label class="muted small">Kraj</label><input name="buyer_country" value="{{ d['buyer_country'] }}"></div>
              <div><label class="muted small">Email</label><input name="buyer_email" value="{{ d['buyer_email'] }}"></div>
              <div><label class="muted small">Telefon</label><input name="buyer_phone" value="{{ d['buyer_phone'] }}"></div>

              <div style="grid-column:1/-1;">
                <h2>Pozycje faktury — wybierz ilości z zamówień klienta</h2>
                <div class="hint" style="margin-bottom:10px;">
                  {% if invoice_from_packing %}
                    Ilości pobrano z ostatniej listy pakowej. Nie można zafakturować więcej niż spakowano.
                  {% else %}
                    To lista utworzona przed zapisywaniem zawartości paczki. Sprawdź ilości ręcznie; kolejne faktury pobiorą je z listy pakowej.
                  {% endif %}
                </div>
                <table>
                  <thead><tr><th>Zamówienie</th><th>Notatka klienta</th><th>SKU</th><th>Model / Nazwa</th><th>Zamówiono</th><th>Zafakturowano</th><th>Pozostało</th><th>Na magazynie</th><th>Ilość na fakturze</th><th>Netto/szt {{ d['currency'] }}</th><th>Brutto/szt {{ d['currency'] }}</th></tr></thead>
                  <tbody>
                    {% for it in items %}
                    <tr>
                      <td><b>{{ it['source_order_no'] }}</b></td>
                      <td>{{ it['source_order_note'] or '-' }}</td>
                      <td><b>{{ it['sku'] }}</b></td>
                      <td>{{ it['model'] or '' }}{% if it['name'] %}<div class="muted small">{{ it['name'] }}</div>{% endif %}</td>
                      <td>{{ it['ordered_qty'] }}</td>
                      <td>{{ it['invoiced_qty'] }}</td>
                      <td><b>{{ it['remaining_qty'] }}</b></td>
                      <td><b>{{ it['stock_qty'] }}</b> szt.</td>
                      <td>
                        <input type="number" min="0" name="invoice_qty_{{ it['id'] }}" value="{{ it['suggested_invoice_qty'] }}" max="{{ it['remaining_qty'] }}" style="width:110px;" {% if it['remaining_qty'] <= 0 %}disabled{% endif %}>
                      </td>
                      <td>{{ "%.2f"|format(it['net_price']) }}</td>
                      <td>{{ "%.2f"|format(it['gross_price']) }}</td>
                    </tr>
                    {% endfor %}
                  </tbody>
                </table>
              </div>

              <div class="flex" style="align-items:flex-end;">
                <a class="btn" href="{{ url_for('order_packing_list_download_admin', order_id=o['id']) }}" target="_blank">Pakuj</a>
                <button class="btn primary" type="submit" name="submit_action" value="invoice">Zapisz fakturę PDF</button>
              </div>
            </form>
          </div>

          <script>
          (() => {
            const issueDate = document.getElementById('invoice_issue_date');
            const paymentTo = document.getElementById('invoice_payment_to');
            if (!issueDate || !paymentTo) return;
            issueDate.addEventListener('change', () => {
              if (!issueDate.value) return;
              const parts = issueDate.value.split('-').map(Number);
              if (parts.length !== 3 || parts.some(Number.isNaN)) return;
              const due = new Date(Date.UTC(parts[0], parts[1] - 1, parts[2]));
              due.setUTCDate(due.getUTCDate() + 7);
              paymentTo.value = due.toISOString().slice(0, 10);
            });
          })();
          </script>

          <div class="card">
            <h2>Zapisane faktury</h2>
            <table>
              <thead><tr><th>Numer</th><th>Data</th><th>Netto</th><th>Brutto</th><th>Status klienta</th><th>Płatność</th><th>Akcje</th></tr></thead>
              <tbody>
                {% for inv in invoice_rows %}
                  <tr>
                    <td><b>{{ inv['invoice_no'] }}</b></td>
                    <td>{{ inv['issue_date'] }}</td>
                    <td>{{ "%.2f"|format(inv['total_net']) }}</td>
                    <td>{{ "%.2f"|format(inv['total_gross']) }}</td>
                    <td>{{ "Udostępniona" if inv['sent_to_client'] else "Tylko wewnętrzna" }}</td>
                    <td>
                      {% if inv['paid'] %}
                        <span class="badge ok">Opłacona</span>
                        {% if inv['paid_at'] %}<div class="muted small">{{ inv['paid_at'] }}</div>{% endif %}
                      {% else %}
                        <span class="badge danger">Nieopłacona</span>
                        {% if inv['payment_reminder'] %}<span class="badge ok">Przypomnienie wysłane</span>{% endif %}
                      {% endif %}
                    </td>
                    <td>
                      <div class="flex">
                        <a class="btn" href="{{ url_for('invoice_download_admin', invoice_id=inv['id']) }}" target="_blank">Pobierz PDF</a>
                        <a class="btn" href="{{ url_for('order_packing_list_download_admin', order_id=inv['order_id'] or o['id']) }}" target="_blank">Pakuj</a>
                        <form method="post" action="{{ url_for('invoice_regenerate_admin', invoice_id=inv['id']) }}">
                          <button class="btn" type="submit">Regeneruj PDF</button>
                        </form>
                        {% if not inv['sent_to_client'] %}
                          <form method="post" action="{{ url_for('order_invoice_send', order_id=o['id'], invoice_id=inv['id']) }}">
                            <button class="btn primary" type="submit">WyĹ›lij fakturÄ™ klientowi</button>
                          </form>
                        {% else %}
                          <span class="badge">Widoczna w panelu klienta</span>
                        {% endif %}
                        {% if not inv['paid'] %}
                          <form method="post" action="{{ url_for('invoice_payment_reminder_admin', invoice_id=inv['id']) }}">
                            <input type="hidden" name="next" value="{{ request.full_path }}">
                            <button class="btn" type="submit">Przypomnij o płatności</button>
                          </form>
                          <form method="post" action="{{ url_for('invoice_paid_admin', invoice_id=inv['id']) }}">
                            <input type="hidden" name="next" value="{{ request.full_path }}">
                            <button class="btn ok" type="submit">Faktura opłacona</button>
                          </form>
                        {% else %}
                          <form method="post" action="{{ url_for('invoice_unpaid_admin', invoice_id=inv['id']) }}">
                            <input type="hidden" name="next" value="{{ request.full_path }}">
                            <button class="btn" type="submit">Cofnij opłacenie</button>
                          </form>
                        {% endif %}
                        <form method="post" action="{{ url_for('order_invoice_delete', order_id=o['id'], invoice_id=inv['id']) }}" onsubmit="return confirm('UsunÄ…Ä‡ fakturÄ™?')">
                          <button class="btn danger" type="submit">UsuĹ„ fakturÄ™</button>
                        </form>
                      </div>
                    </td>
                  </tr>
                {% endfor %}
                {% if not invoice_rows %}
                  <tr><td colspan="7" class="muted">Brak wystawionych faktur.</td></tr>
                {% endif %}
              </tbody>
            </table>
          </div>
        {% endblock %}
        """
        return render_template_string(tpl, title="Faktura", base_url=BASE_URL, db_path=DB_PATH, o=o, d=data, company=company, items=items, invoice_rows=invoice_rows, msg=msg, canonical_order_no=canonical_order_no, invoice_from_packing=invoice_from_packing)




    @app.get("/api/client_invoices")
    def api_client_invoices():
        maybe_pull_shared_from_supabase()
        email = _email_key(g.client_user["email"])

        c = conn()
        cur = c.cursor()
        cur.execute("""
          SELECT
            i.*,
            m.invoice_id AS meta_invoice_id,
            COALESCE(m.pdf_path,'') AS pdf_path,
            COALESCE(m.sent_to_client,0) AS sent_to_client,
            COALESCE(m.seen_by_client,0) AS seen_by_client,
            COALESCE(m.payment_reminder,0) AS payment_reminder,
            COALESCE(m.paid,0) AS paid,
            COALESCE(m.paid_at,'') AS paid_at,
            COALESCE(m.seen_at,'') AS seen_at,
            COALESCE(k.status,'draft') AS ksef_status,
            COALESCE(k.ksef_number,'') AS ksef_number,
            COALESCE(k.last_error,'') AS ksef_error,
            COALESCE(k.sent_at,'') AS ksef_sent_at,
            o.id AS source_order_id,
            o.order_no,
            o.created_at AS source_order_created_at,
            o.note AS source_order_note,
            o.customer_email AS order_customer_email
          FROM invoices i
          LEFT JOIN invoice_meta m ON m.invoice_id = i.id
          LEFT JOIN ksef_documents k ON k.invoice_id = i.id
          LEFT JOIN orders o ON o.id = i.order_id
          WHERE (
              LOWER(COALESCE(i.buyer_email,'')) = ?
              OR LOWER(COALESCE(o.customer_email,'')) = ?
            )
            AND (
              COALESCE(m.sent_to_client,0)=1
              OR m.invoice_id IS NULL
            )
          ORDER BY i.order_id DESC, i.id DESC
        """, (email, email))
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            if d.get("meta_invoice_id") is None:
                d["sent_to_client"] = 1
            d["order_display"] = order_display_no(
                d.get("source_order_id"),
                d.get("source_order_created_at"),
                d.get("order_no"),
                d.get("source_order_note")
            ) if d.get("source_order_id") else (d.get("order_no") or "")
            d["pdf_exists"] = 1 if d.get("pdf_path") else 0
            api_base = request.url_root.rstrip("/")
            d["download_url"] = f"{api_base}/api/invoices/{d.get('id')}/download?email={urllib.parse.quote_plus(email)}"
            rows.append(d)
        c.close()
        rows.sort(key=lambda x: ((x.get("seen_by_client") or 0), (x.get("issue_date") or ""), int(x.get("id") or 0)), reverse=True)
        return jsonify(ok=True, invoices=rows)




    @app.get("/invoices")
    def invoices():
        maybe_pull_shared_from_supabase()
        q = norm(request.args.get("q"))
        selected_customer = norm(request.args.get("customer"))
        selected_month = norm(request.args.get("month"))
        selected_payment = norm(request.args.get("payment"))
        selected_type = norm(request.args.get("document_type"))
        selected_currency = norm(request.args.get("currency")).upper()
        selected_ksef = norm(request.args.get("ksef"))
        selected_sent = norm(request.args.get("sent"))
        history_search_active = any((
            q, selected_customer, selected_month, selected_payment,
            selected_type, selected_currency, selected_ksef, selected_sent,
        ))
        default_limited = not history_search_active
        cutoff_date = (app_now().date() - timedelta(days=30)).isoformat()
        c = conn()
        cur = c.cursor()
        params = [] if history_search_active else [cutoff_date]
        where = "" if history_search_active else "WHERE i.issue_date >= ?"

        cur.execute(f"""
          SELECT
            i.*,
            COALESCE(m.pdf_path,'') AS pdf_path,
            COALESCE(m.sent_to_client,0) AS sent_to_client,
            COALESCE(m.seen_by_client,0) AS seen_by_client,
            COALESCE(m.payment_reminder,0) AS payment_reminder,
            COALESCE(m.paid,0) AS paid,
            COALESCE(m.paid_at,'') AS paid_at,
            COALESCE(m.seen_at,'') AS seen_at,
            COALESCE(k.status,'draft') AS ksef_status,
            COALESCE(k.ksef_number,'') AS ksef_number,
            COALESCE(k.last_error,'') AS ksef_error,
            COALESCE(k.sent_at,'') AS ksef_sent_at,
            o.id AS source_order_id,
            o.order_no AS source_order_no,
            o.created_at AS source_order_created_at,
            o.note AS source_order_note,
            o.customer_name AS order_customer_name
          FROM invoices i
          LEFT JOIN invoice_meta m ON m.invoice_id = i.id
          LEFT JOIN ksef_documents k ON k.invoice_id = i.id
          LEFT JOIN orders o ON o.id = i.order_id
          {where}
          ORDER BY LOWER(COALESCE(i.buyer_name, o.customer_name, '')), i.issue_date DESC, i.id DESC
        """, params)
        rows = [dict(r) for r in cur.fetchall()]
        c.close()

        # Filtry i statystyki tego ekranu są wyliczane wyłącznie w pamięci.
        # Nie zapisujemy danych ani nie zmieniamy istniejących akcji faktury.
        view = norm(request.args.get("view")) or "all"
        if view not in {"all", "customers"}:
            view = "all"
        today = app_now().date().isoformat()
        current_month = today[:7]

        for inv in rows:
            inv["customer_display"] = inv.get("buyer_name") or inv.get("order_customer_name") or "Bez klienta"
            inv["currency"] = normalize_order_currency(inv.get("currency"))
            inv["document_type"] = resolve_invoice_type(inv)
            inv["document_type_label"] = {"domestic": "KRAJOWA", "wdt": "WDT", "export": "EKSPORT"}.get(inv["document_type"], "KRAJOWA")
            due = norm(inv.get("payment_to"))[:10]
            inv["payment_status"] = "paid" if inv.get("paid") else ("overdue" if due and due < today else "unpaid")
            inv["payment_status_label"] = {"paid": "Zapłacona", "overdue": "Po terminie", "unpaid": "Nieopłacona"}[inv["payment_status"]]
            inv["pdf_ok"] = 1 if (invoice_pdf_exists(inv.get("pdf_path", ""), inv.get("invoice_no", ""))[0] or inv.get("invoice_items_json")) else 0

        all_rows = list(rows)
        summary = {
            "all": len(all_rows),
            "unpaid": sum(1 for inv in all_rows if not inv.get("paid")),
            "overdue": sum(1 for inv in all_rows if inv["payment_status"] == "overdue"),
            "paid": sum(1 for inv in all_rows if inv.get("paid")),
            "ksef": sum(1 for inv in all_rows if inv.get("ksef_status") == "sent"),
            "unsent": sum(1 for inv in all_rows if not inv.get("sent_to_client")),
        }
        month_totals = {}
        for inv in all_rows:
            if norm(inv.get("issue_date"))[:7] != current_month:
                continue
            total = month_totals.setdefault(inv["currency"], {"currency": inv["currency"], "net": 0.0, "gross": 0.0})
            total["net"] += float(inv.get("total_net") or 0)
            total["gross"] += float(inv.get("total_gross") or 0)

        customers = sorted({inv["customer_display"] for inv in all_rows}, key=str.casefold)
        months = sorted({norm(inv.get("issue_date"))[:7] for inv in all_rows if norm(inv.get("issue_date"))[:7]}, reverse=True)
        currencies = sorted({inv["currency"] for inv in all_rows})
        query = q.casefold()
        rows = [inv for inv in all_rows if (
            (not query or any(query in norm(inv.get(field)).casefold() for field in ("invoice_no", "customer_display", "source_order_no", "source_order_note")))
            and (not selected_customer or inv["customer_display"] == selected_customer)
            and (not selected_month or norm(inv.get("issue_date"))[:7] == selected_month)
            and (not selected_payment or (selected_payment == "open" and not inv.get("paid")) or inv["payment_status"] == selected_payment)
            and (not selected_type or inv["document_type"] == selected_type)
            and (not selected_currency or inv["currency"] == selected_currency)
            and (not selected_ksef or (selected_ksef == "none" and inv.get("ksef_status") not in {"sent", "ready", "error"}) or inv.get("ksef_status") == selected_ksef)
            and (not selected_sent or (selected_sent == "sent" and inv.get("sent_to_client")) or (selected_sent == "unsent" and not inv.get("sent_to_client")))
        )]
        rows.sort(key=lambda inv: (norm(inv.get("issue_date")), int(inv.get("id") or 0)), reverse=True)

        notice = ""
        notice_error = False
        if request.args.get("generated") == "1":
            if request.args.get("email_sent") == "1":
                notice = "Faktura została zapisana i wysłano klientowi wiadomość e-mail."
            elif request.args.get("email_sent") == "0":
                notice = "Faktura została zapisana, ale wiadomość e-mail nie została wysłana: " + (
                    norm(request.args.get("email_error")) or "nieznany błąd wysyłki"
                )
                notice_error = True

        groups = []
        groups_by_key = {}
        for inv in rows:
            customer_name = inv.get("buyer_name") or inv.get("order_customer_name") or "Bez klienta"
            display_name = re.sub(r",\s*", ", ", re.sub(r"\s+", " ", customer_name)).strip()
            buyer_tax_no = re.sub(r"\D", "", norm(inv.get("buyer_tax_no")))
            normalized_name = re.sub(r"[\W_]+", "", display_name.casefold(), flags=re.UNICODE)
            key = ("nip", buyer_tax_no) if buyer_tax_no else ("name", normalized_name)
            current = groups_by_key.get(key)
            if current is None:
                current = {"customer_name": display_name, "invoices": [], "months": [], "currency_totals": {}}
                groups.append(current)
                groups_by_key[key] = current
            inv["order_display"] = order_display_no(
                inv.get("source_order_id"),
                inv.get("source_order_created_at"),
                inv.get("source_order_no"),
                inv.get("source_order_note")
            ) if inv.get("source_order_id") else "-"
            inv["pdf_ok"] = 1 if (invoice_pdf_exists(inv.get("pdf_path", ""), inv.get("invoice_no", ""))[0] or inv.get("invoice_items_json")) else 0
            current["invoices"].append(inv)
            invoice_currency = normalize_order_currency(inv.get("currency"))
            inv["currency"] = invoice_currency
            currency_total = current["currency_totals"].setdefault(
                invoice_currency, {"currency": invoice_currency, "total_net": 0.0, "total_gross": 0.0}
            )
            currency_total["total_net"] += float(inv.get("total_net") or 0)
            currency_total["total_gross"] += float(inv.get("total_gross") or 0)

        for g in groups:
            month_map = {}
            for inv in g["invoices"]:
                issue_date = norm(inv.get("issue_date"))
                month_key = issue_date[:7] if len(issue_date) >= 7 else "bez-daty"
                month_label = month_key if month_key != "bez-daty" else "Bez daty"
                if month_key not in month_map:
                    month_map[month_key] = {"month": month_key, "label": month_label, "invoices": [], "currency_totals": {}}
                    g["months"].append(month_map[month_key])
                month = month_map[month_key]
                month["invoices"].append(inv)
                invoice_currency = inv["currency"]
                currency_total = month["currency_totals"].setdefault(
                    invoice_currency, {"currency": invoice_currency, "total_net": 0.0, "total_gross": 0.0}
                )
                currency_total["total_net"] += float(inv.get("total_net") or 0)
                currency_total["total_gross"] += float(inv.get("total_gross") or 0)

        return render_template_string(
            INVOICES_LIST_TEMPLATE, title="Faktury", base_url=BASE_URL, db_path=DB_PATH,
            rows=rows, groups=groups, q=q, view=view, summary=summary,
            month_totals=list(month_totals.values()), current_month=current_month,
            customers=customers, months=months, currencies=currencies,
            selected_customer=selected_customer, selected_month=selected_month,
            selected_payment=selected_payment, selected_type=selected_type,
            selected_currency=selected_currency, selected_ksef=selected_ksef,
            selected_sent=selected_sent, notice=notice, notice_error=notice_error,
            default_limited=default_limited, cutoff_date=cutoff_date,
        )

        tpl = r"""
        {% extends "base.html" %}
        {% block content %}
          <div class="card">
            <div class="flex">
              <h1 style="margin:0;">Faktury</h1>
            </div>
            <form method="get" class="flex" style="margin-top:12px;">
              <input name="q" value="{{ q }}" placeholder="Szukaj: klient, numer faktury, numer zamówienia, notatka">
              <button class="btn primary" type="submit">Szukaj</button>
              <a class="btn" href="{{ url_for('invoices') }}">Wyczyść</a>
            </form>
          </div>

          {% if notice %}
            <div class="card" style="{% if notice_error %}border-color:#fecaca;background:#fff1f2;color:#991b1b;{% endif %}">
              {{ notice }}
            </div>
          {% endif %}

          {% for g in groups %}
            <div class="card">
              <details {% if q %}open{% endif %}>
                <summary class="flex" style="cursor:pointer; align-items:center;">
                  <h2 style="margin:0;">{{ g.customer_name }}</h2>
                  <span class="badge">{{ g.invoices|length }} faktur</span>
                  {% for total in g.currency_totals.values() %}
                    <span class="badge">Netto: {{ "%.2f"|format(total.total_net) }} {{ total.currency }}</span>
                    <span class="badge">Brutto: {{ "%.2f"|format(total.total_gross) }} {{ total.currency }}</span>
                  {% endfor %}
                  <span class="btn right">Pokaż faktury</span>
                </summary>

                {% for m in g.months %}
                  <details style="margin-top:10px;" {% if q %}open{% endif %}>
                    <summary class="flex" style="cursor:pointer; align-items:center;">
                      <b>{{ m.label }}</b>
                      <span class="badge">{{ m.invoices|length }} faktur</span>
                      {% for total in m.currency_totals.values() %}
                        <span class="badge">Netto: {{ "%.2f"|format(total.total_net) }} {{ total.currency }}</span>
                        <span class="badge">Brutto: {{ "%.2f"|format(total.total_gross) }} {{ total.currency }}</span>
                      {% endfor %}
                    </summary>

                    <table style="margin-top:10px;">
                      <thead>
                        <tr>
                          <th>Faktura</th>
                          <th>Data</th>
                          <th>Zamówienie</th>
                          <th>Netto</th>
                          <th>Brutto</th>
                          <th>Status</th>
                          <th>Akcje</th>
                        </tr>
                      </thead>
                      <tbody>
                        {% for inv in m.invoices %}
                          <tr>
                            <td><b>{{ inv.invoice_no }}</b></td>
                            <td>{{ inv.issue_date }}</td>
                            <td>{{ inv.order_display }}</td>
                            <td>{{ "%.2f"|format(inv.total_net) }} {{ inv.currency }}</td>
                            <td>{{ "%.2f"|format(inv.total_gross) }} {{ inv.currency }}</td>
                            <td>
                              {% if inv.sent_to_client %}
                                <span class="badge ok">Udostępniona klientowi</span>
                              {% else %}
                                <span class="badge">Nieudostępniona</span>
                              {% endif %}
                              {% if inv.sent_to_client %}
                                {% if inv.seen_by_client %}
                                  <span class="badge ok">PDF pobrany</span>
                                  {% if inv.seen_at %}<div class="muted small">{{ inv.seen_at }}</div>{% endif %}
                                {% else %}
                                  <span class="badge">PDF niepobrany</span>
                                {% endif %}
                              {% endif %}
                              {% if not inv.pdf_ok %}
                                <span class="badge danger">Brak PDF</span>
                              {% endif %}
                              {% if inv.paid %}
                                <span class="badge ok">Opłacona</span>
                              {% else %}
                                <span class="badge danger">Nieopłacona</span>
                                {% if inv.payment_reminder %}<span class="badge ok">Przypomnienie wysłane</span>{% endif %}
                              {% endif %}
                              {% if inv.ksef_status == 'sent' %}
                                <span class="badge ok">W KSeF</span>
                                {% if inv.ksef_number %}<div class="muted small">{{ inv.ksef_number }}</div>{% endif %}
                              {% elif inv.ksef_status == 'ready' %}
                                <span class="badge ok">KSeF FA(3) OK</span>
                              {% elif inv.ksef_status == 'error' %}
                                <span class="badge danger">KSeF do poprawy</span>
                                {% if inv.ksef_error %}<div class="muted small">{{ inv.ksef_error }}</div>{% endif %}
                              {% else %}
                                <span class="badge">Nie wysłana do KSeF</span>
                              {% endif %}
                            </td>
                            <td>
                              <div class="flex">
                                <a class="btn" href="{{ url_for('invoice_download_admin', invoice_id=inv.id) }}" target="_blank">Faktura PDF</a>
                                {% if inv.source_order_id %}
                                <a class="btn" href="{{ url_for('order_packing_list_download_admin', order_id=inv.source_order_id) }}" target="_blank">Pakuj</a>
                                {% else %}
                                <a class="btn" href="{{ url_for('invoice_packing_list_download_admin', invoice_id=inv.id) }}" target="_blank">Pakuj</a>
                                {% endif %}
                                {% if not inv.sent_to_client %}
                                  <form method="post" action="{{ url_for('invoice_send_admin', invoice_id=inv.id) }}">
                                    <input type="hidden" name="next" value="{{ request.full_path }}">
                                    <button class="btn primary" type="submit">Udostępnij klientowi</button>
                                  </form>
                                {% endif %}
                                {% if inv.source_order_id %}
                                  <a class="btn" href="{{ url_for('order_view', order_id=inv.source_order_id) }}">Zamówienie</a>
                                {% endif %}
                                {% if not inv.paid %}
                                  <form method="post" action="{{ url_for('invoice_payment_reminder_admin', invoice_id=inv.id) }}">
                                    <input type="hidden" name="next" value="{{ request.full_path }}">
                                    <button class="btn" type="submit">Przypomnij o płatności</button>
                                  </form>
                                  <form method="post" action="{{ url_for('invoice_paid_admin', invoice_id=inv.id) }}">
                                    <input type="hidden" name="next" value="{{ request.full_path }}">
                                    <button class="btn ok" type="submit">Faktura opłacona</button>
                                  </form>
                                {% else %}
                                  <form method="post" action="{{ url_for('invoice_unpaid_admin', invoice_id=inv.id) }}">
                                    <input type="hidden" name="next" value="{{ request.full_path }}">
                                    <button class="btn" type="submit">Cofnij opłacenie</button>
                                  </form>
                                {% endif %}
                                {% if inv.ksef_status != 'sent' %}
                                  <a class="btn" href="{{ url_for('invoice_ksef_xml', invoice_id=inv.id) }}">XML KSeF FA(3)</a>
                                  <form method="post" action="{{ url_for('invoice_ksef_validate', invoice_id=inv.id) }}">
                                    <input type="hidden" name="next" value="{{ request.full_path }}">
                                    <button class="btn" type="submit">Sprawdź KSeF</button>
                                  </form>
                                  <form method="post" action="{{ url_for('invoice_ksef_send', invoice_id=inv.id) }}" onsubmit="return confirm('UWAGA: to jest realna wysyłka faktury do KSeF. Po wysłaniu faktura otrzyma numer KSeF i nie będzie można jej edytować. Kontynuować?');">
                                    <input type="hidden" name="next" value="{{ request.full_path }}">
                                    <button class="btn primary" type="submit">Wyślij do KSeF</button>
                                  </form>
                                  <a class="btn" href="{{ url_for('invoice_edit_admin', invoice_id=inv.id) }}">Edytuj</a>
                                  <form method="post" action="{{ url_for('invoice_delete_admin', invoice_id=inv.id) }}" onsubmit="return confirm('Usunąć fakturę {{ inv.invoice_no }}? To usunie też PDF i widoczność w panelu klienta.')">
                                    <input type="hidden" name="next" value="{{ request.full_path }}">
                                    <button class="btn danger" type="submit">Usuń</button>
                                  </form>
                                {% else %}
                                  <form method="post" action="{{ url_for('invoice_rollback_admin', invoice_id=inv.id) }}" onsubmit="return confirm('AWARYJNIE cofnąć fakturę {{ inv.invoice_no }} w aplikacji? To usunie lokalny zapis faktury, status KSeF, widoczność u klienta i przeliczy zamówienia oraz stany. Używaj tylko przy pomyłce/testach.');">
                                    <input type="hidden" name="next" value="{{ request.full_path }}">
                                    <button class="btn danger" type="submit">Cofnij fakturę</button>
                                  </form>
                                {% endif %}
                              </div>
                            </td>
                          </tr>
                        {% endfor %}
                      </tbody>
                    </table>
                  </details>
                {% endfor %}
              </details>
            </div>
          {% endfor %}

          {% if not groups %}
            <div class="card muted">Brak faktur.</div>
          {% endif %}
        {% endblock %}
        """
        return render_template_string(
            tpl, title="Faktury", base_url=BASE_URL, db_path=DB_PATH,
            groups=groups, q=q, notice=notice, notice_error=notice_error
        )




    @app.get("/ksef")
    def ksef_dashboard():
        maybe_pull_shared_from_supabase()
        ksef_cfg = ksef_config_summary()
        c = conn()
        cur = c.cursor()
        cur.execute("""
          SELECT i.*, COALESCE(k.status,'draft') AS ksef_status,
                 COALESCE(k.ksef_number,'') AS ksef_number,
                 COALESCE(k.last_error,'') AS ksef_error,
                 COALESCE(k.validated_at,'') AS ksef_validated_at,
                 COALESCE(k.sent_at,'') AS ksef_sent_at
          FROM invoices i
          LEFT JOIN ksef_documents k ON k.invoice_id=i.id
          ORDER BY i.issue_date DESC, i.id DESC
          LIMIT 200
        """)
        rows = [dict(r) for r in cur.fetchall()]
        c.close()

        counts = {"draft": 0, "ready": 0, "error": 0, "sent": 0}
        for r in rows:
            counts[r.get("ksef_status") or "draft"] = counts.get(r.get("ksef_status") or "draft", 0) + 1

        tpl = r"""
        {% extends "base.html" %}
        {% block content %}
          <div class="card">
            <div class="flex">
              <h1 style="margin:0;">KSeF</h1>
              <span class="badge">FA(3)</span>
            </div>
            <div class="hint" style="margin-top:10px;">
              Generator tworzy XML w strukturze FA(3). Przed wysłaniem sprawdź fakturę przyciskiem „Sprawdź” i przetestuj plik w Aplikacji Podatnika KSeF.
            </div>
            {% if not ksef_cfg.configured %}
              <div class="hint" style="margin-top:10px; border-color:#fecaca; background:#fff1f2;">
                Wysyłka bezpośrednia jest gotowa, ale w Render brakuje: <b>{{ ksef_cfg.missing|join(', ') }}</b>.
              </div>
            {% else %}
              <div class="hint" style="margin-top:10px;">
                Wysyłka bezpośrednia aktywna: <b>{{ ksef_cfg.env }}</b>.
              </div>
            {% endif %}
            <div class="kpi" style="margin-top:10px;">
              <div class="pill">Do sprawdzenia: <b>{{ counts.get('draft',0) }}</b></div>
              <div class="pill">FA(3) OK: <b>{{ counts.get('ready',0) }}</b></div>
              <div class="pill">Do poprawy: <b>{{ counts.get('error',0) }}</b></div>
              <div class="pill">Wysłane: <b>{{ counts.get('sent',0) }}</b></div>
            </div>
          </div>

          <div class="card">
            <table>
              <thead>
                <tr><th>Faktura</th><th>Klient</th><th>Data</th><th>Brutto</th><th>Status KSeF</th><th>Akcje</th></tr>
              </thead>
              <tbody>
                {% for inv in rows %}
                  <tr>
                    <td><b>{{ inv.invoice_no }}</b></td>
                    <td>{{ inv.buyer_name or '-' }}</td>
                    <td>{{ inv.issue_date }}</td>
                    <td>{{ "%.2f"|format(inv.total_gross or 0) }}</td>
                    <td>
                      {% if inv.ksef_status == 'ready' %}
                        <span class="badge ok">FA(3) OK</span>
                      {% elif inv.ksef_status == 'error' %}
                        <span class="badge danger">Do poprawy</span>
                      {% elif inv.ksef_status == 'sent' %}
                        <span class="badge ok">Wysłana</span>
                        {% if inv.ksef_number %}<div class="muted">{{ inv.ksef_number }}</div>{% endif %}
                      {% else %}
                        <span class="badge">Do sprawdzenia</span>
                      {% endif %}
                      {% if inv.ksef_error %}<div class="muted">{{ inv.ksef_error }}</div>{% endif %}
                    </td>
                    <td>
                      <div class="flex">
                        <form method="post" action="{{ url_for('invoice_ksef_validate', invoice_id=inv.id) }}">
                          <button class="btn" type="submit">Sprawdź</button>
                        </form>
                        <a class="btn primary" href="{{ url_for('invoice_ksef_xml', invoice_id=inv.id) }}">Pobierz XML KSeF FA(3)</a>
                        {% if inv.ksef_status != 'sent' %}
                          <form method="post" action="{{ url_for('invoice_ksef_send', invoice_id=inv.id) }}" onsubmit="return confirm('UWAGA: to jest realna wysyłka faktury do KSeF. Po wysłaniu faktura otrzyma numer KSeF i nie będzie można jej edytować. Kontynuować?');">
                            <input type="hidden" name="next" value="{{ request.full_path }}">
                            <button class="btn primary" type="submit">Wyślij do KSeF</button>
                          </form>
                          <form method="post" action="{{ url_for('invoice_ksef_mark_sent', invoice_id=inv.id) }}" onsubmit="return confirm('Oznaczyć fakturę jako wysłaną do KSeF?');" style="display:flex; gap:6px; flex-wrap:wrap; align-items:center;">
                            <input type="hidden" name="next" value="{{ request.full_path }}">
                            <input name="ksef_number" placeholder="Numer KSeF" style="width:220px;">
                            <button class="btn" type="submit">Oznacz wysłaną</button>
                          </form>
                          <a class="btn" href="{{ url_for('invoice_edit_admin', invoice_id=inv.id) }}">Edytuj fakturę</a>
                        {% else %}
                          <span class="badge ok">Wysłana do KSeF — edycja zablokowana</span>
                          <form method="post" action="{{ url_for('invoice_rollback_admin', invoice_id=inv.id) }}" onsubmit="return confirm('AWARYJNIE cofnąć fakturę {{ inv.invoice_no }} w aplikacji? To usunie lokalny zapis faktury, status KSeF, widoczność u klienta i przeliczy zamówienia oraz stany. Nie usuwa faktury z KSeF.');">
                            <input type="hidden" name="next" value="{{ request.full_path }}">
                            <button class="btn danger" type="submit">Cofnij w aplikacji</button>
                          </form>
                        {% endif %}
                      </div>
                    </td>
                  </tr>
                {% endfor %}
                {% if not rows %}
                  <tr><td colspan="6" class="muted">Brak faktur.</td></tr>
                {% endif %}
              </tbody>
            </table>
          </div>
        {% endblock %}
        """
        return render_template_string(tpl, title="KSeF", base_url=BASE_URL, db_path=DB_PATH, rows=rows, counts=counts, ksef_cfg=ksef_cfg)




    @app.post("/invoices/<int:invoice_id>/ksef/validate")
    def invoice_ksef_validate(invoice_id):
        inv, company, items, problems = build_invoice_ksef_payload(invoice_id)
        if not inv:
            return "Nie znaleziono faktury", 404
        if problems:
            upsert_ksef_doc(invoice_id, "error", last_error="; ".join(problems[:5]))
        else:
            xml = build_ksef_draft_xml(inv, company, items)
            path = ksef_xml_path(invoice_id, inv.get("invoice_no") or f"FV_{invoice_id}")
            schema = ksef_schema_path()
            schema_errors = validate_fa3_xml(xml, schema) if os.path.exists(schema) else []
            if schema_errors:
                upsert_ksef_doc(invoice_id, "error", last_error="; ".join(schema_errors[:3]))
                return redirect(request.form.get("next") or url_for("ksef_dashboard"))
            with open(path, "w", encoding="utf-8") as f:
                f.write(xml)
            upsert_ksef_doc(invoice_id, "ready", xml_path=path)
        return redirect(request.form.get("next") or url_for("ksef_dashboard"))




    @app.post("/invoices/<int:invoice_id>/ksef/mark-sent")
    def invoice_ksef_mark_sent(invoice_id):
        next_url = request.form.get("next") or url_for("ksef_dashboard")
        ksef_number = (request.form.get("ksef_number") or "").strip()
        if not ksef_number:
            upsert_ksef_doc(invoice_id, "error", last_error="Wpisz numer KSeF, żeby oznaczyć fakturę jako wysłaną.")
            return redirect(next_url)
        upsert_ksef_doc(invoice_id, "sent", ksef_number=ksef_number, last_error="")
        return redirect(next_url)




    @app.post("/invoices/<int:invoice_id>/ksef/send")
    def invoice_ksef_send(invoice_id):
        next_url = request.form.get("next") or url_for("ksef_dashboard")
        current_ksef = load_ksef_doc(invoice_id)
        if current_ksef.get("status") == "sent":
            return redirect(next_url)
        inv, company, items, problems = build_invoice_ksef_payload(invoice_id)
        if not inv:
            return "Nie znaleziono faktury", 404
        if problems:
            upsert_ksef_doc(invoice_id, "error", last_error="; ".join(problems[:5]))
            return redirect(next_url)

        xml = build_ksef_draft_xml(inv, company, items)
        schema = ksef_schema_path()
        schema_errors = validate_fa3_xml(xml, schema) if os.path.exists(schema) else []
        if schema_errors:
            upsert_ksef_doc(invoice_id, "error", last_error="; ".join(schema_errors[:3]))
            return redirect(next_url)

        path = ksef_xml_path(invoice_id, inv.get("invoice_no") or f"FV_{invoice_id}")
        with open(path, "w", encoding="utf-8") as f:
            f.write(xml)

        if send_invoice_to_ksef is None:
            upsert_ksef_doc(invoice_id, "error", xml_path=path, last_error="Brak modułu ksef_api.py albo zależności requests/cryptography.")
            return redirect(next_url)

        result = send_invoice_to_ksef(xml)
        if result.get("ok"):
            ksef_number = result.get("ksef_number") or (f"ref: {result.get('invoice_reference_number')}" if result.get("invoice_reference_number") else "")
            upsert_ksef_doc(invoice_id, "sent", xml_path=path, ksef_number=ksef_number)
        else:
            upsert_ksef_doc(invoice_id, "error", xml_path=path, last_error=result.get("message") or "Nie udało się wysłać faktury do KSeF.")
        return redirect(next_url)




    @app.get("/invoices/<int:invoice_id>/ksef/xml")
    def invoice_ksef_xml(invoice_id):
        inv, company, items, problems = build_invoice_ksef_payload(invoice_id)
        if not inv:
            return "Nie znaleziono faktury", 404
        if problems:
            upsert_ksef_doc(invoice_id, "error", last_error="; ".join(problems[:5]))
            return "Nie można wygenerować XML KSeF:\n- " + "\n- ".join(problems), 400

        xml = build_ksef_draft_xml(inv, company, items)
        schema = ksef_schema_path()
        schema_errors = validate_fa3_xml(xml, schema) if os.path.exists(schema) else []
        if schema_errors:
            upsert_ksef_doc(invoice_id, "error", last_error="; ".join(schema_errors[:3]))
            return "XML nie przeszedł walidacji FA(3):\n- " + "\n- ".join(schema_errors), 400

        path = ksef_xml_path(invoice_id, inv.get("invoice_no") or f"FV_{invoice_id}")
        with open(path, "w", encoding="utf-8") as f:
            f.write(xml)
        upsert_ksef_doc(invoice_id, "ready", xml_path=path)

        return send_file(path, mimetype="application/xml", as_attachment=True, download_name=xml_filename(inv.get("invoice_no") or f"FV_{invoice_id}"))




    @app.get("/invoices/<int:invoice_id>/download")
    def invoice_download_admin(invoice_id):
        row = load_invoice_with_meta(invoice_id)
        if not row:
            return "Nie znaleziono faktury", 404

        if parse_supabase_storage_ref(row.get("pdf_path", "")):
            try:
                data, filename = supabase_storage_download_bytes(row.get("pdf_path", ""))
                return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=True, download_name=filename)
            except Exception:
                pass

        ok_pdf, abs_path = invoice_pdf_exists(row.get("pdf_path", ""), row.get("invoice_no", ""))
        if not ok_pdf:
            c = conn()
            cur = c.cursor()
            cur.execute("SELECT * FROM orders WHERE id=?", (row["order_id"],))
            o = cur.fetchone()
            c.close()
            if not o:
                return "Brak powiązanego zamówienia", 404

            items = invoice_items_from_saved_json(invoice_id)
            if not items:
                return "Brak pozycji faktury", 400

            meta = invoice_meta_payload(row)
            abs_path, total_net, total_gross = generate_order_invoice_pdf(o, items, meta)
            packing_pdf_path = generate_invoice_packing_list_pdf(o, items, meta, abs_path)
            stored_pdf_path = upload_invoice_pdfs_to_supabase(invoice_id, row.get("invoice_no") or f"FV_{invoice_id}", abs_path, packing_pdf_path)

            current_meta = load_invoice_meta(invoice_id) or {}
            upsert_invoice_meta(
                invoice_id,
                stored_pdf_path,
                current_meta.get("invoice_items_json") or json.dumps(items, ensure_ascii=False),
                sent_to_client=int(current_meta.get("sent_to_client") or 0),
                seen_by_client=int(current_meta.get("seen_by_client") or 0),
                seen_at=current_meta.get("seen_at")
            )

            if supabase_enabled():
                try:
                    sync_local_rows_to_supabase("invoices", "id", [invoice_id])
                except Exception:
                    pass
                try:
                    sync_invoice_meta_to_supabase(invoice_id)
                except Exception:
                    pass
            if parse_supabase_storage_ref(stored_pdf_path):
                data, filename = supabase_storage_download_bytes(stored_pdf_path)
                return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=True, download_name=filename)

        if supabase_enabled() and abs_path and os.path.exists(abs_path) and not parse_supabase_storage_ref(row.get("pdf_path", "")):
            try:
                items = invoice_items_from_saved_json(invoice_id)
                packing_pdf_path = ""
                if items:
                    pack_candidate = packing_list_pdf_path_for_invoice(abs_path, row.get("invoice_no") or f"FV_{invoice_id}")
                    if os.path.exists(pack_candidate):
                        packing_pdf_path = pack_candidate
                stored_pdf_path = upload_invoice_pdfs_to_supabase(invoice_id, row.get("invoice_no") or f"FV_{invoice_id}", abs_path, packing_pdf_path)
                current_meta = load_invoice_meta(invoice_id) or {}
                upsert_invoice_meta(
                    invoice_id,
                    stored_pdf_path,
                    current_meta.get("invoice_items_json") or (json.dumps(items, ensure_ascii=False) if items else ""),
                    sent_to_client=int(current_meta.get("sent_to_client") or 0),
                    seen_by_client=int(current_meta.get("seen_by_client") or 0),
                    seen_at=current_meta.get("seen_at")
                )
                sync_invoice_meta_to_supabase(invoice_id)
                data, filename = supabase_storage_download_bytes(stored_pdf_path)
                return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=True, download_name=filename)
            except Exception:
                pass
        return send_file(abs_path, mimetype="application/pdf", as_attachment=True, download_name=os.path.basename(abs_path))




    @app.post("/invoices/<int:invoice_id>/regenerate")
    def invoice_regenerate_admin(invoice_id):
        inv = load_invoice_with_meta(invoice_id)
        if not inv:
            return "Nie znaleziono faktury", 404

        c = conn()
        cur = c.cursor()
        cur.execute("SELECT * FROM orders WHERE id=?", (inv["order_id"],))
        o = cur.fetchone()
        c.close()
        if not o:
            return "Brak powiÄ…zanego zamĂłwienia", 404

        items = invoice_items_from_saved_json(invoice_id)
        if not items:
            return "Brak pozycji faktury", 400

        meta = invoice_meta_payload(inv)
        auto_type, auto_currency, auto_country = automatic_invoice_tax_context(
            dict(o), inv.get("buyer_tax_no"), inv.get("buyer_country")
        )
        meta.update(invoice_type=auto_type, currency=auto_currency, buyer_country=auto_country or inv.get("buyer_country"))
        # Starsze błędne PDF-y WDT mogły mieć w JSON ceny z lokalnego cennika PLN.
        # Regeneracja odbudowuje je z cen zapisanych w zamówieniu klienta (EUR),
        # zachowując dokładnie te same alokacje i ilości.
        if auto_type == "wdt" and auto_currency == "EUR" and any(normalize_order_currency(x.get("currency")) != "EUR" for x in items):
            edit_rows = invoice_edit_items(invoice_id, dict(inv, invoice_type=auto_type))
            qty_form = {f"invoice_qty_{row['id']}": str(row.get("current_invoice_qty") or 0) for row in edit_rows}
            corrected = prepare_invoice_edit_items(edit_rows, qty_form, auto_type, auto_currency)
            if corrected:
                items = corrected
        pdf_path, total_net, total_gross = generate_order_invoice_pdf(o, items, meta)
        packing_pdf_path = generate_invoice_packing_list_pdf(o, items, meta, pdf_path)
        stored_pdf_path = upload_invoice_pdfs_to_supabase(invoice_id, inv["invoice_no"], pdf_path, packing_pdf_path)

        current_meta = load_invoice_meta(invoice_id) or {}
        upsert_invoice_meta(
            invoice_id,
            stored_pdf_path,
            current_meta.get("invoice_items_json") or json.dumps(items, ensure_ascii=False),
            sent_to_client=int(current_meta.get("sent_to_client") or 0),
            seen_by_client=int(current_meta.get("seen_by_client") or 0),
            seen_at=current_meta.get("seen_at")
        )

        if supabase_enabled():
            try:
                sync_local_rows_to_supabase("invoices", "id", [invoice_id])
            except Exception:
                pass
            try:
                sync_invoice_meta_to_supabase(invoice_id)
            except Exception:
                pass

        return redirect(request.referrer or url_for("orders"))




    @app.post("/invoices/<int:invoice_id>/payment-reminder")
    def invoice_payment_reminder_admin(invoice_id):
        _set_invoice_payment_state(invoice_id, reminder=1, paid=0)
        try:
            if send_payment_reminder:
                invoice_row, pdf_url = _invoice_email_context(invoice_id)
                send_payment_reminder(invoice_row, pdf_url=pdf_url)
        except Exception:
            pass
        return _redirect_after_invoice_action()




    @app.post("/invoices/<int:invoice_id>/paid")
    def invoice_paid_admin(invoice_id):
        _set_invoice_payment_state(invoice_id, reminder=0, paid=1)
        return _redirect_after_invoice_action()




    @app.post("/invoices/<int:invoice_id>/unpaid")
    def invoice_unpaid_admin(invoice_id):
        _set_invoice_payment_state(invoice_id, reminder=0, paid=0)
        return _redirect_after_invoice_action()



    @app.post("/api/invoices/<int:invoice_id>/seen")
    def api_invoice_seen(invoice_id):
        email = _email_key(g.client_user["email"])
        c = conn()
        cur = c.cursor()
        cur.execute("""
          SELECT
            i.id,
            m.invoice_id AS meta_invoice_id,
            i.buyer_email,
            o.customer_email AS order_customer_email,
            COALESCE(m.pdf_path,'') AS pdf_path,
            COALESCE(m.sent_to_client,0) AS sent_to_client,
            i.invoice_no
          FROM invoices i
          LEFT JOIN invoice_meta m ON m.invoice_id = i.id
          LEFT JOIN orders o ON o.id = i.order_id
          WHERE i.id=?
          LIMIT 1
        """, (invoice_id,))
        row = cur.fetchone()
        c.close()
        if not row:
            return jsonify(ok=False, error="Nie znaleziono faktury"), 404

        if email:
            buyer_ok = _email_key(row["buyer_email"]) == email
            order_ok = _email_key(row["order_customer_email"]) == email
            has_meta = row["meta_invoice_id"] is not None
            if (has_meta and int(row["sent_to_client"] or 0) != 1) or not (buyer_ok or order_ok):
                return jsonify(ok=False, error="Brak dostÄ™pu"), 403

        meta = load_invoice_meta(invoice_id) or {}
        ts = now_iso()
        upsert_invoice_meta(
            invoice_id,
            meta.get("pdf_path",""),
            meta.get("invoice_items_json",""),
            sent_to_client=int(meta.get("sent_to_client") or 0),
            seen_by_client=1,
            seen_at=ts
        )

        if supabase_enabled():
            try:
                sync_invoice_meta_to_supabase(invoice_id)
            except Exception:
                pass

        return jsonify(ok=True, seen_at=ts)



    @app.get("/api/invoices/<int:invoice_id>/download")
    def api_invoice_download(invoice_id):
        maybe_pull_shared_from_supabase()
        email = _email_key(g.client_user["email"])
        c = conn()
        cur = c.cursor()
        cur.execute("""
          SELECT
            i.*,
            m.invoice_id AS meta_invoice_id,
            COALESCE(m.pdf_path,'') AS pdf_path,
            COALESCE(m.sent_to_client,0) AS sent_to_client,
            o.customer_email AS order_customer_email
          FROM invoices i
          LEFT JOIN invoice_meta m ON m.invoice_id = i.id
          LEFT JOIN orders o ON o.id = i.order_id
          WHERE i.id=?
          LIMIT 1
        """, (invoice_id,))
        row = cur.fetchone()
        c.close()
        if not row:
            return "Nie znaleziono faktury", 404

        if email:
            buyer_ok = _email_key(row["buyer_email"]) == email
            order_ok = _email_key(row["order_customer_email"]) == email
            has_meta = row["meta_invoice_id"] is not None
            if (has_meta and int(row["sent_to_client"] or 0) != 1) or not (buyer_ok or order_ok):
                return "Brak dostÄ™pu", 403

        def mark_downloaded_by_client():
            if not email:
                return
            meta = load_invoice_meta(invoice_id) or {}
            upsert_invoice_meta(
                invoice_id,
                meta.get("pdf_path", ""),
                meta.get("invoice_items_json", ""),
                sent_to_client=int(meta.get("sent_to_client") or 0),
                seen_by_client=1,
                seen_at=now_iso(),
                payment_reminder=int(meta.get("payment_reminder") or 0),
                paid=int(meta.get("paid") or 0),
                paid_at=meta.get("paid_at")
            )
            if supabase_enabled():
                try:
                    sync_invoice_meta_to_supabase(invoice_id)
                except Exception:
                    pass

        if parse_supabase_storage_ref(row["pdf_path"]):
            try:
                data, filename = supabase_storage_download_bytes(row["pdf_path"])
                mark_downloaded_by_client()
                return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=True, download_name=filename)
            except Exception:
                pass

        ok_pdf, abs_path = invoice_pdf_exists(row["pdf_path"], row["invoice_no"])
        if not ok_pdf:
            cur_order = None
            c = conn()
            cur = c.cursor()
            cur.execute("SELECT * FROM orders WHERE id=?", (row["order_id"],))
            cur_order = cur.fetchone()
            c.close()
            if not cur_order:
                return "Brak powiązanego zamówienia", 404
            items = invoice_items_from_saved_json(invoice_id)
            if not items:
                return "Brak pozycji faktury", 400
            meta = invoice_meta_payload(dict(row))
            abs_path, total_net, total_gross = generate_order_invoice_pdf(cur_order, items, meta)
            packing_pdf_path = generate_invoice_packing_list_pdf(cur_order, items, meta, abs_path)
            stored_pdf_path = upload_invoice_pdfs_to_supabase(invoice_id, row["invoice_no"], abs_path, packing_pdf_path)
            current_meta = load_invoice_meta(invoice_id) or {}
            upsert_invoice_meta(
                invoice_id,
                stored_pdf_path,
                current_meta.get("invoice_items_json") or json.dumps(items, ensure_ascii=False),
                sent_to_client=int(current_meta.get("sent_to_client") or 0),
                seen_by_client=int(current_meta.get("seen_by_client") or 0),
                seen_at=current_meta.get("seen_at")
            )
            if supabase_enabled():
                try:
                    sync_invoice_meta_to_supabase(invoice_id)
                except Exception:
                    pass
            if parse_supabase_storage_ref(stored_pdf_path):
                data, filename = supabase_storage_download_bytes(stored_pdf_path)
                mark_downloaded_by_client()
                return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=True, download_name=filename)

        if supabase_enabled() and abs_path and os.path.exists(abs_path) and not parse_supabase_storage_ref(row["pdf_path"]):
            try:
                items = invoice_items_from_saved_json(invoice_id)
                packing_pdf_path = ""
                if items:
                    pack_candidate = packing_list_pdf_path_for_invoice(abs_path, row["invoice_no"])
                    if os.path.exists(pack_candidate):
                        packing_pdf_path = pack_candidate
                stored_pdf_path = upload_invoice_pdfs_to_supabase(invoice_id, row["invoice_no"], abs_path, packing_pdf_path)
                current_meta = load_invoice_meta(invoice_id) or {}
                upsert_invoice_meta(
                    invoice_id,
                    stored_pdf_path,
                    current_meta.get("invoice_items_json") or (json.dumps(items, ensure_ascii=False) if items else ""),
                    sent_to_client=int(current_meta.get("sent_to_client") or 0),
                    seen_by_client=int(current_meta.get("seen_by_client") or 0),
                    seen_at=current_meta.get("seen_at")
                )
                sync_invoice_meta_to_supabase(invoice_id)
                data, filename = supabase_storage_download_bytes(stored_pdf_path)
                mark_downloaded_by_client()
                return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=True, download_name=filename)
            except Exception:
                pass

        try:
            mark_downloaded_by_client()
            return send_file(abs_path, mimetype="application/pdf", as_attachment=True, download_name=os.path.basename(abs_path))
        except Exception as e:
            return f"BĹ‚Ä…d pobierania PDF: {e}", 500



    @app.post("/invoices/<int:invoice_id>/delete")
    def invoice_delete_admin(invoice_id):
        _delete_invoice_everywhere(invoice_id)
        return _redirect_after_invoice_action()




    @app.post("/invoices/<int:invoice_id>/rollback")
    def invoice_rollback_admin(invoice_id):
        _delete_invoice_everywhere(invoice_id)
        return _redirect_after_invoice_action()




    @app.post("/orders/<int:order_id>/invoice/<int:invoice_id>/delete")
    def order_invoice_delete(order_id, invoice_id):
        inv = load_invoice_with_meta(invoice_id)
        if not inv or int(inv.get("order_id") or 0) != int(order_id):
            abort(404)
        _delete_invoice_everywhere(invoice_id)

        return redirect(url_for("order_invoice", order_id=order_id, deleted="1"))




    @app.post("/invoices/<int:invoice_id>/send")
    def invoice_send_admin(invoice_id):
        _order_id, email_ok, email_error = _send_invoice_to_client(invoice_id)
        target = norm(request.values.get("next")) or request.referrer or url_for("invoices")
        separator = "&" if "?" in target else "?"
        if email_ok:
            return redirect(target + separator + "email_sent=1")
        return redirect(target + separator + "email_sent=0&email_error=" + urllib.parse.quote_plus(email_error[:300]))




    @app.post("/orders/<int:order_id>/invoice/<int:invoice_id>/send")
    def order_invoice_send(order_id, invoice_id):
        _order_id, email_ok, email_error = _send_invoice_to_client(invoice_id)
        if email_ok:
            return redirect(url_for("order_invoice", order_id=order_id, sent="1", invoice_id=invoice_id))
        return redirect(url_for(
            "order_invoice", order_id=order_id, invoice_id=invoice_id,
            email_error=email_error[:300]
        ))




    @app.route("/invoices/<int:invoice_id>/edit", methods=["GET", "POST"])
    def invoice_edit_admin(invoice_id):
        inv = load_invoice_with_meta(invoice_id)
        if not inv:
            return "Nie znaleziono faktury", 404
        ksef_doc = load_ksef_doc(invoice_id)
        if ksef_doc.get("status") == "sent":
            tpl = r"""
            {% extends "base.html" %}
            {% block content %}
              <div class="card">
                <div class="flex">
                  <h1 style="margin:0;">Faktura wysłana do KSeF</h1>
                  <a class="btn right" href="{{ url_for('invoices') }}">← Faktury</a>
                </div>
                <div class="hint" style="margin-top:10px;">
                  Ta faktura ma już numer KSeF i jej edycja została zablokowana, żeby nie powstała różnica między aplikacją a KSeF.
                </div>
                {% if ksef_doc.ksef_number %}
                  <p><b>Numer KSeF:</b> {{ ksef_doc.ksef_number }}</p>
                {% endif %}
                <div class="flex" style="margin-top:12px;">
                  <a class="btn" href="{{ url_for('invoice_download_admin', invoice_id=inv.id) }}" target="_blank">Faktura PDF</a>
                  <a class="btn" href="{{ url_for('invoice_ksef_xml', invoice_id=inv.id) }}">XML KSeF FA(3)</a>
                  <a class="btn" href="{{ url_for('invoices') }}">Wróć do faktur</a>
                </div>
              </div>
            {% endblock %}
            """
            return render_template_string(tpl, title="Faktura wysłana do KSeF", base_url=BASE_URL, db_path=DB_PATH, inv=inv, ksef_doc=ksef_doc)

        c = conn()
        cur = c.cursor()
        cur.execute("SELECT * FROM orders WHERE id=?", (inv["order_id"],))
        order_row = cur.fetchone()
        c.close()

        auto_type, auto_currency, auto_country = automatic_invoice_tax_context(
            dict(order_row) if order_row else {}, inv.get("buyer_tax_no"), inv.get("buyer_country")
        )
        inv["invoice_type"] = auto_type
        inv["currency"] = auto_currency
        inv["buyer_country"] = auto_country or inv.get("buyer_country")
        edit_items = invoice_edit_items(invoice_id, dict(inv))

        msg = ""
        if request.method == "POST":
            data = {k: norm(request.form.get(k)) for k in [
                "invoice_no", "issue_date", "sell_date", "payment_type", "payment_to",
                "buyer_name", "buyer_tax_no", "buyer_address", "buyer_country",
                "buyer_email", "buyer_phone", "invoice_type", "currency"
            ]}
            data["invoice_type"], data["currency"], automatic_country = automatic_invoice_tax_context(
                dict(order_row) if order_row else {}, data.get("buyer_tax_no"), data.get("buyer_country")
            )
            country_aliases = {
                "DEUTSCHLAND":"DE", "GERMANY":"DE", "NIEMCY":"DE",
                "POLSKA":"PL", "POLAND":"PL", "UNITED STATES":"US", "USA":"US",
                "UNITED KINGDOM":"GB", "GROSSBRITANNIEN":"GB", "WIELKA BRYTANIA":"GB",
            }
            data["buyer_country"] = automatic_country or country_aliases.get(data["buyer_country"].upper(), data["buyer_country"].upper())
            refresh_foreign_prices = (
                data["invoice_type"] == "wdt" and data["currency"] == "EUR"
                and any(
                    normalize_order_currency(item.get("currency")) != "EUR" or money_float(item.get("net_price")) <= 0
                    for item in edit_items if int(item.get("current_invoice_qty") or 0) > 0
                )
            )
            if refresh_foreign_prices and data["invoice_type"] == "wdt" and data["currency"] == "EUR":
                try:
                    eur_rows = supabase_request(
                        "/rest/v1/pricing_eur", method="GET",
                        params={"select": "sku,price_eur,uvp_eur", "limit": 5000}, timeout=30,
                    ) or []
                    eur_by_sku = {norm(row.get("sku")).lower(): row for row in eur_rows}
                    missing_eur = []
                    for edit_item in edit_items:
                        eur_price = eur_by_sku.get(norm(edit_item.get("sku")).lower()) or {}
                        price = money_float(eur_price.get("price_eur"))
                        if price <= 0 and int(edit_item.get("current_invoice_qty") or 0) > 0:
                            missing_eur.append(norm(edit_item.get("sku")))
                            continue
                        if price > 0:
                            edit_item["net_price"] = price
                            edit_item["gross_price"] = price
                            edit_item["currency"] = "EUR"
                    if missing_eur:
                        msg = "Brak ceny EUR dla SKU: " + ", ".join(missing_eur[:10])
                except Exception as exc:
                    msg = "Nie udało się pobrać cennika EUR: " + str(exc)
            invoice_items = prepare_invoice_edit_items(edit_items, request.form, data["invoice_type"], data["currency"])
            existing_invoice_id = invoice_no_exists(data["invoice_no"], invoice_id)
            if msg:
                pass
            elif not data["invoice_no"]:
                msg = "Numer faktury jest wymagany."
            elif not data["invoice_type"]:
                msg = "Wybierz typ podatkowy faktury."
            elif existing_invoice_id:
                msg = f"Faktura o takim numerze już istnieje! Numer: {data['invoice_no']}. Wybierz inny numer faktury."
            elif not invoice_items:
                msg = "Faktura musi zawierać co najmniej jedną pozycję."
            else:
                old_order_ids = sorted({int(x.get("source_order_id") or x.get("order_id") or 0) for x in edit_items if int(x.get("current_invoice_qty") or 0) > 0})
                st, pc, city = split_address(data.get("buyer_address", ""))
                c = conn()
                cur = c.cursor()
                cur.execute("""
                  UPDATE invoices
                  SET invoice_no=?, issue_date=?, sell_date=?, payment_type=?, payment_to=?,
                      buyer_name=?, buyer_tax_no=?, buyer_street=?, buyer_post_code=?, buyer_city=?,
                      buyer_country=?, buyer_email=?, buyer_phone=?, invoice_type=?, currency=?
                  WHERE id=?
                """, (
                    data["invoice_no"], data["issue_date"], data["sell_date"], data["payment_type"], data["payment_to"],
                    data["buyer_name"], data["buyer_tax_no"], st, pc, city,
                    data["buyer_country"], data["buyer_email"], data["buyer_phone"], data["invoice_type"], data["currency"], invoice_id
                ))
                c.commit()
                c.close()

                updated = load_invoice_with_meta(invoice_id)
                if invoice_items and updated:
                    order_for_pdf = order_row
                    if not order_for_pdf:
                        first_order_id = int(invoice_items[0].get("source_order_id") or invoice_items[0].get("order_id") or 0)
                        if first_order_id:
                            c = conn()
                            cur = c.cursor()
                            cur.execute("SELECT * FROM orders WHERE id=?", (first_order_id,))
                            order_for_pdf = cur.fetchone()
                            c.close()

                    pdf_path, total_net, total_gross = generate_order_invoice_pdf(order_for_pdf, invoice_items, invoice_meta_payload(updated))
                    packing_pdf_path = generate_invoice_packing_list_pdf(order_for_pdf, invoice_items, invoice_meta_payload(updated), pdf_path)
                    stored_pdf_path = upload_invoice_pdfs_to_supabase(invoice_id, data["invoice_no"], pdf_path, packing_pdf_path)
                    allocation_ids = replace_invoice_allocations(invoice_id, invoice_items)
                    new_order_ids = sorted({int(x.get("source_order_id") or x.get("order_id") or 0) for x in invoice_items})
                    touched_order_ids = sorted(set(old_order_ids + new_order_ids))
                    changed_order_ids, changed_product_ids = reconcile_orders_after_invoice_change(touched_order_ids)

                    c = conn()
                    cur = c.cursor()
                    cur.execute("UPDATE invoices SET total_net=?, total_gross=? WHERE id=?", (total_net, total_gross, invoice_id))
                    c.commit()
                    c.close()

                    meta = load_invoice_meta(invoice_id) or {}
                    upsert_invoice_meta(
                        invoice_id,
                        stored_pdf_path,
                        json.dumps(invoice_items, ensure_ascii=False),
                        sent_to_client=int(meta.get("sent_to_client") or 0),
                        seen_by_client=0,
                        seen_at=None,
                        payment_reminder=int(meta.get("payment_reminder") or 0),
                        paid=int(meta.get("paid") or 0),
                        paid_at=meta.get("paid_at")
                    )

                if supabase_enabled():
                    try:
                        sync_local_rows_to_supabase("invoices", "id", [invoice_id])
                    except Exception:
                        pass
                    try:
                        sync_invoice_meta_to_supabase(invoice_id)
                    except Exception:
                        pass
                    try:
                        supabase_delete_rows("invoice_allocations", {"invoice_id": invoice_id})
                    except Exception:
                        pass
                    if allocation_ids:
                        try:
                            sync_local_rows_to_supabase("invoice_allocations", "id", allocation_ids)
                        except Exception:
                            pass
                    if changed_order_ids:
                        try:
                            sync_local_rows_to_supabase("orders", "id", changed_order_ids)
                        except Exception:
                            pass
                    if changed_product_ids:
                        try:
                            sync_local_rows_to_supabase("stock", "product_id", changed_product_ids)
                        except Exception:
                            pass

                return redirect(url_for("invoices", edited="1", invoice_id=invoice_id))

        buyer_address = "\n".join([x for x in [
            inv.get("buyer_street") or "",
            " ".join([inv.get("buyer_post_code") or "", inv.get("buyer_city") or ""]).strip()
        ] if x])

        tpl = r"""
        {% extends "base.html" %}
        {% block content %}
          <div class="card">
            <div class="flex">
              <h1 style="margin:0;">Edytuj fakturę {{ inv.invoice_no }}</h1>
              <a class="btn right" href="{{ url_for('invoices') }}">← Faktury</a>
            </div>
            {% if msg %}<div class="hint" style="margin-top:10px;">{{ msg }}</div>{% endif %}
          </div>

          <div class="card">
            <form method="post" class="row">
              <div><label class="muted small">Numer faktury</label><input name="invoice_no" value="{{ inv.invoice_no }}" required></div>
              <input type="hidden" name="invoice_type" value="{{ inv.invoice_type }}">
              <input type="hidden" name="currency" value="{{ inv.currency }}">
              <div><label class="muted small">Rozliczenie</label><div class="hint"><b>{{ 'WDT 0%' if inv.invoice_type=='wdt' else ('Eksport 0%' if inv.invoice_type=='export' else 'Krajowa 23%') }}</b> · {{ inv.currency }} — z zamówienia i cennika klienta</div></div>
              <div><label class="muted small">Data wystawienia</label><input name="issue_date" type="date" value="{{ inv.issue_date }}"></div>
              <div><label class="muted small">Data sprzedaży</label><input name="sell_date" type="date" value="{{ inv.sell_date }}"></div>
              <div><label class="muted small">Forma płatności</label>
                <select name="payment_type">
                  <option value="gotowka" {% if inv.payment_type in ['cash','gotowka'] %}selected{% endif %}>gotówka</option>
                  <option value="przelew" {% if inv.payment_type in ['transfer','przelew'] %}selected{% endif %}>przelew</option>
                  <option value="karta" {% if inv.payment_type in ['card','karta'] %}selected{% endif %}>karta</option>
                </select>
              </div>
              <div><label class="muted small">Termin płatności</label><input name="payment_to" type="date" value="{{ inv.payment_to }}"></div>
              <div><label class="muted small">Nabywca</label><input name="buyer_name" value="{{ inv.buyer_name }}"></div>
              <div><label class="muted small">NIP / VAT UE / Tax ID nabywcy</label><input name="buyer_tax_no" value="{{ inv.buyer_tax_no }}"></div>
              <div><label class="muted small">Adres nabywcy</label><textarea name="buyer_address" placeholder="Ulica&#10;Kod pocztowy Miasto">{{ buyer_address }}</textarea></div>
              <div><label class="muted small">Kraj</label><input name="buyer_country" value="{{ inv.buyer_country or 'PL' }}"></div>
              <div><label class="muted small">Email</label><input name="buyer_email" value="{{ inv.buyer_email }}"></div>
              <div><label class="muted small">Telefon</label><input name="buyer_phone" value="{{ inv.buyer_phone }}"></div>
              <div style="grid-column:1/-1;">
                <h2>Pozycje faktury</h2>
                <div class="hint" style="margin-bottom:10px;">
                  Zmień ilości pozycji na tej fakturze. Wpisanie 0 usuwa pozycję z faktury.
                </div>
                <table>
                  <thead>
                    <tr>
                      <th>Zamówienie</th>
                      <th>Notatka</th>
                      <th>SKU</th>
                      <th>Model / Nazwa</th>
                      <th>Zamówiono</th>
                      <th>Na innych fakturach</th>
                      <th>Maks. na tej fakturze</th>
                      <th>Ilość na fakturze</th>
                      <th>Netto/szt ({{ inv.currency }})</th>
                      <th>Brutto/szt ({{ inv.currency }})</th>
                    </tr>
                  </thead>
                  <tbody>
                    {% for it in edit_items %}
                      <tr>
                        <td><b>{{ it.source_order_no }}</b></td>
                        <td>{{ it.source_order_note or '-' }}</td>
                        <td>{{ it.sku }}</td>
                        <td>{{ it.model or '' }}{% if it.name %}<div class="muted small">{{ it.name }}</div>{% endif %}</td>
                        <td>{{ it.ordered_qty }}</td>
                        <td>{{ it.invoiced_other_qty }}</td>
                        <td><b>{{ it.remaining_qty }}</b></td>
                        <td><input type="number" min="0" max="{{ it.remaining_qty }}" name="invoice_qty_{{ it.id }}" value="{{ it.current_invoice_qty }}" style="width:110px;"></td>
                        <td>{{ "%.2f"|format(it.net_price) }}</td>
                        <td>{{ "%.2f"|format(it.gross_price) }}</td>
                      </tr>
                    {% endfor %}
                  </tbody>
                </table>
              </div>
              <div style="grid-column:1/-1;" class="flex">
                <button class="btn primary" type="submit">Zapisz i regeneruj PDF</button>
                <a class="btn" href="{{ url_for('invoice_download_admin', invoice_id=inv.id) }}" target="_blank">Podgląd PDF</a>
              </div>
            </form>
          </div>
        {% endblock %}
        """
        return render_template_string(tpl, title="Edytuj fakturę", base_url=BASE_URL, db_path=DB_PATH, inv=inv, buyer_address=buyer_address, msg=msg, edit_items=edit_items)


    exported = {'order_invoice': order_invoice, 'api_client_invoices': api_client_invoices, 'invoices': invoices, 'ksef_dashboard': ksef_dashboard, 'invoice_ksef_validate': invoice_ksef_validate, 'invoice_ksef_mark_sent': invoice_ksef_mark_sent, 'invoice_ksef_send': invoice_ksef_send, 'invoice_ksef_xml': invoice_ksef_xml, 'invoice_download_admin': invoice_download_admin, 'invoice_regenerate_admin': invoice_regenerate_admin, 'invoice_payment_reminder_admin': invoice_payment_reminder_admin, 'invoice_paid_admin': invoice_paid_admin, 'invoice_unpaid_admin': invoice_unpaid_admin, 'api_invoice_seen': api_invoice_seen, 'api_invoice_download': api_invoice_download, 'invoice_delete_admin': invoice_delete_admin, 'invoice_rollback_admin': invoice_rollback_admin, 'order_invoice_delete': order_invoice_delete, 'invoice_send_admin': invoice_send_admin, 'order_invoice_send': order_invoice_send, 'invoice_edit_admin': invoice_edit_admin}
    globals().update(exported)
    return exported
