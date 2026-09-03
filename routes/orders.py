"""Mechanically extracted Flask routes; business logic is unchanged."""

def register_routes(context):
    globals().update(context)


    @app.get("/orders/stock-issue-audit")
    def stock_issue_audit():
        maybe_pull_shared_from_supabase(force=True)
        c = conn()
        try:
            rows = missed_stock_issue_candidates(c.cursor())
        finally:
            c.close()
        tpl = r"""
        {% extends "base.html" %}
        {% block content %}
          <div class="card">
            <div class="flex">
              <div>
                <h1 style="margin:0 0 8px;">Audyt wydań magazynowych</h1>
                <div class="muted">Wysłane pełne zamówienia, które nadal nie mają potwierdzonego zdjęcia stanu.</div>
              </div>
              <a class="btn right" href="{{ url_for('orders') }}">← Zamówienia</a>
            </div>
          </div>
          <div class="card">
            <div class="flex" style="margin-bottom:16px;">
              <div><b>Znaleziono: {{ rows|length }}</b>{% if rows %}<div class="muted">Zakres: {{ rows[0]['shipped_at'] }} – {{ rows[-1]['shipped_at'] }}</div>{% endif %}</div>
              {% if rows %}
              <form class="right" method="post" action="{{ url_for('stock_issue_repair') }}" onsubmit="return confirm('Odjąć ze stanu wszystkie pozycje z {{ rows|length }} wykazanych zamówień?');">
                <button class="btn danger" type="submit">Napraw wykazane stany</button>
              </form>
              {% endif %}
            </div>
            {% if request.args.get('repaired') %}<div class="badge" style="margin-bottom:14px;">Naprawiono zamówienia: {{ request.args.get('repaired') }}</div>{% endif %}
            <table>
              <thead><tr><th>Zamówienie</th><th>Status</th><th>Wysłano</th><th>Sztuki</th><th>Pozycje</th></tr></thead>
              <tbody>
              {% for row in rows %}
                <tr><td><a href="{{ url_for('order_view', order_id=row['id']) }}"><b>{{ canonical_order_no(row['id'], row['created_at'], row['order_no']) }}</b></a></td><td>{{ order_status_label(row['status']) }}</td><td>{{ row['shipped_at'] }}</td><td><b>{{ row['item_qty'] }}</b></td><td>{{ row['items_label'] }}</td></tr>
              {% endfor %}
              {% if not rows %}<tr><td colspan="5" class="muted">Brak wysłanych zamówień wymagających naprawy.</td></tr>{% endif %}
              </tbody>
            </table>
          </div>
        {% endblock %}
        """
        return render_template_string(tpl, title="Audyt wydań", base_url=BASE_URL, db_path=DB_PATH, rows=rows, canonical_order_no=canonical_order_no, order_status_label=order_status_label)




    @app.post("/orders/stock-issue-audit/repair")
    def stock_issue_repair():
        maybe_pull_shared_from_supabase(force=True)
        c = conn()
        changed_orders = []
        changed_products = []
        try:
            cur = c.cursor()
            for row in missed_stock_issue_candidates(cur):
                product_ids = issue_order_stock(cur, int(row["id"]))
                if product_ids:
                    changed_orders.append(int(row["id"]))
                    changed_products.extend(product_ids)
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()
        if supabase_enabled():
            if changed_orders:
                sync_local_rows_to_supabase("orders", "id", changed_orders)
            if changed_products:
                sync_local_rows_to_supabase("stock", "product_id", sorted(set(changed_products)))
        return redirect(url_for("stock_issue_audit", repaired=len(changed_orders)))




    @app.get("/orders")
    def orders():
        maybe_pull_shared_from_supabase()
        q = norm(request.args.get("q"))
        tab = norm(request.args.get("tab")) or "new"
        ready_today = norm(request.args.get("ready_today")) == "1"
        created_today = norm(request.args.get("created_today")) == "1"
        issued_today = norm(request.args.get("issued_today")) == "1"
        if tab not in {"new", "issued", "realized", "all"}:
            tab = "new"

        c = conn()
        cur = c.cursor()

        cur.execute("""
          SELECT
            SUM(CASE WHEN LOWER(COALESCE(status,''))='completed' THEN 1 ELSE 0 END) AS completed_count,
            SUM(CASE WHEN LOWER(COALESCE(status,'')) IN ('in_delivery','issued')
              OR (COALESCE(warehouse_issued,0)=1 AND LOWER(COALESCE(status,'')) NOT IN ('completed','cancelled'))
              THEN 1 ELSE 0 END) AS issued_count,
            SUM(CASE WHEN COALESCE(warehouse_issued,0)=0
              AND LOWER(COALESCE(status,'')) NOT IN ('in_delivery','issued','completed','cancelled')
              THEN 1 ELSE 0 END) AS to_issue_count
          FROM orders
        """)
        stats_row = cur.fetchone()
        order_stats = {
            "completed": int(stats_row["completed_count"] or 0),
            "issued": int(stats_row["issued_count"] or 0),
            "to_issue": int(stats_row["to_issue_count"] or 0),
        }

        where_parts = []
        params = []

        if tab == "new":
            where_parts.append("COALESCE(o.warehouse_issued,0)=0")
            where_parts.append("LOWER(COALESCE(o.status,'')) NOT IN ('in_delivery','issued','completed','cancelled')")
        elif tab == "issued":
            where_parts.append("(LOWER(COALESCE(o.status,'')) IN ('in_delivery','issued') OR (COALESCE(o.warehouse_issued,0)=1 AND LOWER(COALESCE(o.status,'')) NOT IN ('completed','cancelled')))")
        elif tab == "realized":
            where_parts.append("LOWER(COALESCE(o.status,''))='completed'")

        if created_today:
            where_parts.append("date(o.created_at)=date('now','localtime')")
        if issued_today:
            where_parts.append("""EXISTS (
              SELECT 1 FROM invoice_allocations ia
              WHERE ia.order_id=o.id AND date(ia.created_at)=date('now','localtime')
              UNION ALL
              SELECT 1 FROM invoices i
              WHERE i.order_id=o.id AND date(i.created_at)=date('now','localtime')
                AND NOT EXISTS (SELECT 1 FROM invoice_allocations ia2 WHERE ia2.invoice_id=i.id)
            )""")

        if q:
            where_parts.append("(order_no LIKE ? OR customer_name LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like])

        where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        sql = f"""
          SELECT o.*,
                 COALESCE((
                   SELECT SUM(oi.qty * COALESCE(oi.unit_net_price, pr.net_price, 0))
                   FROM order_items oi
                   LEFT JOIN products p ON p.id=oi.product_id
                   LEFT JOIN pricing pr ON (TRIM(LOWER(pr.model)) = TRIM(LOWER(p.model)) OR TRIM(LOWER(pr.model)) = TRIM(LOWER(p.sku)))
                   WHERE oi.order_id=o.id
                 ), 0) AS order_value_net,
                 CASE WHEN EXISTS (
                   SELECT 1
                   FROM order_items oi
                   LEFT JOIN stock s ON s.product_id=oi.product_id
                   WHERE oi.order_id=o.id
                     AND (
                       COALESCE(s.qty,0) + COALESCE((
                         SELECT SUM(ci.qty)
                         FROM china_items ci
                         JOIN china_packages cp ON cp.id=ci.package_id
                         WHERE ci.product_id=oi.product_id
                           AND cp.status IN ('planned', 'ordered', 'shipped')
                       ),0)
                     ) < oi.qty
                 ) THEN 1 ELSE 0 END AS has_shortage
          FROM orders o
          {where_sql}
          ORDER BY o.id DESC
          LIMIT 300
        """
        cur.execute(sql, tuple(params))
        rows = [dict(r) for r in cur.fetchall()]

        visible_open_ids = sorted([r["id"] for r in rows if int(r.get("warehouse_issued") or 0) == 0 and norm(r["status"]).lower() in CURRENT_ORDER_STATUSES])
        if visible_open_ids:
            status_ph = ",".join(["?"] * len(CURRENT_ORDER_STATUSES))
            cur.execute(f"SELECT id FROM orders WHERE COALESCE(warehouse_issued,0)=0 AND LOWER(COALESCE(status,'')) IN ({status_ph}) AND id<=? ORDER BY id", (*sorted(CURRENT_ORDER_STATUSES), visible_open_ids[-1]))
            open_order_ids = [int(r["id"]) for r in cur.fetchall()]

            ph = ",".join(["?"] * len(open_order_ids))
            cur.execute(f"""
              SELECT oi.order_id, oi.product_id, SUM(oi.qty) AS qty
              FROM order_items oi
              WHERE oi.order_id IN ({ph})
              GROUP BY oi.order_id, oi.product_id
            """, tuple(open_order_ids))
            demand_rows = cur.fetchall()

            by_order = {}
            product_ids = set()
            for dr in demand_rows:
                oid = int(dr["order_id"])
                pid = int(dr["product_id"])
                qty = int(dr["qty"])
                by_order.setdefault(oid, []).append((pid, qty))
                product_ids.add(pid)

            pool_stock = {}
            pool_delivery = {}
            if product_ids:
                pph = ",".join(["?"] * len(product_ids))
                cur.execute(f"""
                  SELECT p.id AS product_id,
                         COALESCE(s.qty,0) AS stock_qty,
                         COALESCE((
                           SELECT SUM(ci.qty)
                           FROM china_items ci
                           JOIN china_packages cp ON cp.id=ci.package_id
                           WHERE ci.product_id=p.id
                             AND cp.status IN ('planned', 'ordered', 'shipped')
                         ),0) AS in_delivery_qty
                  FROM products p
                  LEFT JOIN stock s ON s.product_id=p.id
                  WHERE p.id IN ({pph})
                """, tuple(product_ids))
                for pr in cur.fetchall():
                    pid = int(pr["product_id"])
                    pool_stock[pid] = int(pr["stock_qty"])
                    pool_delivery[pid] = int(pr["in_delivery_qty"])

            has_shortage = {oid: 0 for oid in open_order_ids}
            for oid in open_order_ids:
                for pid, need0 in by_order.get(oid, []):
                    need = int(need0)
                    stock_now = pool_stock.get(pid, 0)
                    from_stock = min(stock_now, need)
                    pool_stock[pid] = stock_now - from_stock
                    need -= from_stock

                    delivery_now = pool_delivery.get(pid, 0)
                    from_delivery = min(delivery_now, need)
                    pool_delivery[pid] = delivery_now - from_delivery
                    need -= from_delivery

                    if need > 0:
                        has_shortage[oid] = 1

            for r in rows:
                if r["status"] in ("new", "packed", "confirmed", "in_delivery"):
                    r["has_shortage"] = has_shortage.get(r["id"], 0)
                else:
                    r["has_shortage"] = 0

        if ready_today:
            # Dokladnie ten sam warunek co na kafelku pulpitu: cale zamowienie
            # musi miescic sie w fizycznym stanie. Towar w drodze nie wystarcza.
            status_ph = ",".join(["?"] * len(CURRENT_ORDER_STATUSES))
            cur.execute(f"""
              SELECT o.id, o.created_at, oi.product_id, SUM(oi.qty) AS required_qty
              FROM orders o
              JOIN order_items oi ON oi.order_id=o.id
              WHERE LOWER(COALESCE(o.status,'')) IN ({status_ph})
                AND COALESCE(o.warehouse_issued,0)=0
              GROUP BY o.id, oi.product_id
              ORDER BY o.created_at, o.id, oi.product_id
            """, tuple(sorted(CURRENT_ORDER_STATUSES)))
            ready_demand = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT product_id, MAX(0, COALESCE(qty,0)) AS qty FROM stock")
            ready_stock = {int(r["product_id"]): int(r["qty"] or 0) for r in cur.fetchall()}
            ready_orders = {}
            for demand in ready_demand:
                oid = int(demand["id"])
                ready_orders.setdefault(oid, []).append(
                    (int(demand["product_id"]), int(demand["required_qty"] or 0))
                )
            ready_ids = set()
            for oid, needs in ready_orders.items():
                if needs and all(ready_stock.get(pid, 0) >= qty for pid, qty in needs):
                    ready_ids.add(oid)
                    for pid, qty in needs:
                        ready_stock[pid] = ready_stock.get(pid, 0) - qty
            rows = [r for r in rows if int(r["id"]) in ready_ids]

        c.close()

        tpl = r"""
        {% extends "base.html" %}
        {% block content %}
          <style>
            .order-stat-card{position:relative;display:flex;align-items:center;gap:16px;min-height:122px;text-decoration:none;color:inherit;cursor:pointer;overflow:hidden;transition:transform .15s ease,box-shadow .15s ease,border-color .15s ease;background:#fff;}
            .order-stat-card::after{content:'›';position:absolute;right:22px;top:50%;transform:translateY(-50%);font-size:30px;font-weight:400;opacity:.34;}
            .order-stat-card:hover{transform:translateY(-3px);box-shadow:0 16px 34px rgba(24,45,84,.14);}
            .order-stat-card:focus-visible{outline:3px solid rgba(79,111,235,.28);outline-offset:2px;}
            .order-stat-icon{display:grid;place-items:center;flex:0 0 50px;width:50px;height:50px;border-radius:15px;font-size:22px;font-weight:900;}
            .order-stat-copy{min-width:0;padding-right:34px;}.order-stat-label{font-weight:750;}.order-stat-value{font-size:34px;font-weight:850;line-height:1;margin-top:10px;}.order-stat-hint{font-size:12px;margin-top:7px;color:#71809f;}
            .order-stat-card.completed{border-color:#cdebdc;background:linear-gradient(135deg,#fff 45%,#effaf4)}.order-stat-card.completed .order-stat-icon{background:#dcf7e8;color:#08744d;}
            .order-stat-card.issued{border-color:#d8e2ff;background:linear-gradient(135deg,#fff 45%,#f0f4ff)}.order-stat-card.issued .order-stat-icon{background:#e1e9ff;color:#3156c7;}
            .order-stat-card.to-issue{border-color:#f4dfad;background:linear-gradient(135deg,#fff 45%,#fff8e8)}.order-stat-card.to-issue .order-stat-icon{background:#ffefc7;color:#9a6200;}
            .order-stat-card.active{border-width:2px;box-shadow:0 12px 28px rgba(24,45,84,.14);}.order-stat-card.active::after{opacity:.75;}
            .orders-toolbar{display:flex;align-items:center;gap:10px;justify-content:flex-end;margin-bottom:12px;}.orders-toolbar .all-orders{margin-right:auto;}
            @media(max-width:760px){.order-stat-card{min-height:104px}.orders-toolbar{align-items:stretch;flex-direction:column}.orders-toolbar .all-orders{margin-right:0}}
          </style>
          <div class="grid3">
            <a class="card order-stat-card completed {% if tab=='realized' %}active{% endif %}" href="{{ url_for('orders', tab='realized') }}" aria-label="Pokaż zrealizowane zamówienia" {% if tab=='realized' %}aria-current="page"{% endif %}>
              <div class="order-stat-icon">✓</div><div class="order-stat-copy"><div class="order-stat-label">Zrealizowane łącznie</div><div class="order-stat-value">{{ order_stats.completed }}</div><div class="order-stat-hint">Pokaż zakończone</div></div>
            </a>
            <a class="card order-stat-card issued {% if tab=='issued' %}active{% endif %}" href="{{ url_for('orders', tab='issued') }}" aria-label="Pokaż wydane zamówienia" {% if tab=='issued' %}aria-current="page"{% endif %}>
              <div class="order-stat-icon">⇢</div><div class="order-stat-copy"><div class="order-stat-label">Wydane</div><div class="order-stat-value">{{ order_stats.issued }}</div><div class="order-stat-hint">Pokaż wydane</div></div>
            </a>
            <a class="card order-stat-card to-issue {% if tab=='new' %}active{% endif %}" href="{{ url_for('orders', tab='new') }}" aria-label="Pokaż zamówienia do wydania" {% if tab=='new' %}aria-current="page"{% endif %}>
              <div class="order-stat-icon">○</div><div class="order-stat-copy"><div class="order-stat-label">Do wydania</div><div class="order-stat-value">{{ order_stats.to_issue }}</div><div class="order-stat-hint">Pokaż oczekujące</div></div>
            </a>
          </div>
          <div class="card">
            <div class="orders-toolbar">
              <a class="btn all-orders {% if tab=='all' %}primary{% endif %}" href="{{ url_for('orders', tab='all') }}">Wszystkie zamówienia</a>
              <a class="btn" href="{{ url_for('stock_issue_audit') }}">Audyt wydań</a>
              <a class="btn primary" href="{{ url_for('order_new') }}">+ Nowe zamówienie</a>
            </div>
            <form method="get" class="grid3">
              <input type="hidden" name="tab" value="{{ tab }}">
              <input name="q" value="{{ q }}" placeholder="Szukaj: numer zamĂłwienia lub klient">
              <button class="btn primary" type="submit">Szukaj</button>
              <a class="btn" href="{{ url_for('orders', tab=tab) }}">WyczyĹ›Ä‡</a>
            </form>
          </div>

          <style>
            .st-unconfirmed{background:#ef4444;color:#fff;border-color:#ef4444;}
            .st-confirmed{background:#16a34a;color:#fff;border-color:#16a34a;}
            .st-delivery{background:#2563eb;color:#fff;border-color:#2563eb;}
            .st-issued{background:#6b7280;color:#fff;border-color:#6b7280;}
          </style>

          <div class="card">
            <table>
              <thead>
                <tr><th>Nr</th><th>Klient</th><th>Status</th><th>WartoĹ›Ä‡ netto</th><th>Data</th><th>Akcje</th></tr>
              </thead>
              <tbody>
                {% for r in rows %}
                  <tr {% if tab == 'new' and (r['has_shortage'] or r['status'] in ['new','pending','unconfirmed']) %}style="background:#ffe7e7;"{% endif %}>
                    <td><b>{{ order_display_no(r['id'], r['created_at'], r['order_no'], r['note']) }}</b></td>
                    <td>{{ r['customer_name'] }}</td>
                    <td><span class="badge {{ order_status_css(r['status']) }}">{{ order_status_label(r['status']) }}</span></td>
                    <td><span class="badge">{{ "%.2f"|format(r['order_value_net']) }} {{ r['currency'] or 'PLN' }}</span></td>
                    <td class="muted">{{ r['created_at'] }}</td>
                    <td class="flex">
                      <a class="btn" href="{{ url_for('order_view', order_id=r['id']) }}">SzczegĂłĹ‚y</a>
                      {% if r['status'] != 'issued' %}
                        <a class="btn" href="{{ url_for('order_label', order_id=r['id']) }}">Etykieta 30x50</a>
                        <form method="post" action="{{ url_for('order_delete', order_id=r['id']) }}" onsubmit="return confirm('UsunÄ…Ä‡ zamĂłwienie?')">
                          <button class="btn danger" type="submit">UsuĹ„</button>
                        </form>
                      {% else %}
                        <span class="muted">PodglÄ…d</span>
                      {% endif %}
                    </td>
                  </tr>
                {% endfor %}
                {% if not rows %}
                  <tr><td colspan="5" class="muted">Brak zamĂłwieĹ„.</td></tr>
                {% endif %}
              </tbody>
            </table>
          </div>
        {% endblock %}
        """
        return render_template_string(tpl, title="ZamĂłwienia", base_url=BASE_URL, db_path=DB_PATH, rows=rows, q=q, tab=tab, order_stats=order_stats, order_status_label=order_status_label, order_status_css=order_status_css, canonical_order_no=canonical_order_no)



    @app.get("/orders/new")
    def order_new():
        maybe_pull_shared_from_supabase()
        c = conn()
        cur = c.cursor()
        cur.execute("SELECT id, sku, model, name FROM products WHERE COALESCE(archived,0)=0 ORDER BY sku LIMIT 5000")
        products_rows = cur.fetchall()
        cur.execute("SELECT id, name, address, phone, email, nip FROM customers ORDER BY name")
        customers_rows = cur.fetchall()
        c.close()

        tpl = r"""
        {% extends "base.html" %}
        {% block content %}
          <div class="card">
            <h1>Nowe zamĂłwienie</h1>
            <div class="muted">Produkty wybierasz z bazy. Przy wyborze pokazuje stan magazynowy.</div>
          </div>

          <div class="card">
            <form method="post" action="{{ url_for('order_create') }}">
              <div class="row">
                <div>
                  <label class="muted small">Wybierz staĹ‚ego klienta (opcjonalnie)</label>
                  <select id="customerSelect" name="customer_id" onchange="fillCustomer(this.value)">
                    <option value="">-- rÄ™cznie / nowy klient --</option>
                    {% for c in customers %}
                      <option value="{{ c['id'] }}">{{ c['name'] }}</option>
                    {% endfor %}
                  </select>
                </div>
                <div class="muted">Po wyborze pola klienta zostanÄ… automatycznie uzupeĹ‚nione.</div>
              </div>

              <div class="row">
                <div>
                  <label class="muted small">ZamawiajÄ…cy (nazwa firmy / osoba)</label>
                  <input name="customer_name" required>
                </div>
                <div>
                  <label class="muted small">Telefon</label>
                  <input name="customer_phone">
                </div>
              </div>

              <div class="row" style="margin-top:10px;">
                <div>
                  <label class="muted small">Adres (na etykietÄ™)</label>
                  <textarea name="customer_address" placeholder="Ulica, kod, miasto, kraj"></textarea>
                </div>
                <div>
                  <label class="muted small">Email</label>
                  <input name="customer_email">
                  <div style="height:10px;"></div>
                  <label class="muted small">Adres WysyĹ‚ki</label>
                  <input name="note">
                </div>
              </div>

              <div class="line"></div>

              <div class="flex">
                <h2 style="margin:0;">Pozycje zamĂłwienia</h2>
                <button class="btn" onclick="addItemRow(); return false;">+ Dodaj pozycjÄ™</button>
              </div>

              <div id="itemsContainer" style="margin-top:10px;"></div>

              <template id="itemRowTpl">
                <div class="items-row card" style="margin:10px 0;">
                  <div>
                    <label class="muted small">Produkt (SKU)</label>
                    <select name="product_id[]" onchange="refreshStock(this.value, this.dataset.stockTarget)" data-stock-target="">
                      <option value="">-- wybierz --</option>
                      {% for p in products %}
                        <option value="{{ p['id'] }}">{{ p['sku'] }}{% if p['model'] %} â€˘ {{ p['model'] }}{% endif %}{% if p['name'] %} â€˘ {{ p['name'] }}{% endif %}</option>
                      {% endfor %}
                    </select>
                  </div>
                  <div>
                    <label class="muted small">IloĹ›Ä‡</label>
                    <input name="qty[]" value="1">
                  </div>
                  <div>
                    <label class="muted small">Stan</label>
                    <div class="badge" id="">-</div>
                  </div>
                  <div class="flex" style="align-items:flex-end;">
                    <button class="btn danger" onclick="removeRow(this); return false;">UsuĹ„</button>
                  </div>
                </div>
              </template>

              <div class="line"></div>
              <button class="btn primary" type="submit">Zapisz zamĂłwienie</button>
              <a class="btn" href="{{ url_for('orders') }}">Anuluj</a>
            </form>
          </div>

    <script>
    // po dodaniu wiersza trzeba podpiÄ…Ä‡ ID na badge (stan)
    function addItemRow(){
      const tpl = document.getElementById("itemRowTpl");
      const container = document.getElementById("itemsContainer");
      const node = tpl.content.cloneNode(true);

      // znajdĹş select i badge w nowo wstawionym wierszu
      const wrap = node.querySelector(".items-row");
      const select = wrap.querySelector("select");
      const badge = wrap.querySelector(".badge");

      const id = "stock_" + Math.random().toString(36).slice(2);
      badge.id = id;
      select.dataset.stockTarget = id;

      container.appendChild(node);
    }

    addItemRow(); // startowo 1 pozycja

    const customersData = {{ customers_json|safe }};
    function fillCustomer(customerId){
      if(!customerId || !customersData[customerId]) return;
      const c = customersData[customerId];
      document.querySelector('input[name="customer_name"]').value = c.name || '';
      document.querySelector('textarea[name="customer_address"]').value = c.address || '';
      document.querySelector('input[name="customer_phone"]').value = c.phone || '';
      document.querySelector('input[name="customer_email"]').value = c.email || '';
    }
    </script>

        {% endblock %}
        """
        customers_json = {
            str(r["id"]): {
                "name": r["name"],
                "address": r["address"],
                "phone": r["phone"],
                "email": r["email"],
            }
            for r in customers_rows
        }
        return render_template_string(
            tpl,
            title="Nowe zamĂłwienie",
            base_url=BASE_URL,
            db_path=DB_PATH,
            products=products_rows,
            customers=customers_rows,
            customers_json=json.dumps(customers_json, ensure_ascii=False)
        )



    @app.post("/orders/create")
    def order_create():
        customer_id = to_int(request.form.get("customer_id"), 0)
        customer_name = norm(request.form.get("customer_name"))
        if not customer_name:
            return "Brak zamawiajÄ…cego", 400

        customer_address = norm(request.form.get("customer_address"))
        customer_phone = norm(request.form.get("customer_phone"))
        customer_email = norm(request.form.get("customer_email"))
        note = norm(request.form.get("note"))

        product_ids = request.form.getlist("product_id[]")
        qtys = request.form.getlist("qty[]")

        items = []
        for pid, q in zip(product_ids, qtys):
            pid = to_int(pid, 0)
            qty = to_int(q, 0)
            if pid > 0 and qty > 0:
                items.append((pid, qty))

        if not items:
            return "Dodaj minimum 1 pozycjÄ™", 400

        if supabase_enabled():
            oid = remote_first_create_order(customer_id if customer_id > 0 else None, customer_name, customer_address, customer_phone, customer_email, note, items)
        else:
            c = conn()
            cur = c.cursor()
            created_at = now_iso()
            cur.execute("""
              INSERT INTO orders(order_no, customer_id, customer_name, customer_address, customer_phone, customer_email, status, note, created_at, qr_data_url)
              VALUES(?,?,?,?,?,?,?,?,?,?)
            """, ("TEMP", customer_id if customer_id > 0 else None, customer_name, customer_address, customer_phone, customer_email, "new", note, created_at, ""))
            oid = cur.lastrowid

            order_no = make_order_no(oid, created_at)
            qr_data_url = ""
            cur.execute("UPDATE orders SET order_no=?, qr_data_url=? WHERE id=?", (order_no, qr_data_url, oid))

            for pid, qty in items:
                cur.execute("SELECT sku FROM products WHERE id=?", (pid,))
                p = cur.fetchone()
                if not p:
                    continue
                sku = p["sku"]
                cur.execute("""
                  INSERT INTO order_items(order_id, product_id, sku, qty, created_at)
                  VALUES(?,?,?,?,?)
                """, (oid, pid, sku, qty, now_iso()))

            c.commit()
            c.close()

        try:
            normalize_temp_order_numbers()
        except Exception:
            pass
        return redirect(url_for("order_view", order_id=oid))



    @app.get("/orders/<int:order_id>")
    def order_view(order_id):
        maybe_pull_shared_from_supabase()
        c = conn()
        cur = c.cursor()
        cur.execute("SELECT * FROM orders WHERE id=?", (order_id,))
        o = cur.fetchone()
        if not o:
            c.close()
            abort(404)

        cur.execute("""
          SELECT oi.*, p.model, p.ean, p.name,
                 COALESCE(s.qty, 0) AS stock_qty,
                 COALESCE(oi.unit_net_price, pr.net_price, 0) AS net_price,
                 COALESCE(oi.unit_gross_price, pr.gross_price, 0) AS gross_price,
                 COALESCE(oi.currency, ord.currency, 'PLN') AS currency,
                 (oi.qty * COALESCE(oi.unit_net_price, pr.net_price, 0)) AS line_value_net,
                 (oi.qty * COALESCE(oi.unit_gross_price, pr.gross_price, 0)) AS line_value_gross,
                 COALESCE(s.qty,0) AS stock,
                 COALESCE((
                    SELECT SUM(ci.qty)
                    FROM china_items ci
                    JOIN china_packages cp ON cp.id=ci.package_id
                    WHERE ci.product_id=oi.product_id
                      AND cp.status IN ('planned', 'ordered', 'shipped')
                 ), 0) AS in_delivery
          FROM order_items oi
          JOIN orders ord ON ord.id=oi.order_id
          JOIN products p ON p.id=oi.product_id
          LEFT JOIN stock s ON s.product_id=p.id
          LEFT JOIN pricing pr ON (TRIM(LOWER(pr.model)) = TRIM(LOWER(p.model)) OR TRIM(LOWER(pr.model)) = TRIM(LOWER(p.sku)))
          WHERE oi.order_id=?
          ORDER BY oi.id
        """, (order_id,))
        items = [dict(r) for r in cur.fetchall()]

        for it in items:
            it["in_delivery_available"] = int(it.get("in_delivery", 0))
            it["delivery_used"] = 0
            it["line_shortage"] = 0

        if norm(o["status"]).lower() in CURRENT_ORDER_STATUSES:
            status_ph = ",".join(["?"] * len(CURRENT_ORDER_STATUSES))
            cur.execute(f"SELECT id FROM orders WHERE LOWER(COALESCE(status,'')) IN ({status_ph}) AND id<=? ORDER BY id", (*sorted(CURRENT_ORDER_STATUSES), order_id))
            scoped_order_ids = [int(r["id"]) for r in cur.fetchall()]
            if scoped_order_ids:
                sph = ",".join(["?"] * len(scoped_order_ids))
                cur.execute(f"""
                  SELECT oi.id, oi.order_id, oi.product_id, oi.qty
                  FROM order_items oi
                  WHERE oi.order_id IN ({sph})
                  ORDER BY oi.order_id, oi.id
                """, tuple(scoped_order_ids))
                seq_items = cur.fetchall()

                product_ids = {int(r["product_id"]) for r in seq_items}
                pool_stock = {}
                pool_delivery = {}
                if product_ids:
                    pph = ",".join(["?"] * len(product_ids))
                    cur.execute(f"""
                      SELECT p.id AS product_id,
                             COALESCE(s.qty,0) AS stock_qty,
                             COALESCE((
                               SELECT SUM(ci.qty)
                               FROM china_items ci
                               JOIN china_packages cp ON cp.id=ci.package_id
                               WHERE ci.product_id=p.id
                                 AND cp.status IN ('planned', 'ordered', 'shipped')
                             ),0) AS in_delivery_qty
                      FROM products p
                      LEFT JOIN stock s ON s.product_id=p.id
                      WHERE p.id IN ({pph})
                    """, tuple(product_ids))
                    for pr in cur.fetchall():
                        pid = int(pr["product_id"])
                        pool_stock[pid] = int(pr["stock_qty"])
                        pool_delivery[pid] = int(pr["in_delivery_qty"])

                item_alloc = {}
                for sr in seq_items:
                    pid = int(sr["product_id"])
                    need = int(sr["qty"])

                    stock_now = pool_stock.get(pid, 0)
                    from_stock = min(stock_now, need)
                    pool_stock[pid] = stock_now - from_stock
                    need_after_stock = need - from_stock

                    delivery_now = pool_delivery.get(pid, 0)
                    from_delivery = min(delivery_now, need_after_stock)
                    pool_delivery[pid] = delivery_now - from_delivery
                    shortage = need_after_stock - from_delivery

                    if int(sr["order_id"]) == order_id:
                        item_alloc[int(sr["id"])] = {
                            "in_delivery_available": from_delivery,
                            "delivery_used": from_delivery,
                            "line_shortage": shortage,
                        }

                for it in items:
                    al = item_alloc.get(int(it["id"]))
                    if al:
                        it.update(al)

        cur.execute("SELECT id, sku, model, name FROM products WHERE COALESCE(archived,0)=0 ORDER BY sku LIMIT 5000")
        products_rows = cur.fetchall()
        c.close()

        order_url = build_public_url(url_for("order_view", order_id=order_id))

        tpl = r"""
        {% extends "base.html" %}
        {% block content %}
          <div class="card">
            <div class="flex">
              <h1 style="margin:0;">{{ order_display_no(o['id'], o['created_at'], o['order_no'], o['note']) }}</h1>
              <span class="badge {{ order_status_css(o['status']) }}">{{ order_status_label(o['status']) }}</span>
              <form method="post" action="{{ url_for('order_status_update', order_id=o['id']) }}" class="flex" style="margin-left:10px;">
                <select name="status" aria-label="Ręczna korekta statusu">
                  {% for status_key in ['new','confirmed','packed','packed_partial','in_delivery','shipped','partially_shipped','issued','completed','cancelled'] %}
                    <option value="{{ status_key }}" {% if (o['status'] or '')|lower == status_key %}selected{% endif %}>{{ order_status_label(status_key) }}</option>
                  {% endfor %}
                </select>
                <button class="btn" type="submit" onclick="return confirm('Zapisać ręczną korektę statusu? Nie zmieni to stanu magazynowego ani nie wyśle e-maila.')">Zmień status</button>
              </form>
              <div class="right flex">
                <a class="btn" href="{{ url_for('orders') }}">â† Lista</a>
                <a class="btn primary" href="{{ url_for('order_packing_list_download_admin', order_id=o['id']) }}">Pakuj</a>
                {% if (o['currency'] or 'PLN') == 'EUR' %}
                  <a class="btn primary" href="{{ url_for('order_proforma', order_id=o['id']) }}" target="_blank">Proforma EUR</a>
                  <a class="btn primary" href="{{ url_for('order_invoice', order_id=o['id']) }}">Faktura WDT 0%</a>
                {% else %}
                  <a class="btn primary" href="{{ url_for('order_invoice', order_id=o['id']) }}">Faktura</a>
                {% endif %}
                <form method="post" action="{{ url_for('order_confirmation_resend', order_id=o['id']) }}">
                  <button class="btn" type="submit">Wyślij ponownie potwierdzenie</button>
                </form>
                  <a class="btn primary" href="{{ url_for('order_label', order_id=o['id']) }}">Etykieta 30x50</a>
                  {% if locked %}
                    <span class="badge">Wydane z magazynu</span>
                  {% endif %}
                  <form method="post" action="{{ url_for('order_delete', order_id=o['id']) }}" onsubmit="return confirm('UsunÄ…Ä‡ zamĂłwienie?')">
                    <button class="btn danger" type="submit">UsuĹ„ zamĂłwienie</button>
                  </form>
              </div>
            </div>
            <div class="muted" style="margin-top:6px;">{{ o['created_at'] }}</div>
            <form method="post" action="{{ url_for('order_mark_shipped', order_id=o['id']) }}" class="flex" style="margin-top:14px;padding:14px;border:1px solid #dbe4f2;border-radius:16px;background:#f8fbff;">
              <div><b>Wysyłka do klienta</b><div class="muted">Wpisz numer przesyłki i oznacz zamówienie jako wysłane.</div></div>
              {% if o['inpost_shipment_id'] %}
                <a class="btn primary" href="{{ url_for('order_inpost_label', order_id=o['id'], bundle='1') }}">Pobierz PDF A4 + A6</a>
              {% else %}
                <a class="btn" href="{{ url_for('order_inpost_create', order_id=o['id']) }}">Generuj etykietę InPost</a>
              {% endif %}
              <select name="carrier" required style="min-width:150px;">
                <option value="">-- Kurier --</option>
                {% for carrier_key, carrier_name in [('inpost','InPost'),('dpd','DPD'),('fedex','FedEx'),('dhl','DHL'),('ups','UPS')] %}
                  <option value="{{ carrier_key }}" {% if (o['carrier'] or '')|lower == carrier_key %}selected{% endif %}>{{ carrier_name }}</option>
                {% endfor %}
              </select>
              <input name="tracking_no" value="{{ o['tracking_no'] or '' }}" placeholder="Numer śledzenia" required style="min-width:260px;">
              <label style="display:flex;align-items:center;gap:7px;"><input type="checkbox" name="notify_customer" value="1" checked> Wyślij e-mail klientowi</label>
              <button class="btn primary" type="submit">Wysłane</button>
              {% if o['tracking_no'] %}<a class="btn" target="_blank" href="{{ carrier_tracking_url(o['carrier'], o['tracking_no']) }}">Śledź</a>{% endif %}
            </form>
            {% if request.args.get('shipment_sent') == '1' %}<div class="hint" style="margin-top:10px;">Status i numer przesyłki zapisane.{% if request.args.get('notification_skipped') == '1' %} Powiadomienie klienta zostało pominięte.{% else %} Klient otrzymał e-mail.{% endif %}</div>{% endif %}
            {% if request.args.get('shipment_email_error') %}<div class="hint" style="margin-top:10px;">Status zapisany, ale e-mail nie został wysłany: {{ request.args.get('shipment_email_error') }}</div>{% endif %}
            {% if request.args.get('confirmation_sent') == '1' %}
              <div class="hint" style="margin-top:10px;">Potwierdzenie zamówienia zostało wysłane ponownie.</div>
            {% elif request.args.get('confirmation_error') %}
              <div class="hint" style="margin-top:10px; border-color:#fecaca; background:#fff1f2;">Nie udało się wysłać potwierdzenia: {{ request.args.get('confirmation_error') }}</div>
            {% endif %}
          </div>

          <div class="row">
            <div class="card">
              <h2>ZamawiajÄ…cy</h2>
              <div><b>{{ o['customer_name'] }}</b></div>
              <div class="muted" style="white-space:pre-line; margin-top:6px;">{{ o['customer_address'] or "-" }}</div>
              <div class="muted" style="margin-top:6px;">Tel: {{ o['customer_phone'] or "-" }}</div>
              <div class="muted">Email: {{ o['customer_email'] or "-" }}</div>
              <div class="line"></div>
              <div class="muted small">Kod zamĂłwienia do skanowania: <b>{{ canonical_order_no(o['id'], o['created_at'], o['order_no']) }}</b></div>
              <div class="muted small" style="margin-top:10px;">QR jest uĹĽywany do etykiety 30x50 i skanowania zamĂłwienia.</div>
            </div>

            <div class="card">
              <h2>Notatka</h2>
              <div>{{ o['note'] or "-" }}</div>
              <div class="line"></div>
              <div class="hint">
                <b>Wydaj z magazynu</b> odejmie iloĹ›ci z magazynu, ale nie zmieni automatycznie statusu klienta na â€žZrealizowaneâ€ť.<br>
                JeĹ›li brakuje stanu, pozycja moĹĽe byÄ‡ realizowana z <b>towaru w drodze z Chin</b> (kolumna â€žW dostawieâ€ť poniĹĽej).
              </div>
            </div>
          </div>

          {% if not locked %}
          <div class="card">
            <h2>Dodaj produkt do zamĂłwienia</h2>
            <form method="post" action="{{ url_for('order_item_add', order_id=o['id']) }}" class="items-row">
              <div>
                <select name="product_id" required>
                  <option value="">-- wybierz produkt --</option>
                  {% for p in products %}
                    <option value="{{ p['id'] }}">{{ p['sku'] }}{% if p['model'] %} â€˘ {{ p['model'] }}{% endif %}{% if p['name'] %} â€˘ {{ p['name'] }}{% endif %}</option>
                  {% endfor %}
                </select>
              </div>
              <div>
                <input name="qty" value="1" required>
              </div>
              <div class="flex" style="align-items:flex-end;">
                <button class="btn primary" type="submit">Dodaj</button>
              </div>
            </form>
          </div>
          {% endif %}

          <div class="card">
            <h2>Pozycje</h2>
            <table>
              <thead>
                <tr><th>SKU</th><th>Model / Nazwa</th><th>IloĹ›Ä‡</th><th>Cena netto</th><th>Cena brutto</th><th>WartoĹ›Ä‡ netto</th><th>WartoĹ›Ä‡ brutto</th><th>Stan teraz</th><th>W dostawie (dostÄ™pne)</th><th>Realizacja</th><th>Akcje</th></tr>
              </thead>
              <tbody>
                {% set ns = namespace(total_net=0, total_gross=0) %}
                {% for it in items %}
                  {% set ns.total_net = ns.total_net + it['line_value_net'] %}
                  {% set ns.total_gross = ns.total_gross + it['line_value_gross'] %}
                  <tr>
                    <td><b>{{ it['sku'] }}</b></td>
                    <td>
                      {{ it['model'] or "" }}
                      {% if it['name'] %}<div class="muted small">{{ it['name'] }}</div>{% endif %}
                      {% if it['ean'] %}<div class="muted small">EAN: {{ it['ean'] }}</div>{% endif %}
                    </td>
                    <td>
                      {% if locked %}
                        <span class="badge">{{ it['qty'] }}</span>
                      {% else %}
                        <form method="post" action="{{ url_for('order_item_update', order_id=o['id'], item_id=it['id']) }}" class="flex">
                          <input name="qty" value="{{ it['qty'] }}" style="width:90px;">
                          <button class="btn" type="submit">ZmieĹ„</button>
                        </form>
                      {% endif %}
                    </td>
                    <td><span class="badge">{{ "%.2f"|format(it['net_price']) }} {{ it['currency'] or o['currency'] or 'PLN' }}</span></td>
                    <td><span class="badge">{{ "%.2f"|format(it['gross_price']) }} {{ it['currency'] or o['currency'] or 'PLN' }}</span></td>
                    <td><span class="badge">{{ "%.2f"|format(it['line_value_net']) }} {{ it['currency'] or o['currency'] or 'PLN' }}</span></td>
                    <td><span class="badge">{{ "%.2f"|format(it['line_value_gross']) }} {{ it['currency'] or o['currency'] or 'PLN' }}</span></td>
                    <td><span class="badge">{{ it['stock'] }}</span></td>
                    <td><span class="badge">{{ it['in_delivery_available'] }}</span></td>
                    <td>
                      {% if it['line_shortage'] <= 0 and it['delivery_used'] == 0 %}
                        <span class="badge">Z magazynu</span>
                      {% elif it['line_shortage'] <= 0 %}
                        <span class="badge">CzÄ™Ĺ›Ä‡ / caĹ‚oĹ›Ä‡ z Chin</span>
                      {% else %}
                        <span class="badge">Brak towaru</span>
                      {% endif %}
                    </td>
                    <td>
                      {% if not locked %}
                        <form method="post" action="{{ url_for('order_item_delete', order_id=o['id'], item_id=it['id']) }}" onsubmit="return confirm('UsunÄ…Ä‡ pozycjÄ™?')">
                          <button class="btn danger" type="submit">UsuĹ„</button>
                        </form>
                      {% else %}
                        <span class="muted">PodglÄ…d</span>
                      {% endif %}
                    </td>
                  </tr>
                {% endfor %}
                {% if items %}
                  <tr>
                    <td colspan="5" style="text-align:right;"><b>Suma netto:</b></td>
                    <td><span class="badge"><b>{{ "%.2f"|format(ns.total_net) }} {{ o['currency'] or 'PLN' }}</b></span></td>
                    <td colspan="5"></td>
                  </tr>
                  <tr>
                    <td colspan="6" style="text-align:right;"><b>Suma brutto:</b></td>
                    <td><span class="badge"><b>{{ "%.2f"|format(ns.total_gross) }} {{ o['currency'] or 'PLN' }}</b></span></td>
                    <td colspan="4"></td>
                  </tr>
                {% else %}
                  <tr><td colspan="11" class="muted">Brak pozycji w zamĂłwieniu.</td></tr>
                {% endif %}
              </tbody>
            </table>
          </div>
        {% endblock %}
        """
        return render_template_string(tpl, title=canonical_order_no(o["id"], o["created_at"], o["order_no"]), base_url=BASE_URL, db_path=DB_PATH, o=o, items=items, order_url=order_url, products=products_rows, locked=(int(o["warehouse_issued"] or 0)==1), order_status_label=order_status_label, order_status_css=order_status_css, canonical_order_no=canonical_order_no)




    @app.post("/orders/<int:order_id>/confirmation/resend")
    def order_confirmation_resend(order_id):
        try:
            maybe_pull_shared_from_supabase(force=True)
        except Exception:
            pass
        result = _safe_saved_order_confirmation(order_id, force=True)
        if result.get("ok"):
            return redirect(url_for("order_view", order_id=order_id, confirmation_sent="1"))
        return redirect(url_for("order_view", order_id=order_id, confirmation_error=norm(result.get("error")) or "Nieznany błąd"))



    @app.post("/orders/<int:order_id>/items/add")
    def order_item_add(order_id):
        product_id = to_int(request.form.get("product_id"), 0)
        qty = to_int(request.form.get("qty"), 0)
        if product_id <= 0 or qty <= 0:
            return "NieprawidĹ‚owy produkt lub iloĹ›Ä‡", 400

        c = conn()
        cur = c.cursor()
        cur.execute("SELECT status, warehouse_issued FROM orders WHERE id=?", (order_id,))
        o = cur.fetchone()
        if not o:
            c.close()
            abort(404)
        if int(o["warehouse_issued"] or 0) == 1:
            c.close()
            return "ZamĂłwienie wydane z magazynu jest tylko do podglÄ…du", 400

        cur.execute("SELECT sku FROM products WHERE id=? AND COALESCE(archived,0)=0", (product_id,))
        p = cur.fetchone()
        if not p:
            c.close()
            return "Brak produktu", 404

        if supabase_enabled():
            created_item = supabase_insert_row("order_items", {
                "order_id": order_id,
                "product_id": product_id,
                "sku": p["sku"],
                "qty": qty,
                "created_at": now_iso(),
            })
            if not created_item or "id" not in created_item:
                c.close()
                return "Nie udaĹ‚o siÄ™ dodaÄ‡ pozycji do Supabase", 500
            cur.execute(
                "INSERT INTO order_items(id, order_id, product_id, sku, qty, created_at) VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET order_id=excluded.order_id, product_id=excluded.product_id, sku=excluded.sku, qty=excluded.qty, created_at=excluded.created_at",
                (int(created_item["id"]), order_id, product_id, p["sku"], qty, created_item.get("created_at") or now_iso())
            )
        else:
            cur.execute("""
              INSERT INTO order_items(order_id, product_id, sku, qty, created_at)
              VALUES(?,?,?,?,?)
            """, (order_id, product_id, p["sku"], qty, now_iso()))
        c.commit()
        c.close()
        return redirect(url_for("order_view", order_id=order_id))



    @app.post("/orders/<int:order_id>/items/<int:item_id>/update")
    def order_item_update(order_id, item_id):
        qty = to_int(request.form.get("qty"), 0)
        if qty <= 0:
            return "IloĹ›Ä‡ musi byÄ‡ > 0", 400
        c = conn()
        cur = c.cursor()
        cur.execute("SELECT status, warehouse_issued FROM orders WHERE id=?", (order_id,))
        o = cur.fetchone()
        if not o:
            c.close()
            abort(404)
        if int(o["warehouse_issued"] or 0) == 1:
            c.close()
            return "ZamĂłwienie wydane z magazynu jest tylko do podglÄ…du", 400
        invoiced_qty = int(invoiced_qty_by_order_item_ids([item_id]).get(int(item_id)) or 0)
        if qty < invoiced_qty:
            c.close()
            return f"Nie moĹĽesz ustawiÄ‡ iloĹ›ci poniĹĽej juĹĽ zafakturowanej ({invoiced_qty} szt.)", 400
        cur.execute("UPDATE order_items SET qty=? WHERE id=? AND order_id=?", (qty, item_id, order_id))
        c.commit()
        c.close()

        if supabase_enabled():
            supabase_update_rows("order_items", {"qty": qty}, {"id": item_id})

        return redirect(url_for("order_view", order_id=order_id))




    @app.post("/orders/<int:order_id>/items/<int:item_id>/delete")
    def order_item_delete(order_id, item_id):
        c = conn()
        cur = c.cursor()
        cur.execute("SELECT status, warehouse_issued FROM orders WHERE id=?", (order_id,))
        o = cur.fetchone()
        if not o:
            c.close()
            abort(404)
        if int(o["warehouse_issued"] or 0) == 1:
            c.close()
            return "ZamĂłwienie wydane z magazynu jest tylko do podglÄ…du", 400
        invoiced_qty = int(invoiced_qty_by_order_item_ids([item_id]).get(int(item_id)) or 0)
        if invoiced_qty > 0:
            c.close()
            return f"Nie moĹĽesz usunÄ…Ä‡ pozycji, bo jest juĹĽ zafakturowana ({invoiced_qty} szt.)", 400

        if supabase_enabled():
            supabase_delete_rows("order_items", {"id": item_id})

        cur.execute("DELETE FROM order_items WHERE id=? AND order_id=?", (item_id, order_id))
        c.commit()
        c.close()
        return redirect(url_for("order_view", order_id=order_id))




    @app.post("/orders/<int:order_id>/delete")
    def order_delete(order_id):
        c = conn()
        cur = c.cursor()
        cur.execute("SELECT id, warehouse_issued FROM orders WHERE id=?", (order_id,))
        o = cur.fetchone()
        if not o:
            c.close()
            abort(404)

        cur.execute("SELECT product_id, qty FROM order_items WHERE order_id=?", (order_id,))
        items = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT id FROM invoices WHERE order_id=?", (order_id,))
        invoice_ids = [int(r["id"]) for r in cur.fetchall()]

        changed_product_ids = []
        if int(o["warehouse_issued"] or 0) == 1:
            for it in items:
                pid = int(it["product_id"])
                qty = int(it["qty"])
                cur.execute("INSERT OR IGNORE INTO stock(product_id, qty) VALUES (?, 0)", (pid,))
                cur.execute("UPDATE stock SET qty = qty + ? WHERE product_id=?", (qty, pid))
                changed_product_ids.append(pid)

        if invoice_ids:
            cur.execute("DELETE FROM invoice_allocations WHERE invoice_id IN (" + ",".join(["?"] * len(invoice_ids)) + ")", tuple(invoice_ids))
            cur.execute("DELETE FROM invoice_meta WHERE invoice_id IN (" + ",".join(["?"] * len(invoice_ids)) + ")", tuple(invoice_ids))
        cur.execute("DELETE FROM invoice_allocations WHERE order_id=?", (order_id,))
        cur.execute("DELETE FROM invoices WHERE order_id=?", (order_id,))
        cur.execute("DELETE FROM order_items WHERE order_id=?", (order_id,))
        cur.execute("DELETE FROM orders WHERE id=?", (order_id,))
        c.commit()
        c.close()

        if supabase_enabled():
            try:
                supabase_delete_rows("invoice_allocations", {"order_id": order_id})
                for iid in invoice_ids:
                    supabase_delete_rows("invoice_allocations", {"invoice_id": iid})
                    supabase_delete_rows("invoice_meta", {"invoice_id": iid})
                    supabase_delete_rows("invoices", {"id": iid})
                supabase_delete_rows("order_items", {"order_id": order_id})
                supabase_delete_rows("orders", {"id": order_id})
                if changed_product_ids:
                    sync_local_rows_to_supabase("stock", "product_id", changed_product_ids)
            except Exception:
                pass

        return redirect(url_for("orders"))



    @app.post("/orders/<int:order_id>/status")
    def order_status_update(order_id):
        new_status = norm(request.form.get("status")).lower()
        # Status "shipped" można nadać wyłącznie osobnym formularzem,
        # który wymaga numeru przesyłki i wysyła powiadomienie do klienta.
        allowed = {
            "new", "confirmed", "packed", "packed_partial", "in_delivery",
            "shipped", "partially_shipped", "issued", "completed", "cancelled",
        }
        if new_status not in allowed:
            return "NieprawidĹ‚owy status", 400

        c = conn()
        cur = c.cursor()
        cur.execute("SELECT id, order_no, qr_data_url, status, created_at, warehouse_issued FROM orders WHERE id=?", (order_id,))
        o = cur.fetchone()
        if not o:
            c.close()
            abort(404)

        qr_data_url = (o["qr_data_url"] or "").strip()
        if new_status == "confirmed":
            qr_data_url = make_qr_data_url(canonical_order_no(o["id"], o["created_at"], o["order_no"]))

        warehouse_issued = int(o["warehouse_issued"] or 0)

        cur.execute(
            "UPDATE orders SET status=?, qr_data_url=?, warehouse_issued=? WHERE id=?",
            (new_status, qr_data_url, warehouse_issued, order_id)
        )
        c.commit()
        c.close()

        if supabase_enabled():
            try:
                supabase_update_rows(
                    "orders",
                    {"status": new_status, "qr_data_url": qr_data_url, "warehouse_issued": warehouse_issued},
                    {"id": order_id}
                )
            except Exception:
                pass


        if norm(request.form.get("return_to")).lower() == "dashboard":
            return redirect(url_for("home"))
        return redirect(url_for("order_view", order_id=order_id))




    @app.get("/orders/<int:order_id>/issue")
    def order_issue(order_id):
        # Stara ręczna akcja jest wyłączona. Stan schodzi podczas pełnego
        # zafakturowania zamówienia.
        return redirect(url_for("order_view", order_id=order_id))




    @app.get("/orders/<int:order_id>/print")
    def order_print(order_id):
        c = conn()
        cur = c.cursor()
        cur.execute("SELECT * FROM orders WHERE id=?", (order_id,))
        o = cur.fetchone()
        if not o:
            c.close()
            abort(404)

        cur.execute("""
          SELECT oi.sku, oi.qty, p.model, p.name, COALESCE(s.qty,0) AS stock
          FROM order_items oi
          JOIN products p ON p.id=oi.product_id
          LEFT JOIN stock s ON s.product_id=p.id
          WHERE oi.order_id=?
          ORDER BY oi.id
        """, (order_id,))
        items = cur.fetchall()
        c.close()

        in_stock = []
        missing = []
        total_qty = 0
        total_missing_qty = 0
        for it in items:
            need = int(it["qty"])
            have = int(it["stock"])
            row = {
                "sku": it["sku"],
                "model": it["model"] or "",
                "name": it["name"] or "",
                "qty": need,
                "stock": have,
                "missing": max(0, need - have),
            }
            total_qty += need
            total_missing_qty += row["missing"]
            if have >= need:
                in_stock.append(row)
            else:
                missing.append(row)

        buf = io.BytesIO()
        w = 210 * mm
        h = 297 * mm
        cpdf = canvas.Canvas(buf, pagesize=(w, h))
        pdf_font, pdf_font_bold = get_pdf_font_names()

        y = h - 18 * mm
        cpdf.setFont(pdf_font_bold, 14)
        cpdf.drawString(15 * mm, y, f"Wydruk zamĂłwienia: {order_display_no(o['id'], o['created_at'], o['order_no'], o['note'])}")
        y -= 7 * mm
        cpdf.setFont(pdf_font, 10)
        cpdf.drawString(15 * mm, y, f"Klient: {o['customer_name']}")
        y -= 5 * mm
        cpdf.drawString(15 * mm, y, f"Data: {o['created_at']}")
        y -= 6 * mm
        cpdf.setFont(pdf_font_bold, 10)
        cpdf.drawString(15 * mm, y, f"ĹÄ…czna liczba sztuk w zamĂłwieniu: {total_qty}")
        y -= 5 * mm
        cpdf.setFont(pdf_font, 10)
        cpdf.drawString(15 * mm, y, f"ĹÄ…czny brak na stanie: {total_missing_qty}")

        def draw_section(title, rows, y_pos, show_missing=False):
            cpdf.setFont(pdf_font_bold, 11)
            cpdf.drawString(15 * mm, y_pos, title)
            y_pos -= 6 * mm
            cpdf.setFont(pdf_font_bold, 9)
            cpdf.drawString(15 * mm, y_pos, "SKU")
            cpdf.drawString(55 * mm, y_pos, "Model/Nazwa")
            cpdf.drawString(160 * mm, y_pos, "IloĹ›Ä‡")
            if show_missing:
                cpdf.drawString(176 * mm, y_pos, "Brak")
            y_pos -= 5 * mm
            cpdf.setFont(pdf_font, 9)
            for r in rows:
                label = (r['model'] or r['name'] or "")[:48]
                cpdf.drawString(15 * mm, y_pos, r['sku'])
                cpdf.drawString(55 * mm, y_pos, label)
                cpdf.drawRightString(173 * mm, y_pos, str(r['qty']))
                if show_missing:
                    cpdf.drawRightString(195 * mm, y_pos, str(r['missing']))
                y_pos -= 5 * mm
                if y_pos < 20 * mm:
                    cpdf.showPage()
                    y_pos = h - 20 * mm
                    cpdf.setFont(pdf_font, 9)
            return y_pos

        y -= 10 * mm
        y = draw_section("Produkty w magazynie", in_stock, y, show_missing=False)
        y -= 6 * mm
        y = draw_section("Brak na stanie", missing, y, show_missing=True)

        cpdf.showPage()
        cpdf.save()
        buf.seek(0)
        fname = safe_filename(canonical_order_no(o["id"], o["created_at"], o["order_no"])) + "_druk_zamowienia.pdf"
        return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=fname)




    @app.get("/orders/<int:order_id>/label")
    def order_label(order_id):
        c = conn()
        cur = c.cursor()
        cur.execute("SELECT * FROM orders WHERE id=?", (order_id,))
        o = cur.fetchone()
        c.close()
        if not o:
            abort(404)

        # Etykieta 30x50 ma zawsze uĹĽywaÄ‡ aktualnego QR z poprawnym numerem ZAM-...
        # i nadpisywaÄ‡ stare QR-y wygenerowane kiedy zamĂłwienie miaĹ‚o jeszcze TEMP.
        qr_data_url = make_qr_data_url(canonical_order_no(o["id"], o["created_at"], o["order_no"]))

        c = conn()
        cur = c.cursor()
        cur.execute("UPDATE orders SET qr_data_url=? WHERE id=?", (qr_data_url, order_id))
        c.commit()
        c.close()
        if supabase_enabled():
            try:
                supabase_update_rows("orders", {"qr_data_url": qr_data_url}, {"id": order_id})
            except Exception:
                pass

        # PDF 30x50 mm
        w = 30 * mm
        h = 50 * mm

        buf = io.BytesIO()
        cpdf = canvas.Canvas(buf, pagesize=(w, h))

        # Umieszczenie QR
        qr_bytes = b""
        if qr_data_url.startswith("data:image"):
            try:
                qr_bytes = base64.b64decode(qr_data_url.split(",", 1)[1])
            except Exception:
                qr_bytes = b""

        if not qr_bytes:
            fallback_qr = make_qr_data_url(canonical_order_no(o["id"], o["created_at"], o["order_no"]))
            if fallback_qr.startswith("data:image"):
                qr_bytes = base64.b64decode(fallback_qr.split(",", 1)[1])

        qr_buf = io.BytesIO(qr_bytes)
        qr_img = ImageReader(qr_buf)

        # QR na gĂłrze (wiÄ™kszy), dane poniĹĽej
        margin = 2 * mm
        qr_size = 26 * mm  # zostaje margines
        cpdf.drawImage(qr_img, margin, h - margin - qr_size, width=qr_size, height=qr_size, preserveAspectRatio=True, mask='auto')

        # Dane zamawiajÄ…cego + nr zamĂłwienia
        pdf_font, pdf_font_bold = get_pdf_font_names()
        text_y = h - margin - qr_size - 2*mm
        max_text_width = w - (2 * margin)

        def wrap_pdf_text(value, font_name, font_size, max_width):
            words = str(value or "").split()
            if not words:
                return []
            lines = []
            current = words[0]
            for word in words[1:]:
                test = current + " " + word
                if pdfmetrics.stringWidth(test, font_name, font_size) <= max_width:
                    current = test
                else:
                    lines.append(current)
                    current = word
            lines.append(current)
            return lines

        customer_lines = wrap_pdf_text((o["customer_name"] or "")[:60], pdf_font_bold, 6.2, max_text_width)
        if not customer_lines:
            customer_lines = [""]

        cpdf.setFont(pdf_font_bold, 6.2)
        cpdf.drawString(margin, text_y, customer_lines[0][:60])

        order_no_value = order_display_no(o['id'], o['created_at'], o['order_no'], o['note'])
        order_no_lines = [f"Nr: {order_no_value}"]

        if pdfmetrics.stringWidth(order_no_lines[0], pdf_font_bold, 5.1) > max_text_width:
            order_no_lines = [f"Nr: {order_no_value[:13]}", order_no_value[13:]]

        cpdf.setFont(pdf_font_bold, 5.1)
        cpdf.drawString(margin, text_y - 3.2*mm, order_no_lines[0])
        extra_offset_mm = 0
        if len(order_no_lines) > 1 and order_no_lines[1].strip():
            cpdf.drawString(margin, text_y - 6.0*mm, order_no_lines[1].strip())
            extra_offset_mm = 2.8

        cpdf.setFont(pdf_font, 6.0)
        addr = (o["customer_address"] or "").strip()
        phone = (o["customer_phone"] or "").strip()

        lines = []
        if addr:
            # podziel na linie i dodatkowo Ĺ‚am dĹ‚ugie
            for ln in addr.splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                while len(ln) > 42:
                    lines.append(ln[:42])
                    ln = ln[42:]
                lines.append(ln)
        if phone:
            lines.append(f"Tel: {phone}")

        y = text_y - (6.8 + extra_offset_mm)*mm
        for ln in lines[:6]:
            cpdf.drawString(margin, y, ln)
            y -= 3.2*mm

        cpdf.showPage()
        cpdf.save()
        buf.seek(0)

        fname = safe_filename(canonical_order_no(o["id"], o["created_at"], o["order_no"])) + "_label_30x50.pdf"
        return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=fname)




    @app.route("/api/client/orders", methods=["POST", "OPTIONS"])
    def api_client_orders_create():
        """Create the complete order and send its email in one backend request."""
        if not _client_order_origin_allowed():
            return jsonify(ok=False, error="Niedozwolone źródło żądania"), 403
        if request.method == "OPTIONS":
            return ("", 204)

        if not supabase_enabled():
            app.logger.error("Odrzucono zamówienie klienta: brak konfiguracji Supabase")
            return jsonify(ok=False, error="Brak konfiguracji połączenia z Supabase"), 503

        data = request.get_json(silent=True) or {}
        client_user = _authenticated_client_user()
        if not client_user:
            app.logger.warning("Odrzucono zamówienie klienta: brak lub nieważny token")
            return jsonify(ok=False, error="Brak autoryzacji"), 401

        customer_email = client_user["email"]
        customer_name = client_user["name"]
        note = norm(data.get("note"))
        idempotency_key = norm(request.headers.get("Idempotency-Key"))
        try:
            uuid.UUID(idempotency_key)
        except Exception:
            return jsonify(ok=False, error="Brak lub niepoprawny Idempotency-Key"), 400

        try:
            maybe_pull_shared_from_supabase(force=True)
        except Exception as exc:
            app.logger.error("Nie udało się odświeżyć danych Supabase przed zamówieniem: %s", exc)
            return jsonify(ok=False, error="Nie udało się odświeżyć danych produktów"), 503

        existing = _order_by_idempotency_key(idempotency_key)
        if existing:
            if norm(existing.get("customer_email")).lower() != customer_email:
                return jsonify(ok=False, error="Konflikt Idempotency-Key"), 409
            app.logger.info("Ponowiono request user_id=%s order_id=%s idempotency_key=%s", client_user["id"], existing["id"], idempotency_key)
            return jsonify(ok=True, duplicate=True, order={"id": existing["id"], "order_no": existing["order_no"]}, email=_safe_saved_order_confirmation(int(existing["id"])))

        raw_items = data.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            return jsonify(ok=False, error="Zamówienie nie zawiera pozycji"), 400
        if len(raw_items) > 100:
            return jsonify(ok=False, error="Zamówienie może zawierać maksymalnie 100 pozycji"), 400

        try:
            profile = _client_profile_for_email(customer_email)
            catalog_rows = _client_stock_catalog_rows(profile)
        except Exception as exc:
            app.logger.error("Nie udało się ustalić cennika klienta przed zamówieniem: %s", type(exc).__name__)
            return jsonify(ok=False, error="Nie udało się pobrać aktualnego cennika klienta"), 503
        customer_name = norm(profile.get("name")) or customer_name
        catalog_by_product_id = {to_int(row.get("product_id"), 0): row for row in catalog_rows}

        items = []
        c = conn()
        try:
            cur = c.cursor()
            for item in raw_items:
                if not isinstance(item, dict):
                    return jsonify(ok=False, error="Nieprawidłowa pozycja zamówienia"), 400
                product_id = item.get("product_id")
                qty = item.get("qty")
                if isinstance(product_id, bool) or not isinstance(product_id, int) or product_id <= 0:
                    return jsonify(ok=False, error="Nieprawidłowy identyfikator produktu"), 400
                if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
                    label = norm(item.get("sku")) or str(product_id)
                    return jsonify(ok=False, error=f"Nieprawidłowa ilość dla produktu {label}"), 400
                cur.execute("SELECT id, sku, name FROM products WHERE id=? AND COALESCE(archived,0)=0 LIMIT 1", (product_id,))
                product = cur.fetchone()
                if not product:
                    return jsonify(ok=False, error=f"Nie istnieje produkt ID {product_id}"), 400
                submitted_sku = norm(item.get("sku"))
                if submitted_sku and submitted_sku.lower() != norm(product["sku"]).lower():
                    return jsonify(ok=False, error=f"Produkt ID {product_id} nie odpowiada SKU {submitted_sku}"), 400
                catalog_row = catalog_by_product_id.get(product_id) or {}
                if not catalog_row.get("price_available"):
                    return jsonify(ok=False, error=f"Brak ceny w cenniku klienta dla SKU {product['sku']}"), 400
                items.append({
                    "product_id": product_id,
                    "qty": qty,
                    "unit_net_price": money_float(catalog_row.get("net_price")),
                    "unit_gross_price": money_float(catalog_row.get("gross_price")),
                    "unit_retail_price": money_float(catalog_row.get("retail_price")),
                    "currency": normalize_order_currency(catalog_row.get("currency")),
                })
        finally:
            c.close()

        try:
            order_id = remote_first_create_order(
                profile.get("id"),
                customer_name,
                norm(profile.get("address")),
                norm(profile.get("phone")),
                customer_email,
                note,
                items,
                idempotency_key=idempotency_key,
                price_list=profile.get("price_list", "pln"),
                currency=profile.get("currency", "PLN"),
            )
            email_result = _safe_saved_order_confirmation(order_id)
            c = conn()
            try:
                cur = c.cursor()
                cur.execute("SELECT order_no FROM orders WHERE id=?", (order_id,))
                row = cur.fetchone()
                order_no = row["order_no"] if row else make_order_no(order_id, now_iso())
            finally:
                c.close()
            app.logger.info("Utworzono zamówienie user_id=%s order_id=%s order_no=%s items=%s email_ok=%s", client_user["id"], order_id, order_no, len(items), bool(email_result.get("ok")))
            return jsonify(ok=True, order={"id": order_id, "order_no": order_no}, email=email_result)
        except Exception as exc:
            # remote_first_create_order może zdążyć zapisać rekord w Supabase, a
            # następnie utracić połączenie podczas lokalnego odświeżenia. Pobieramy
            # stan ponownie i rozpoznajemy zamówienie po niezmiennym kluczu, zamiast
            # zwracać klientowi fałszywy błąd 500.
            try:
                maybe_pull_shared_from_supabase(force=True)
            except Exception:
                pass
            existing = _order_by_idempotency_key(idempotency_key)
            if existing:
                if norm(existing.get("customer_email")).lower() != customer_email:
                    return jsonify(ok=False, error="Konflikt Idempotency-Key"), 409
                app.logger.info("Konflikt idempotencji user_id=%s order_id=%s", client_user["id"], existing["id"])
                return jsonify(ok=True, duplicate=True, recovered=True, order={"id": existing["id"], "order_no": existing["order_no"]}, email=_safe_saved_order_confirmation(int(existing["id"])))
            app.logger.exception("Błąd tworzenia zamówienia user_id=%s items=%s", client_user["id"], len(items))
            return jsonify(ok=False, error=str(exc)), 500




    @app.route("/api/client_order_email", methods=["POST", "OPTIONS"])
    def api_client_order_email():
        if not _client_order_origin_allowed():
            return jsonify(ok=False, error="Niedozwolone źródło żądania"), 403
        if request.method == "OPTIONS":
            return ("", 204)

        return jsonify(
            ok=False,
            error="Ten endpoint został wyłączony. Zamówienie i potwierdzenie obsługuje /api/client/orders.",
        ), 410

        app.logger.warning("Użyto przestarzałego endpointu /api/client_order_email; zaktualizuj panel do /api/client/orders")

        data = request.get_json(silent=True) or {}
        order_id = to_int(data.get("order_id"), 0)
        order_no = norm(data.get("order_no"))
        fallback_email = norm(data.get("customer_email") or data.get("email")).lower()
        fallback_name = norm(data.get("customer_name")) or (fallback_email.split("@")[0] if fallback_email else "")
        fallback_note = norm(data.get("note"))
        fallback_items = data.get("items") if isinstance(data.get("items"), list) else []

        order = {
            "id": order_id,
            "order_no": order_no,
            "customer_email": fallback_email,
            "customer_name": fallback_name,
            "note": fallback_note,
            "created_at": now_iso(),
        }
        items = []

        try:
            maybe_pull_shared_from_supabase(force=True)
        except Exception:
            pass

        c = conn()
        cur = c.cursor()
        try:
            if order_id:
                cur.execute("SELECT * FROM orders WHERE id=? LIMIT 1", (order_id,))
            elif order_no:
                cur.execute("SELECT * FROM orders WHERE order_no=? LIMIT 1", (order_no,))
            else:
                cur.execute("SELECT * FROM orders WHERE 1=0")
            row = cur.fetchone()
            if row:
                db_order = dict(row)
                # Panel klienta jest źródłem prawdy dla adresu odbiorcy maila.
                # Lokalna baza na Renderze może mieć starszy rekord po synchronizacji,
                # więc nie wolno blokować podmiany, jeśli email już istnieje.
                if fallback_email:
                    db_order["customer_email"] = fallback_email
                if fallback_name:
                    db_order["customer_name"] = fallback_name
                if fallback_note and not norm(db_order.get("note")):
                    db_order["note"] = fallback_note
                order = db_order
                cur.execute("""
                  SELECT oi.sku, oi.qty, COALESCE(p.name, pr.name, '') AS name
                  FROM order_items oi
                  LEFT JOIN products p ON p.id = oi.product_id
                  LEFT JOIN products pr ON pr.sku = oi.sku
                  WHERE oi.order_id=?
                  ORDER BY oi.id
                """, (row["id"],))
                items = [dict(x) for x in cur.fetchall()]
        except Exception:
            items = []
        finally:
            c.close()

        if not items:
            for item in fallback_items[:80]:
                if not isinstance(item, dict):
                    continue
                items.append({
                    "sku": norm(item.get("sku")),
                    "name": norm(item.get("name")),
                    "qty": to_int(item.get("qty"), 0),
                })

        if fallback_email:
            order["customer_email"] = fallback_email
        if fallback_name:
            order["customer_name"] = fallback_name

        event_ref = norm(order.get("id")) or norm(order_id) or norm(order.get("order_no")) or order_no
        try:
            admin_email = norm(email_config_summary().get("admin_email"))
        except Exception:
            admin_email = ""
        recipient = ", ".join([x for x in [norm(order.get("customer_email")), admin_email] if x])
        recipient_hash = hashlib.sha1(recipient.lower().encode("utf-8")).hexdigest()[:12] if recipient else "no-recipient"
        event_key = f"order_confirmation:{event_ref}:{recipient_hash}" if event_ref else ""

        if _email_event_already_ok(event_key):
            return jsonify(ok=True, email={"ok": True, "duplicate": True, "skipped": True, "to": recipient, "order_email": norm(order.get("customer_email"))})

        if not send_order_confirmation:
            result = {"ok": False, "skipped": True, "error": "Brak modułu email_module.py"}
            _record_email_event(event_key, "order_confirmation", event_ref, recipient, result)
            return jsonify(ok=True, email=result)

        try:
            result = send_order_confirmation(order, items, admin_email=admin_email)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        _record_email_event(event_key, "order_confirmation", event_ref, recipient, result)
        return jsonify(ok=True, email=result, to=recipient, order_email=norm(order.get("customer_email")))




    @app.get("/api/order_lookup")
    def api_order_lookup():
        try:
            return _api_order_lookup_impl()
        except Exception as exc:
            app.logger.exception("Błąd szczegółów zamówienia klienta: %s", exc)
            return jsonify(ok=False, error="Nie udało się pobrać szczegółów zamówienia"), 500




    @app.get("/api/client/orders/<int:order_id>/pdf")
    def api_client_order_pdf(order_id: int):
        try:
            return _api_client_order_pdf_impl(order_id)
        except Exception as exc:
            app.logger.exception("Błąd PDF zamówienia klienta order_id=%s: %s", order_id, exc)
            return jsonify(ok=False, error="Nie udało się przygotować PDF zamówienia"), 500




    @app.get("/api/client/orders/<int:order_id>/pdf-retail")
    def api_client_order_pdf_retail(order_id: int):
        try:
            return _api_client_order_pdf_impl(order_id, retail_prices=True)
        except Exception as exc:
            app.logger.exception("Błąd PDF detal zamówienia klienta order_id=%s: %s", order_id, exc)
            return jsonify(ok=False, error="Nie udało się przygotować PDF detal zamówienia"), 500




    @app.get("/orders/<int:order_id>/proforma")
    def order_proforma(order_id: int):
        """Generate a foreign-customer proforma without creating a Polish invoice/KSeF record."""
        maybe_pull_shared_from_supabase()
        c = conn()
        try:
            cur = c.cursor()
            cur.execute("SELECT * FROM orders WHERE id=? LIMIT 1", (order_id,))
            row = cur.fetchone()
            if not row:
                return "Nie znaleziono zamówienia", 404
            order = dict(row)
            if normalize_order_currency(order.get("currency")) != "EUR":
                return "Proforma EUR jest dostępna wyłącznie dla zamówień zagranicznych", 400
            items = _client_order_items_local(c, order)
            company_row = cur.execute("SELECT * FROM company_profile WHERE id=1").fetchone()
            company = dict(company_row) if company_row else {}
        finally:
            c.close()
        try:
            profile = _client_profile_for_email(order.get("customer_email"))
            language = normalize_client_language(profile.get("language"))
            order["customer_tax_no"] = profile.get("nip") or ""
            order["customer_name"] = profile.get("name") or order.get("customer_name")
            order["customer_address"] = profile.get("address") or order.get("customer_address")
            order["customer_phone"] = profile.get("phone") or order.get("customer_phone")
        except Exception:
            language = "en"
        company["pdf_font"], company["pdf_font_bold"] = get_pdf_font_names()
        pdf_buffer, filename = generate_proforma_pdf(
            order,
            items,
            company,
            language=language,
            logo_path=find_logo_path(),
            iban=norm(os.environ.get("PROFORMA_EUR_IBAN") or company.get("bank_account")),
            bic=norm(os.environ.get("PROFORMA_EUR_BIC")),
            bank_name=norm(os.environ.get("PROFORMA_EUR_BANK")),
            place=norm(os.environ.get("PROFORMA_PLACE") or "Kotuszów"),
        )
        return send_file(pdf_buffer, mimetype="application/pdf", as_attachment=True, download_name=filename, max_age=0)




    @app.get("/orders/by-code/<path:token>")
    def order_by_code(token):
        maybe_pull_shared_from_supabase()
        c = conn()
        cur = c.cursor()
        cur.execute("SELECT id, order_no, created_at FROM orders WHERE order_no=? LIMIT 1", (norm(token),))
        row = cur.fetchone()
        if not row:
            cur.execute("SELECT id, order_no, created_at FROM orders ORDER BY id DESC")
            all_rows = cur.fetchall()
            for r in all_rows:
                if canonical_order_no(r["id"], r["created_at"], r["order_no"]) == norm(token):
                    row = r
                    break
        c.close()
        if not row:
            return "Nie znaleziono zamĂłwienia", 404
        return redirect(url_for("order_view", order_id=row["id"]))




    @app.get("/orders/scan")
    def order_scan():
        tpl = r"""
        {% extends "base.html" %}
        {% block content %}
          <style>
            #qrScannerModal.hidden{display:none}
          </style>

          <div class="card">
            <h1 id="openQrScanner" style="cursor:pointer;text-decoration:underline;">Skanuj QR zamĂłwienia</h1>
            <div>
              <input id="orderCodeInput" placeholder="Wklej kod zamĂłwienia ZAM-... albo kliknij â€žSkanuj QR zamĂłwieniaâ€ť" />
              <button id="btnShowOrderByCode" class="btn primary" type="button" style="margin-top:8px">PokaĹĽ zamĂłwienie</button>
            </div>
            <div id="orderScanOut" class="muted" style="margin-top:10px"></div>
          </div>

          <div id="qrScannerModal" class="hidden" style="position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:9999;padding:16px;box-sizing:border-box;">
            <div style="max-width:420px;margin:4vh auto;background:#fff;border-radius:16px;padding:12px;">
              <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px;">
                <b>Skanuj QR</b>
                <button id="closeQrScanner" class="btn" type="button">Zamknij</button>
              </div>
              <div class="muted" style="margin-bottom:8px;">Uruchamia siÄ™ tylko tylny aparat.</div>
              <div id="orderQrReader" style="width:100%;min-height:280px;"></div>
            </div>
          </div>

          <script src="https://unpkg.com/html5-qrcode" type="text/javascript"></script>
          <script>
            const $ = (id)=>document.getElementById(id);

            function parseOrderCode(value){
              const raw = String(value || '').trim();
              if(!raw) return '';

              const match = raw.match(/ZAM-[0-9]{6}[0-9]+|ZAM-[0-9]{8}-[A-Z0-9-]+/i);
              if(match) return match[0].toUpperCase();

              try {
                const url = new URL(raw);
                const fromPath = url.pathname.match(/ZAM-[0-9]{6}[0-9]+|ZAM-[0-9]{8}-[A-Z0-9-]+/i);
                if(fromPath) return fromPath[0].toUpperCase();
              } catch(e) {}

              return raw.toUpperCase();
            }

            function showOrderByCode(rawCode){
              const code = parseOrderCode(rawCode || $('orderCodeInput').value || '');
              if(!code){
                $('orderScanOut').innerHTML = '<div class="muted">Wpisz albo zeskanuj kod ZAM-...</div>';
                return;
              }
              window.location.href = '/orders/by-code/' + encodeURIComponent(code);
            }

            $('btnShowOrderByCode').onclick = () => showOrderByCode();

            let orderQrScanner = null;
            let orderQrScannerRunning = false;

            async function startOrderQrScanner(){
              if(!window.Html5Qrcode) return;
              if(!orderQrScanner){
                orderQrScanner = new Html5Qrcode('orderQrReader');
              }
              if(orderQrScannerRunning) return;

              const onSuccess = async (decodedText) => {
                const parsedCode = parseOrderCode(decodedText);
                $('orderCodeInput').value = parsedCode || decodedText;
                await stopOrderQrScanner();
                $('qrScannerModal').classList.add('hidden');
                showOrderByCode(parsedCode || decodedText);
              };

              try {
                await orderQrScanner.start(
                  { facingMode: { exact: 'environment' } },
                  { fps: 10, qrbox: 220, aspectRatio: 1.0 },
                  onSuccess,
                  () => {}
                );
                orderQrScannerRunning = true;
              } catch (e1) {
                try {
                  await orderQrScanner.start(
                    { facingMode: 'environment' },
                    { fps: 10, qrbox: 220, aspectRatio: 1.0 },
                    onSuccess,
                    () => {}
                  );
                  orderQrScannerRunning = true;
                } catch (e2) {
                  $('orderScanOut').innerHTML = '<div class="muted">Nie udaĹ‚o siÄ™ uruchomiÄ‡ tylnego aparatu.</div>';
                  $('qrScannerModal').classList.add('hidden');
                }
              }
            }

            async function stopOrderQrScanner(){
              try {
                if(orderQrScanner && orderQrScannerRunning){
                  await orderQrScanner.stop();
                  await orderQrScanner.clear();
                }
              } catch (e) {}
              orderQrScannerRunning = false;
            }

            $('openQrScanner').onclick = async () => {
              $('qrScannerModal').classList.remove('hidden');
              await startOrderQrScanner();
            };

            $('closeQrScanner').onclick = async () => {
              await stopOrderQrScanner();
              $('qrScannerModal').classList.add('hidden');
            };

            $('qrScannerModal').onclick = async (e) => {
              if(e.target && e.target.id === 'qrScannerModal'){
                await stopOrderQrScanner();
                $('qrScannerModal').classList.add('hidden');
              }
            };

            window.addEventListener('beforeunload', () => {
              stopOrderQrScanner();
            });
          </script>
        {% endblock %}
        """
        return render_template_string(tpl, title="Skan QR", base_url=BASE_URL, db_path=DB_PATH)




    exported = {'stock_issue_audit': stock_issue_audit, 'stock_issue_repair': stock_issue_repair, 'orders': orders, 'order_new': order_new, 'order_create': order_create, 'order_view': order_view, 'order_confirmation_resend': order_confirmation_resend, 'order_item_add': order_item_add, 'order_item_update': order_item_update, 'order_item_delete': order_item_delete, 'order_delete': order_delete, 'order_status_update': order_status_update, 'order_issue': order_issue, 'order_print': order_print, 'order_label': order_label, 'api_client_orders_create': api_client_orders_create, 'api_client_order_email': api_client_order_email, 'api_order_lookup': api_order_lookup, 'api_client_order_pdf': api_client_order_pdf, 'api_client_order_pdf_retail': api_client_order_pdf_retail, 'order_proforma': order_proforma, 'order_by_code': order_by_code, 'order_scan': order_scan}
    globals().update(exported)
    return exported
