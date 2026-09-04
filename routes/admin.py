"""Mechanically extracted Flask routes; business logic is unchanged."""

def register_routes(context):
    globals().update(context)


    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = ""
        if request.method == "POST":
            if not app.secret_key or not (ADMIN_PASSWORD_HASH or ADMIN_PASSWORD):
                error = "Brak konfiguracji logowania administratora na serwerze."
            elif hmac.compare_digest(norm(request.form.get("username")), ADMIN_USERNAME) and _admin_password_ok(request.form.get("password") or ""):
                session.clear()
                session["admin_authenticated"] = True
                session["csrf_token"] = secrets.token_urlsafe(32)
                session.permanent = True
                target = norm(request.args.get("next"))
                return redirect(target if target.startswith("/") and not target.startswith("//") else url_for("home"))
            else:
                error = "Nieprawidłowy login lub hasło."
        return render_template_string(r'''<!doctype html><html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Logowanie — Niedźwieccy</title><style>body{margin:0;font-family:Inter,Segoe UI,sans-serif;background:#f5f6fa;color:#17233c;display:grid;place-items:center;min-height:100vh}.box{width:min(420px,calc(100% - 28px));background:#fff;padding:30px;border-radius:24px;box-shadow:0 18px 55px rgba(20,35,65,.13)}h1{margin:0 0 5px}.muted{color:#718096;font-size:13px;margin-bottom:22px}label{display:block;font-size:12px;font-weight:700;margin:12px 0 6px}input{width:100%;box-sizing:border-box;padding:12px;border:1px solid #dfe3ec;border-radius:13px;font:inherit}button{width:100%;margin-top:18px;padding:12px;border:0;border-radius:13px;background:#5577ee;color:#fff;font-weight:700}.error{background:#fff1f2;color:#b9384c;padding:10px;border-radius:12px;font-size:12px}</style></head><body><form class="box" method="post"><h1>Panel magazynu</h1><div class="muted">Zaloguj się jako administrator.</div>{% if error %}<div class="error">{{ error }}</div>{% endif %}<label>Login</label><input name="username" autocomplete="username" required><label>Hasło</label><input name="password" type="password" autocomplete="current-password" required><button type="submit">Zaloguj</button></form></body></html>''', error=error)




    @app.get("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))




    @app.get("/")
    def home():
        maybe_pull_shared_from_supabase()
        # Historyczna reconciliacja działa po synchronizacji w tle, poza
        # krytyczną ścieżką renderowania pulpitu.
        c = conn()
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM products WHERE COALESCE(archived,0)=0")
        n_products = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM orders WHERE status IN ('new','packed','packed_partial','confirmed','in_delivery','shipped','partially_shipped')")
        n_orders_current = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM china_packages WHERE status IN ('planned','ordered','shipped')")
        n_china_active = cur.fetchone()["n"]
        cur.execute("SELECT COALESCE(SUM(qty),0) AS n FROM stock")
        n_stock_qty = cur.fetchone()["n"]
        cur.execute("""
          SELECT COALESCE(SUM(ci.qty),0) AS n
          FROM china_items ci
          JOIN china_packages cp ON cp.id=ci.package_id
          WHERE cp.status IN ('ordered', 'shipped')
        """)
        n_in_delivery_qty = cur.fetchone()["n"]

        cur.execute("""
          SELECT COALESCE(SUM(
            (COALESCE(s.qty,0) + COALESCE(d.in_delivery_qty,0)) * COALESCE(
            (
              SELECT pr.net_price
              FROM pricing pr
              WHERE TRIM(LOWER(pr.model)) = TRIM(LOWER(p.sku))
              ORDER BY pr.created_at DESC
              LIMIT 1
            ),
            (
              SELECT pr.net_price
              FROM pricing pr
              WHERE TRIM(LOWER(pr.model)) = TRIM(LOWER(p.model))
              ORDER BY pr.created_at DESC
              LIMIT 1
            ), 0)
          ), 0) AS v
          FROM products p
          LEFT JOIN stock s ON s.product_id=p.id
          LEFT JOIN (
            SELECT ci.product_id, SUM(ci.qty) AS in_delivery_qty
            FROM china_items ci
            JOIN china_packages cp ON cp.id=ci.package_id
            WHERE cp.status IN ('ordered', 'shipped')
            GROUP BY ci.product_id
          ) d ON d.product_id=p.id
          WHERE COALESCE(p.archived,0)=0
        """)

        inventory_value_net = float(cur.fetchone()["v"] or 0)
        cur.execute("SELECT COUNT(*) AS n FROM orders WHERE date(created_at)=date('now','localtime')")
        n_orders_today = int(cur.fetchone()["n"] or 0)
        # "Wydane dzisiaj" ma opisywac dzien faktycznego wydania przez
        # fakturowanie, a nie dzien utworzenia zamowienia. Jedna faktura moze
        # zawierac wiele pozycji (a nawet kilka zamowien), dlatego liczymy
        # unikalne zamowienia na podstawie zapisanych alokacji faktury.
        # Druga czesc UNION jest zgodnosciowym fallbackiem dla starszych faktur,
        # ktore powstaly przed wprowadzeniem invoice_allocations.
        cur.execute("""
          SELECT COUNT(DISTINCT issued.order_id) AS n
          FROM (
            SELECT ia.order_id, ia.created_at AS issued_at
            FROM invoice_allocations ia

            UNION ALL

            SELECT i.order_id, i.created_at AS issued_at
            FROM invoices i
            WHERE i.order_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM invoice_allocations ia2 WHERE ia2.invoice_id=i.id
              )
          ) issued
          JOIN orders o ON o.id=issued.order_id
          WHERE COALESCE(o.warehouse_issued,0)=1
            AND date(issued.issued_at)=date('now','localtime')
        """)
        n_issued_today = int(cur.fetchone()["n"] or 0)
        # Zamowienia, ktore mozna wydac z obecnego stanu. Stan jest rezerwowany
        # od najstarszego zamowienia, aby ta sama sztuka nie byla liczona dwa razy.
        issuable_statuses = {"new", "pending", "unconfirmed", "confirmed", "packed", "packed_partial"}
        status_ph = ",".join(["?"] * len(issuable_statuses))
        cur.execute(f"""
          SELECT o.id, o.order_no, o.created_at, o.note, oi.product_id,
                 SUM(oi.qty) AS required_qty
          FROM orders o
          JOIN order_items oi ON oi.order_id=o.id
          WHERE LOWER(COALESCE(o.status,'')) IN ({status_ph})
            AND COALESCE(o.warehouse_issued,0)=0
          GROUP BY o.id, oi.product_id
          ORDER BY o.created_at, o.id, oi.product_id
        """, tuple(sorted(issuable_statuses)))
        issue_rows = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT product_id, MAX(0, COALESCE(qty,0)) AS qty FROM stock")
        issue_stock_pool = {int(r["product_id"]): int(r["qty"] or 0) for r in cur.fetchall()}
        issue_orders = {}
        for row in issue_rows:
            order_id = int(row["id"])
            issue_orders.setdefault(order_id, {"order": row, "needs": []})["needs"].append(
                (int(row["product_id"]), int(row["required_qty"] or 0))
            )
        issuable_orders = []
        for candidate in issue_orders.values():
            if candidate["needs"] and all(issue_stock_pool.get(pid, 0) >= qty for pid, qty in candidate["needs"]):
                issuable_orders.append(candidate["order"])
                for pid, qty in candidate["needs"]:
                    issue_stock_pool[pid] = issue_stock_pool.get(pid, 0) - qty
        n_issuable_today = len(issuable_orders)
        issuable_order_labels = [
            order_display_no(r["id"], r.get("created_at"), r.get("order_no"), r.get("note") or "")
            for r in issuable_orders[:3]
        ]
        reorder_horizon_days = 60
        try:
            cur.execute("SELECT value FROM cash_flow_settings WHERE key='reorder_horizon_days'")
            horizon_row = cur.fetchone()
            reorder_horizon_days = int(float(horizon_row["value"])) if horizon_row else 60
        except Exception:
            reorder_horizon_days = 60
        if reorder_horizon_days not in (45, 60, 90):
            reorder_horizon_days = 60
        cur.execute("""
          SELECT o.id,o.order_no,o.customer_name,o.created_at,o.status,o.currency,
                 COALESCE(SUM(oi.qty * COALESCE(oi.unit_net_price,pr.net_price,0)),0) AS total_net
          FROM orders o
          LEFT JOIN order_items oi ON oi.order_id=o.id
          LEFT JOIN products p ON p.id=oi.product_id
          LEFT JOIN pricing pr ON (TRIM(LOWER(pr.model))=TRIM(LOWER(p.model)) OR TRIM(LOWER(pr.model))=TRIM(LOWER(p.sku)))
          GROUP BY o.id ORDER BY o.id DESC LIMIT 8
        """)
        recent_orders = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT status,COUNT(*) AS n FROM orders GROUP BY status")
        status_counts = {norm(r["status"]).lower(): int(r["n"] or 0) for r in cur.fetchall()}
        status_new = sum(status_counts.get(x,0) for x in ("new","pending","unconfirmed"))
        status_work = sum(status_counts.get(x,0) for x in ("confirmed","packed","packed_partial","in_delivery","shipped","partially_shipped","issued"))
        status_done = status_counts.get("completed",0)
        status_cancelled = status_counts.get("cancelled",0)
        status_total = status_new + status_work + status_done + status_cancelled
        status_divisor = max(1, status_total)
        overdue_invoices = overdue_invoice_rows(c)
        overdue_count = len(overdue_invoices)
        overdue_total = sum(float(inv.get("total_gross") or 0) for inv in overdue_invoices)
        c.close()

        replenishment_analysis = build_replenishment_analysis(
            conn, today=app_now().date(), horizon_days=reorder_horizon_days
        )
        all_replenishment_rows = recommended_replenishments(
            replenishment_analysis, limit=max(10, len(replenishment_analysis))
        )
        replenishment_rows = all_replenishment_rows[:5]
        replenishment_count = len(all_replenishment_rows)

        tpl = r"""
        {% extends "base.html" %}
        {% block content %}
          <style>
            .dashboard-head{display:flex;align-items:center;gap:14px;margin-bottom:18px}.dashboard-head h1{margin:0}.search-shell{margin-left:28px;flex:1;max-width:580px;position:relative}.search-shell input{padding-left:42px;background:#fff}.search-shell:before{content:"⌕";position:absolute;left:15px;top:9px;color:#8793aa;font-size:19px;z-index:2}.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;margin-bottom:16px}.metric{display:grid;grid-template-columns:55px 1fr;gap:14px;align-items:center;background:#fff;border:1px solid #e7eaf2;border-radius:22px;padding:18px;box-shadow:var(--shadow)}.metric .icon{display:grid;place-items:center;width:55px;height:55px;border-radius:17px;background:var(--soft,#edf3ff);color:var(--tone,#5577ee);font-size:23px}.metric span{color:#718096;font-size:12px;font-weight:650}.metric b{display:block;margin-top:2px;font-size:25px;letter-spacing:-.6px}.metric small{display:block;margin-top:4px;color:#2da176;font-size:10px}.dash-grid{display:grid;grid-template-columns:minmax(0,2.15fr) minmax(300px,.9fr);gap:16px;align-items:start}.panel-title{display:flex;align-items:center;gap:9px;margin-bottom:13px}.panel-title h2{margin:0}.panel-title .btn{margin-left:auto;padding:7px 11px;font-size:11px}.orders-card{padding-bottom:8px}.orders-card table{min-width:780px}.orders-card td{font-size:12px}.customer-name{font-weight:700}.order-no{color:#4166d3;font-weight:750;text-decoration:none}.side-stack{display:grid;gap:16px}.stock-list{display:grid;gap:2px}.stock-item{display:grid;grid-template-columns:42px 1fr auto;align-items:center;gap:10px;padding:10px 2px;border-bottom:1px solid #edf0f5}.stock-icon{display:grid;place-items:center;width:39px;height:39px;border-radius:12px;background:#f1f4f9;color:#68758d}.stock-name{font-size:12px;font-weight:700}.stock-sku{font-size:9px;color:#8b96a9}.stock-qty{font-size:12px;font-weight:800}.stock-qty:after{content:"";display:inline-block;width:7px;height:7px;margin-left:8px;border-radius:50%;background:#ee5262}.donut-wrap{display:grid;grid-template-columns:145px 1fr;align-items:center;gap:14px}.donut{width:140px;height:140px;border-radius:50%;display:grid;place-items:center;background:conic-gradient(#5577ee 0 calc(var(--p1)*1%),#65a7ec calc(var(--p1)*1%) calc((var(--p1) + var(--p2))*1%),#31b98b calc((var(--p1) + var(--p2))*1%) calc((var(--p1) + var(--p2) + var(--p3))*1%),#e05263 calc((var(--p1) + var(--p2) + var(--p3))*1%) 100%)}.donut:before{content:"";width:86px;height:86px;background:#fff;border-radius:50%;position:absolute}.donut-label{position:relative;text-align:center;font-size:11px;color:#77849b}.donut-label b{display:block;color:#17233c;font-size:25px}.legend{display:grid;gap:9px}.legend-row{display:grid;grid-template-columns:9px 1fr auto;gap:7px;align-items:center;font-size:10px}.legend-dot{width:8px;height:8px;border-radius:50%}.quick-card{grid-column:1}.quick-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:10px}.quick-grid .btn{min-height:80px;flex-direction:column;background:#f8faff;border-color:#e8ecf6;font-size:11px}.quick-grid .btn b{font-size:20px}.quick-grid .btn:nth-child(1){background:#edf3ff;color:#4166d3}.quick-grid .btn:nth-child(2){background:#eaf9f4;color:#16835f}.quick-grid .btn:nth-child(3){background:#eef3ff;color:#4b6bd3}.quick-grid .btn:nth-child(4){background:#fff5e5;color:#c57a10}.quick-grid .btn:nth-child(5){background:#f3edff;color:#7650ce}@media(max-width:1200px){.metrics{grid-template-columns:1fr 1fr}.dash-grid{grid-template-columns:1fr}.quick-card{grid-column:auto}}@media(max-width:760px){.dashboard-head{flex-wrap:wrap}.search-shell{order:3;margin-left:0;flex-basis:100%}.metrics{grid-template-columns:1fr}.quick-grid{grid-template-columns:1fr 1fr}.donut-wrap{grid-template-columns:1fr}.donut{margin:auto}}
          </style>
          <style>@media(min-width:1201px){.metrics{grid-template-columns:repeat(6,minmax(0,1fr));gap:12px}.metric{padding:14px;grid-template-columns:46px 1fr;gap:10px}.metric .icon{width:46px;height:46px}.metric small{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}</style>
          <style>.metrics a.metric{text-decoration:none;color:inherit;transition:transform .15s ease,box-shadow .15s ease,border-color .15s ease}.metrics a.metric:hover{transform:translateY(-2px);border-color:#cbd7f5;box-shadow:0 14px 32px rgba(34,55,100,.13)}.metrics a.metric:focus-visible{outline:3px solid rgba(85,119,238,.32);outline-offset:3px}</style>

          <div class="dashboard-head">
            <div><h1>Pulpit</h1><div class="muted">Przewagę buduje się codziennie — jedną dobrą decyzją naraz.</div></div>
            <form class="search-shell" action="{{ url_for('orders') }}"><input name="q" placeholder="Szukaj zamówień, produktów, klientów..."></form>
            <a class="btn primary" href="{{ url_for('order_new') }}">＋ Nowe zamówienie</a>
          </div>

          <div class="metrics">
            <a class="metric metric-link" href="{{ url_for('orders', tab='all', created_today=1) }}" aria-label="Pokaż zamówienia utworzone dzisiaj"><div class="icon">▣</div><div><span>Nowe zamówienia</span><b>{{ n_orders_today }}</b><small>{{ n_orders_current }} aktualnie w toku</small></div></a>
            <a class="metric metric-link" href="{{ url_for('orders', tab='all', issued_today=1) }}" aria-label="Pokaż zamówienia wydane dzisiaj" style="--soft:#eaf9f4;--tone:#1aa176"><div class="icon">◇</div><div><span>Wydane dzisiaj</span><b>{{ n_issued_today }}</b><small>{{ n_stock_qty }} szt. na stanie</small></div></a>
            <a class="metric" href="{{ url_for('orders', tab='new', ready_today=1) }}" style="--soft:#eaf9f4;--tone:#16835f;text-decoration:none;color:inherit"><div class="icon">✓</div><div><span>Możesz wydać dziś</span><b>{{ n_issuable_today }}</b><small title="{{ issuable_order_labels|join(', ') }}">{{ issuable_order_labels|join(', ') if issuable_order_labels else 'Brak kompletnych zamówień' }}</small></div></a>
            <a class="metric" href="{{ url_for('overdue_payments') }}" style="--soft:#fff0f1;--tone:#d9485b;text-decoration:none;color:inherit"><div class="icon">!</div><div><span>Zaległości</span><b>{{ overdue_count }}</b><small>{% if overdue_count %}Sprawdź płatności · {{ "{:,.0f}".format(overdue_total).replace(',', ' ') }} zł{% else %}Brak zaległych faktur{% endif %}</small></div></a>
            <a class="metric metric-link" href="{{ url_for('cash_flow') }}#replenishment-ranking" aria-label="Pokaż ranking produktów do uzupełnienia" style="--soft:#fff6e6;--tone:#db8a13"><div class="icon">△</div><div><span>Trzeba uzupełnić</span><b>{{ replenishment_count }}</b><small>Według rankingu zakupowego</small></div></a>
            <a class="metric metric-link" href="{{ url_for('stock') }}" aria-label="Pokaż szczegóły stanów magazynu" style="--soft:#edf3ff;--tone:#5577ee"><div class="icon">▤</div><div><span>Wartość magazynu</span><b>{{ "{:,.0f}".format(inventory_value_net).replace(',', ' ') }} zł</b><small>Netto z towarem w drodze</small></div></a>
          </div>

          <div class="dash-grid">
            <div class="card orders-card">
              <div class="panel-title"><span>▣</span><h2>Ostatnie zamówienia</h2><a class="btn" href="{{ url_for('orders') }}">Zobacz wszystkie</a></div>
              <table><thead><tr><th>Nr zamówienia</th><th>Klient</th><th>Data</th><th>Wartość</th><th>Status</th><th></th></tr></thead><tbody>
              {% for o in recent_orders %}<tr><td><a class="order-no" href="{{ url_for('order_view',order_id=o.id) }}">{{ canonical_order_no(o.id,o.created_at,o.order_no) }}</a></td><td class="customer-name">{{ o.customer_name or '-' }}</td><td>{{ o.created_at[:16] }}</td><td>{{ "%.2f"|format(o.total_net) }} {{ o.currency or 'PLN' }}</td><td><span class="badge {{ order_status_css(o.status) }}">{{ order_status_label(o.status) }}</span></td><td>{% if (o.status or '')|lower in ['new','pending','unconfirmed'] %}<form method="post" action="{{ url_for('order_status_update', order_id=o.id) }}"><input type="hidden" name="status" value="confirmed"><input type="hidden" name="return_to" value="dashboard"><button class="btn primary" type="submit">Potwierdź</button></form>{% endif %}</td></tr>{% endfor %}
              {% if not recent_orders %}<tr><td colspan="6" class="muted">Brak zamówień do wyświetlenia.</td></tr>{% endif %}
              </tbody></table>
            </div>
            <div class="side-stack">
              <div class="card"><div class="panel-title"><span style="color:#e69a20">△</span><h2>Trzeba uzupełnić:</h2><a class="btn" href="{{ url_for('cash_flow') }}">Wszystkie</a></div><div class="stock-list">
                {% for p in replenishment_rows %}<div class="stock-item"><div class="stock-icon">◇</div><div><div class="stock-name">{{ p.model or p.name or p.sku }}</div><div class="stock-sku">SKU: {{ p.sku }} · wynik {{ p.reorder_score }} · dostępne {{ p.available_qty }}</div></div><div class="stock-qty">+{{ p.suggested_qty }} szt.</div></div>{% endfor %}
                {% if not replenishment_rows %}<div class="muted">Brak produktów wymagających uzupełnienia.</div>{% endif %}
              </div></div>
              <div class="card"><div class="panel-title"><h2>Status zamówień</h2></div><div class="donut-wrap"><div class="donut" style="--p1:{{ status_new*100/status_divisor }};--p2:{{ status_work*100/status_divisor }};--p3:{{ status_done*100/status_divisor }}"><div class="donut-label"><b>{{ status_total }}</b>łącznie</div></div><div class="legend">
                <div class="legend-row"><i class="legend-dot" style="background:#5577ee"></i><span>Nowe</span><b>{{ status_new }}</b></div><div class="legend-row"><i class="legend-dot" style="background:#65a7ec"></i><span>W realizacji</span><b>{{ status_work }}</b></div><div class="legend-row"><i class="legend-dot" style="background:#31b98b"></i><span>Zrealizowane</span><b>{{ status_done }}</b></div><div class="legend-row"><i class="legend-dot" style="background:#e05263"></i><span>Anulowane</span><b>{{ status_cancelled }}</b></div>
              </div></div></div>
            </div>
            <div class="card quick-card"><div class="panel-title"><span>ϟ</span><h2>Szybkie akcje</h2></div><div class="quick-grid">
              <a class="btn" href="{{ url_for('order_new') }}"><b>＋</b><span>Nowe zamówienie</span></a><a class="btn" href="{{ url_for('products') }}"><b>◇</b><span>Dodaj produkt</span></a><a class="btn" href="{{ url_for('china') }}"><b>⇢</b><span>Przyjęcie dostawy</span></a><a class="btn" href="{{ url_for('stock') }}"><b>⇧</b><span>Wydanie z magazynu</span></a><a class="btn" href="{{ url_for('order_scan') }}"><b>▧</b><span>Skanuj QR</span></a><a class="btn" href="{{ url_for('invoices') }}"><b>▤</b><span>Faktury</span></a>
            </div>
          </div>
          </div>
        {% endblock %}
        """
        return render_template_string(tpl, title="Start", base_url=BASE_URL, db_path=DB_PATH,
                                      n_products=n_products, n_orders_current=n_orders_current, n_china_active=n_china_active,
                                      n_stock_qty=n_stock_qty, n_in_delivery_qty=n_in_delivery_qty,
                                      inventory_value_net=inventory_value_net, n_orders_today=n_orders_today,
                                      n_issued_today=n_issued_today, n_issuable_today=n_issuable_today,
                                      overdue_count=overdue_count, overdue_total=overdue_total,
                                      issuable_order_labels=issuable_order_labels, replenishment_rows=replenishment_rows,
                                      replenishment_count=replenishment_count,
                                      recent_orders=recent_orders, status_new=status_new, status_work=status_work,
                                      status_done=status_done, status_cancelled=status_cancelled, status_total=status_total,
                                      status_divisor=status_divisor)




    @app.get("/company")
    def company():
        maybe_pull_shared_from_supabase()
        c = conn()
        cur = c.cursor()
        cur.execute("SELECT * FROM company_profile WHERE id=1")
        row = cur.fetchone()
        if row:
            clean_account, clean_swift = normalize_company_bank_fields(
                row["bank_account"], row["bank_swift"]
            )
            if clean_account != norm(row["bank_account"]) or clean_swift != norm(row["bank_swift"]):
                cur.execute(
                    "UPDATE company_profile SET bank_account=?, bank_swift=?, updated_at=? WHERE id=1",
                    (clean_account, clean_swift, now_iso()),
                )
                c.commit()
                cur.execute("SELECT * FROM company_profile WHERE id=1")
                row = cur.fetchone()
        c.close()

        tpl = r"""
        {% extends "base.html" %}
        {% block content %}
          <div class="card">
            <h1>Dane mojej firmy</h1>
            <div class="muted">Te dane trafiÄ… na fakturÄ™ sprzedaĹĽowÄ….</div>
          </div>

          <div class="card">
            <form method="post" action="{{ url_for('company_save') }}" class="row">
              <div><label class="muted small">Nazwa firmy</label><input name="company_name" value="{{ row['company_name'] if row else '' }}"></div>
              <div><label class="muted small">NIP</label><input name="nip" value="{{ row['nip'] if row else '' }}"></div>
              <div><label class="muted small">Telefon</label><input name="phone" value="{{ row['phone'] if row else '' }}"></div>
              <div><label class="muted small">Email</label><input name="email" value="{{ row['email'] if row else '' }}"></div>
              <div><label class="muted small">Konto bankowe</label><input name="bank_account" value="{{ row['bank_account'] if row else '' }}"></div>
              <div><label class="muted small">SWIFT / BIC</label><input name="bank_swift" value="{{ row['bank_swift'] if row else '' }}" placeholder="np. ALBPPLPWXXX"></div>
              <div><label class="muted small">Adres</label><textarea name="address">{{ row['address'] if row else '' }}</textarea></div>
              <div class="flex" style="align-items:flex-end;"><button class="btn primary" type="submit">Zapisz dane firmy</button></div>
            </form>
          </div>
        {% endblock %}
        """
        return render_template_string(tpl, title="Dane mojej firmy", base_url=BASE_URL, db_path=DB_PATH, row=row)



    @app.post("/company/save")
    def company_save():
        company_name = norm(request.form.get("company_name"))
        address = norm(request.form.get("address"))
        nip = norm(request.form.get("nip"))
        phone = norm(request.form.get("phone"))
        email = norm(request.form.get("email"))
        bank_account, bank_swift = normalize_company_bank_fields(
            request.form.get("bank_account"), request.form.get("bank_swift")
        )

        c = conn()
        cur = c.cursor()
        cur.execute("""
          INSERT INTO company_profile(id, company_name, address, nip, phone, email, bank_account, bank_swift, updated_at)
          VALUES(1,?,?,?,?,?,?,?,?)
          ON CONFLICT(id) DO UPDATE SET
            company_name=excluded.company_name,
            address=excluded.address,
            nip=excluded.nip,
            phone=excluded.phone,
            email=excluded.email,
            bank_account=excluded.bank_account,
            bank_swift=excluded.bank_swift,
            updated_at=excluded.updated_at
        """, (company_name, address, nip, phone, email, bank_account, bank_swift, now_iso()))
        c.commit()
        c.close()
        return redirect(url_for("company"))




    @app.post("/email/order-confirmations/retry-failed")
    def retry_failed_order_confirmations():
        supplied_token = norm(request.headers.get("X-Admin-Token"))
        if not ADMIN_ACTION_TOKEN or supplied_token != ADMIN_ACTION_TOKEN:
            return jsonify(ok=False, error="Brak autoryzacji"), 401
        c = conn()
        try:
            cur = c.cursor()
            cur.execute("""
              SELECT ref_id
              FROM email_events
              WHERE event_type='order_confirmation' AND ok=0
              ORDER BY created_at
              LIMIT 50
            """)
            order_ids = [to_int(row["ref_id"], 0) for row in cur.fetchall()]
        finally:
            c.close()

        retried = 0
        sent = 0
        for order_id in order_ids:
            if order_id <= 0:
                continue
            retried += 1
            result = _send_saved_order_confirmation(order_id)
            if result.get("ok"):
                sent += 1
        app.logger.info("Retry potwierdzeń zamówień retried=%s sent=%s", retried, sent)
        return jsonify(ok=True, retried=retried, sent=sent)




    @app.route("/email-test", methods=["GET", "POST"])
    def email_test():
        cfg = email_config_summary()
        cfg = dict(cfg or {})
        cfg["module_loaded"] = bool(send_email)
        cfg["import_error"] = _EMAIL_IMPORT_ERROR
        cfg["api_key_set"] = "RESEND_API_KEY" not in (cfg.get("missing") or [])
        result = None
        test_to = norm(request.form.get("to")) or norm(cfg.get("admin_email")) or "biuro@niedzwieccy.com"

        if request.method == "POST":
            if not send_email:
                result = {
                    "ok": False,
                    "error": "Moduł email_module.py nie jest załadowany. Wgraj email_module.py do repo obok app.py i zrób deploy.",
                    "import_error": _EMAIL_IMPORT_ERROR,
                }
            else:
                try:
                    result = send_email(
                        test_to,
                        "Test maili z Niedźwieccy Orders",
                        "<div style='font-family:Arial,sans-serif'><h2>Test maili działa</h2><p>Jeśli widzisz tę wiadomość, Resend jest poprawnie podpięty do aplikacji.</p></div>",
                        "Test maili działa. Jeśli widzisz tę wiadomość, Resend jest poprawnie podpięty do aplikacji.",
                    )
                except Exception as exc:
                    result = {"ok": False, "error": str(exc)}

        tpl = r"""
        {% extends "base.html" %}
        {% block content %}
          <div class="card">
            <h1>Test maili</h1>
            <p class="muted">Ta strona sprawdza konfigurację Resend po stronie Rendera. API key nie jest tutaj wyświetlany.</p>
            <div class="kpi">
              <span class="pill">Moduł: <b>{{ 'załadowany' if cfg.module_loaded else 'brak' }}</b></span>
              <span class="pill">Wysyłka: <b>{{ 'włączona' if cfg.enabled else 'wyłączona' }}</b></span>
              <span class="pill">Konfiguracja: <b>{{ 'OK' if cfg.configured else 'brakuje danych' }}</b></span>
              <span class="pill">API key: <b>{{ 'ustawiony' if cfg.api_key_set else 'brak' }}</b></span>
            </div>
            <div class="line"></div>
            <p><b>EMAIL_FROM:</b> {{ cfg['from'] or '-' }}</p>
            <p><b>ADMIN_EMAIL:</b> {{ cfg.admin_email or '-' }}</p>
            {% if cfg.missing %}
              <p class="hint"><b>Brakuje:</b> {{ cfg.missing|join(', ') }}</p>
            {% endif %}
            {% if cfg.import_error %}
              <p class="hint"><b>Błąd importu:</b> {{ cfg.import_error }}</p>
            {% endif %}
            <form method="post" class="flex" style="margin-top:12px">
              <input name="to" value="{{ test_to }}" placeholder="email do testu" style="max-width:420px">
              <button class="btn primary" type="submit">Wyślij test</button>
            </form>
          </div>

          {% if result %}
            <div class="card">
              <h2>Wynik testu</h2>
              {% if result.ok %}
                <p class="badge" style="background:#dcfce7;border-color:#86efac">Mail wysłany</p>
              {% else %}
                <p class="badge" style="background:#fee2e2;border-color:#fecaca">Mail nie wysłany</p>
              {% endif %}
              <pre style="white-space:pre-wrap;background:#111;color:#fff;padding:12px;border-radius:12px;overflow:auto">{{ result|tojson(indent=2) }}</pre>
            </div>
          {% endif %}
        {% endblock %}
        """
        return render_template_string(tpl, title="Test maili", base_url=BASE_URL, db_path=DB_PATH, cfg=cfg, result=result, test_to=test_to)




    @app.get("/payments/overdue")
    def overdue_payments():
        maybe_pull_shared_from_supabase()
        c = conn()
        rows = overdue_invoice_rows(c)
        c.close()
        total_gross = sum(float(inv.get("total_gross") or 0) for inv in rows)
        tpl = r"""
        {% extends "base.html" %}
        {% block content %}
          <div class="card">
            <div class="flex" style="align-items:center">
              <div><h1 style="margin:0">Zaległości</h1><div class="muted">Faktury nieopłacone widoczne od godz. 8:00 następnego dnia po terminie płatności.</div></div>
              <a class="btn right" href="{{ url_for('home') }}">← Pulpit</a>
            </div>
            <div class="flex" style="margin-top:16px"><span class="badge danger">Zaległych faktur: {{ rows|length }}</span><span class="badge">Łącznie brutto: {{ "%.2f"|format(total_gross) }} PLN</span></div>
          </div>
          <div class="card">
            <h2>Sprawdź płatności</h2>
            <table><thead><tr><th>Faktura</th><th>Klient</th><th>Zamówienie</th><th>Termin</th><th>Po terminie</th><th>Brutto</th><th>Akcje</th></tr></thead><tbody>
            {% for inv in rows %}<tr>
              <td><b>{{ inv.invoice_no }}</b></td><td>{{ inv.buyer_name or inv.order_customer_name or '-' }}</td><td>{{ inv.order_display }}</td><td>{{ inv.payment_to }}</td>
              <td><span class="badge danger">{{ inv.overdue_days }} dni</span>{% if inv.payment_reminder %} <span class="badge ok">Przypomnienie wysłane</span>{% endif %}</td><td><b>{{ "%.2f"|format(inv.total_gross or 0) }} PLN</b></td>
              <td><div class="flex"><a class="btn" href="{{ url_for('invoice_download_admin', invoice_id=inv.id) }}" target="_blank">Faktura PDF</a>{% if inv.source_order_id %}<a class="btn" href="{{ url_for('order_view', order_id=inv.source_order_id) }}">Zamówienie</a>{% endif %}<form method="post" action="{{ url_for('invoice_payment_reminder_admin', invoice_id=inv.id) }}"><input type="hidden" name="next" value="{{ request.full_path }}"><button class="btn" type="submit">Przypomnij o płatności</button></form><form method="post" action="{{ url_for('invoice_paid_admin', invoice_id=inv.id) }}"><input type="hidden" name="next" value="{{ request.full_path }}"><button class="btn ok" type="submit">Faktura opłacona</button></form></div></td>
            </tr>{% endfor %}
            {% if not rows %}<tr><td colspan="7" class="muted">Brak zaległych faktur. Wszystkie płatności są aktualne.</td></tr>{% endif %}
            </tbody></table>
          </div>
        {% endblock %}
        """
        return render_template_string(tpl, title="Zaległości", base_url=BASE_URL, db_path=DB_PATH, rows=rows, total_gross=total_gross)



    exported = {'login': login, 'logout': logout, 'home': home, 'company': company, 'company_save': company_save, 'retry_failed_order_confirmations': retry_failed_order_confirmations, 'email_test': email_test, 'overdue_payments': overdue_payments}
    globals().update(exported)
    return exported
