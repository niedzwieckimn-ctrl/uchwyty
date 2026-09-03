"""Mechanically extracted Flask routes; business logic is unchanged."""

def register_routes(context):
    globals().update(context)


    @app.get("/searches")
    def client_searches():
        q = norm(request.args.get("q"))
        rows, source_label = load_client_search_rows(limit=5000)
        if q:
            needle = q.lower()
            rows = [
                r for r in rows
                if needle in (r.get("query") or "").lower()
                or needle in (r.get("customer_email") or "").lower()
                or needle in (r.get("customer_name") or "").lower()
            ]

        global_stats = {}
        model_stats = {}
        client_stats = {}
        phrase_events_seen = set()
        model_events_seen = set()
        for r in rows:
            query = norm(r.get("query"))
            if not query:
                continue
            email = norm(r.get("customer_email")).lower()
            name = norm(r.get("customer_name"))
            client_key = email or name or "anon"
            product_sku = norm(r.get("product_sku"))
            product_model = norm(r.get("product_model"))
            product_name = norm(r.get("product_name"))
            results_count = to_int(r.get("results_count"), 0)
            created_at = norm(r.get("created_at"))

            product_key = product_sku or product_model
            model_event_key = (email, name, query.lower(), product_key.lower(), created_at)
            if product_key and 0 < results_count <= 20 and model_event_key not in model_events_seen:
                model_events_seen.add(model_event_key)
                m = model_stats.setdefault(product_key, {
                    "product_model": product_key,
                    "product_sku": product_sku,
                    "product_name": product_name or product_model,
                    "searches_count": 0,
                    "clients": set(),
                    "phrases": set(),
                    "last_at": "",
                })
                m["searches_count"] += 1
                m["clients"].add(client_key)
                if query:
                    m["phrases"].add(query)
                if product_sku and not m.get("product_sku"):
                    m["product_sku"] = product_sku
                if (product_name or product_model) and not m.get("product_name"):
                    m["product_name"] = product_name or product_model
                if created_at > m["last_at"]:
                    m["last_at"] = created_at

            phrase_event_key = (email, name, query.lower(), created_at)
            if phrase_event_key in phrase_events_seen:
                continue
            phrase_events_seen.add(phrase_event_key)

            g = global_stats.setdefault(query, {
                "query": query,
                "searches_count": 0,
                "clients": set(),
                "no_result_count": 0,
                "max_results": 0,
                "last_at": "",
            })
            g["searches_count"] += 1
            g["clients"].add(client_key)
            if results_count == 0:
                g["no_result_count"] += 1
            g["max_results"] = max(g["max_results"], results_count)
            if created_at > g["last_at"]:
                g["last_at"] = created_at

            client_label = name or email or "Nieznany klient"
            skey = (client_label, email, query)
            s = client_stats.setdefault(skey, {
                "client_label": client_label,
                "customer_email": email,
                "query": query,
                "searches_count": 0,
                "no_result_count": 0,
                "max_results": 0,
                "last_at": "",
            })
            s["searches_count"] += 1
            if results_count == 0:
                s["no_result_count"] += 1
            s["max_results"] = max(s["max_results"], results_count)
            if created_at > s["last_at"]:
                s["last_at"] = created_at

        model_rows = []
        for r in model_stats.values():
            item = dict(r)
            item["clients_count"] = len(item.pop("clients"))
            phrases = sorted(item.pop("phrases"))
            item["phrases_preview"] = ", ".join(phrases[:5])
            model_rows.append(item)
        model_rows.sort(key=lambda r: (r["searches_count"], r["last_at"]), reverse=True)
        model_rows = model_rows[:10]

        global_rows = []
        for r in global_stats.values():
            item = dict(r)
            item["clients_count"] = len(item.pop("clients"))
            global_rows.append(item)
        global_rows.sort(key=lambda r: (r["searches_count"], r["last_at"]), reverse=True)
        global_rows = global_rows[:10]

        summary_rows = list(client_stats.values())
        summary_rows.sort(key=lambda r: r["last_at"], reverse=True)
        summary_rows = summary_rows[:50]

        latest_rows = rows[:50]
        total_count = len(rows)

        tpl = r"""
        {% extends "base.html" %}
        {% block content %}
          <div class="card">
            <div class="flex">
              <h1 style="margin:0;">Top wyszukiwania</h1>
              <span class="badge">Łącznie: {{ total_count }}</span>
              <span class="badge">{{ source_label }}</span>
            </div>
            <form method="get" class="grid3" style="margin-top:10px;">
              <input name="q" value="{{ q }}" placeholder="Szukaj: klient / email / fraza">
              <button class="btn primary" type="submit">Szukaj</button>
              <a class="btn" href="{{ url_for('client_searches') }}">Wyczyść</a>
            </form>
          </div>

          <div class="card">
            <h2>TOP 10 modeli / SKU</h2>
            <div class="muted" style="margin-bottom:8px;">
              Najważniejsze produkty, które klienci realnie zobaczyli po wyszukaniu w panelu — także po nazwie zwyczajowej, rozstawie albo części SKU.
            </div>
            <table>
              <thead>
                <tr><th>Model / SKU</th><th>Nazwa</th><th>Ile razy</th><th>Klientów</th><th>Ostatnio</th></tr>
              </thead>
              <tbody>
                {% for r in model_rows %}
                  <tr>
                    <td><b>{{ r.product_model }}</b>{% if r.product_sku and r.product_sku != r.product_model %}<div class="muted">{{ r.product_sku }}</div>{% endif %}</td>
                    <td>{{ r.product_name or '-' }}</td>
                    <td><span class="badge">{{ r.searches_count }}</span></td>
                    <td>{{ r.clients_count }}</td>
                    <td class="muted">{{ r.last_at }}</td>
                  </tr>
                {% endfor %}
                {% if not model_rows %}
                  <tr><td colspan="5" class="muted">Brak zapisanych wyszukiwań.</td></tr>
                {% endif %}
              </tbody>
            </table>
          </div>

          <details class="card">
            <summary style="cursor:pointer;font-weight:700;font-size:16px;">Pokaż szczegóły: frazy, klienci i ostatnie wpisy</summary>

          <div style="margin-top:14px;">
            <h2>Frazy klientów</h2>
            <div class="muted" style="margin-bottom:8px;">
              Tu zostają wpisane teksty klienta. Pomaga sprawdzić, jak klienci szukają produktów i gdzie pojawiają się literówki albo brakujące nazwy.
            </div>
            <table>
              <thead>
                <tr><th>Fraza</th><th>Wyszukań</th><th>Klientów</th><th>Bez wyników</th><th>Najwięcej wyników</th><th>Ostatnio</th></tr>
              </thead>
              <tbody>
                {% for r in global_rows %}
                  <tr>
                    <td><b>{{ r.query }}</b></td>
                    <td><span class="badge">{{ r.searches_count }}</span></td>
                    <td>{{ r.clients_count }}</td>
                    <td>{% if r.no_result_count %}<span class="badge">{{ r.no_result_count }}</span>{% else %}-{% endif %}</td>
                    <td>{{ r.max_results }}</td>
                    <td class="muted">{{ r.last_at }}</td>
                  </tr>
                {% endfor %}
                {% if not global_rows %}
                  <tr><td colspan="6" class="muted">Brak zapisanych fraz.</td></tr>
                {% endif %}
              </tbody>
            </table>
          </div>

          <div style="margin-top:18px;">
            <h2>Wyszukiwania według klienta</h2>
            <div class="muted" style="margin-bottom:8px;">Tu zobaczysz, kto konkretnie szukał danej frazy.</div>
            <table>
              <thead>
                <tr><th>Klient</th><th>Email</th><th>Fraza</th><th>Ile razy</th><th>Bez wyników</th><th>Ostatnio</th></tr>
              </thead>
              <tbody>
                {% for r in summary_rows %}
                  <tr>
                    <td><b>{{ r.client_label }}</b></td>
                    <td>{{ r.customer_email or '-' }}</td>
                    <td>{{ r.query }}</td>
                    <td><span class="badge">{{ r.searches_count }}</span></td>
                    <td>{% if r.no_result_count %}<span class="badge">{{ r.no_result_count }}</span>{% else %}-{% endif %}</td>
                    <td class="muted">{{ r.last_at }}</td>
                  </tr>
                {% endfor %}
                {% if not summary_rows %}
                  <tr><td colspan="6" class="muted">Brak zapisanych wyszukiwań.</td></tr>
                {% endif %}
              </tbody>
            </table>
          </div>

          <div style="margin-top:18px;">
            <h2>Ostatnie wpisy</h2>
            <table>
              <thead>
                <tr><th>Czas</th><th>Klient</th><th>Email</th><th>Fraza</th><th>Model / SKU</th><th>Wyniki</th></tr>
              </thead>
              <tbody>
                {% for r in latest_rows %}
                  <tr>
                    <td class="muted">{{ r.created_at }}</td>
                    <td>{{ r.customer_name or '-' }}</td>
                    <td>{{ r.customer_email or '-' }}</td>
                    <td><b>{{ r.query }}</b></td>
                    <td>{{ r.product_model or r.product_sku or '-' }}</td>
                    <td>{{ r.results_count }}</td>
                  </tr>
                {% endfor %}
                {% if not latest_rows %}
                  <tr><td colspan="6" class="muted">Brak wpisów.</td></tr>
                {% endif %}
              </tbody>
            </table>
          </div>
          </details>
        {% endblock %}
        """
        return render_template_string(tpl, title="Top wyszukiwania", base_url=BASE_URL, db_path=DB_PATH,
                                      model_rows=model_rows, global_rows=global_rows, summary_rows=summary_rows, latest_rows=latest_rows,
                                      total_count=total_count, q=q, source_label=source_label)




    @app.get("/customers")
    def customers():
        maybe_pull_shared_from_supabase()
        q = norm(request.args.get("q"))
        c = conn()
        cur = c.cursor()
        if q:
            like = f"%{q}%"
            cur.execute("""
              SELECT * FROM customers
              WHERE name LIKE ? OR phone LIKE ? OR email LIKE ? OR address LIKE ? OR nip LIKE ?
              ORDER BY id DESC
              LIMIT 500
            """, (like, like, like, like, like))
        else:
            cur.execute("SELECT * FROM customers ORDER BY id DESC LIMIT 500")
        rows = cur.fetchall()
        c.close()

        tpl = r"""
        {% extends "base.html" %}
        {% block content %}
          <div class="card">
            <h1>Klienci stali</h1>
            <form method="get" class="grid3" style="margin-top:10px;">
              <input name="q" value="{{ q }}" placeholder="Szukaj: nazwa / telefon / email / adres / NIP">
              <button class="btn primary" type="submit">Szukaj</button>
              <a class="btn" href="{{ url_for('customers') }}">WyczyĹ›Ä‡</a>
            </form>
          </div>

          <div class="card">
            <h2>Dodaj klienta</h2>
            <form method="post" action="{{ url_for('customers_create') }}" class="row">
              <div>
                <label class="muted small">Nazwa</label>
                <input name="name" required>
              </div>
              <div>
                <label class="muted small">Telefon</label>
                <input name="phone">
              </div>
              <div>
                <label class="muted small">Email</label>
                <input name="email">
              </div>
              <div>
                <label class="muted small">NIP</label>
                <input name="nip" placeholder="np. 1234567890">
              </div>
              <div>
                <label class="muted small">Adres</label>
                <textarea name="address" placeholder="Ulica, kod, miasto"></textarea>
              </div>
              <div>
                <label class="muted small">Język panelu klienta</label>
                <select name="language">
                  <option value="pl">PL — polski</option><option value="de">DE — niemiecki</option>
                  <option value="en">EN — angielski</option><option value="es">ES — hiszpański</option>
                  <option value="it">IT — włoski</option>
                </select>
              </div>
              <div>
                <label class="muted small">Cennik klienta</label>
                <select name="price_list">
                  <option value="pln">Polska — PLN</option>
                  <option value="eu_eur">UE — EUR</option>
                </select>
              </div>
              <div class="flex" style="align-items:flex-end;">
                <button class="btn primary" type="submit">Zapisz klienta</button>
              </div>
            </form>
          </div>

          <div class="card">
            <h2>Lista klientĂłw</h2>
            <table>
              <thead>
                <tr><th>Nazwa</th><th>Telefon</th><th>Email</th><th>NIP</th><th>Język</th><th>Cennik</th><th>Adres</th><th>Akcje</th></tr>
              </thead>
              <tbody>
                {% for r in rows %}
                  <tr>
                    <td><b>{{ r['name'] }}</b></td>
                    <td>{{ r['phone'] or '-' }}</td>
                    <td>{{ r['email'] or '-' }}</td>
                    <td>{{ r['nip'] or '-' }}</td>
                    <td><span class="badge">{{ (r['language'] or 'pl')|upper }}</span></td>
                    <td><span class="badge">{{ 'UE — EUR' if r['price_list'] == 'eu_eur' else 'Polska — PLN' }}</span></td>
                    <td style="white-space:pre-line;">{{ r['address'] or '-' }}</td>
                    <td>
                      <div class="flex">
                        <a class="btn" href="{{ url_for('customers_edit', customer_id=r['id']) }}">Edytuj</a>
                        <form method="post" action="{{ url_for('customers_delete', customer_id=r['id']) }}" onsubmit="return confirm('UsunÄ…Ä‡ klienta?')">
                          <button class="btn danger" type="submit">UsuĹ„</button>
                        </form>
                      </div>
                    </td>
                  </tr>
                {% endfor %}
                {% if not rows %}
                  <tr><td colspan="8" class="muted">Brak klientĂłw.</td></tr>
                {% endif %}
              </tbody>
            </table>
          </div>
        {% endblock %}
        """
        return render_template_string(tpl, title="Klienci", base_url=BASE_URL, db_path=DB_PATH, rows=rows, q=q)



    @app.post("/customers/create")
    def customers_create():
        name = norm(request.form.get("name"))
        address = norm(request.form.get("address"))
        phone = norm(request.form.get("phone"))
        email = norm(request.form.get("email"))
        nip = norm(request.form.get("nip"))
        language = normalize_client_language(request.form.get("language"))
        price_list = price_list_for_language(language)
        if not name:
            return "Brak nazwy klienta", 400

        if supabase_enabled():
            remote_first_create_customer(name, address, phone, email, nip, language, price_list)
        else:
            c = conn()
            cur = c.cursor()
            cur.execute(
                "INSERT INTO customers(name, address, phone, email, nip, language, price_list, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (name, address, phone, email, nip, language, price_list, now_iso())
            )
            c.commit()
            c.close()

        try:
            link_orders_to_customers_by_email(sync_remote=True)
        except Exception:
            pass
        return redirect(url_for("customers"))



    @app.get("/customers/<int:customer_id>/edit")
    def customers_edit(customer_id):
        c = conn()
        cur = c.cursor()
        cur.execute("SELECT * FROM customers WHERE id=?", (customer_id,))
        row = cur.fetchone()
        c.close()
        if not row:
            return "Nie znaleziono klienta", 404

        tpl = r"""
        {% extends "base.html" %}
        {% block content %}
          <div class="card">
            <h1>Edycja klienta</h1>
            <div class="muted">ZmieĹ„ dane zapisane dla staĹ‚ego klienta.</div>
          </div>

          <div class="card">
            <form method="post" action="{{ url_for('customers_update', customer_id=row['id']) }}" class="row">
              <div>
                <label class="muted small">Nazwa</label>
                <input name="name" value="{{ row['name'] }}" required>
              </div>
              <div>
                <label class="muted small">Telefon</label>
                <input name="phone" value="{{ row['phone'] or '' }}">
              </div>
              <div>
                <label class="muted small">Email</label>
                <input name="email" value="{{ row['email'] or '' }}">
              </div>
              <div>
                <label class="muted small">NIP</label>
                <input name="nip" value="{{ row['nip'] or '' }}" placeholder="np. 1234567890">
              </div>
              <div>
                <label class="muted small">Adres</label>
                <textarea name="address" placeholder="Ulica, kod, miasto">{{ row['address'] or '' }}</textarea>
              </div>
              <div>
                <label class="muted small">Język panelu klienta</label>
                <select name="language">
                  {% for code, label in [('pl','PL — polski'),('de','DE — niemiecki'),('en','EN — angielski'),('es','ES — hiszpański'),('it','IT — włoski')] %}
                    <option value="{{ code }}" {% if (row['language'] or 'pl') == code %}selected{% endif %}>{{ label }}</option>
                  {% endfor %}
                </select>
              </div>
              <div>
                <label class="muted small">Cennik klienta</label>
                <select name="price_list">
                  <option value="pln" {% if (row['price_list'] or 'pln') == 'pln' %}selected{% endif %}>Polska — PLN</option>
                  <option value="eu_eur" {% if row['price_list'] == 'eu_eur' %}selected{% endif %}>UE — EUR</option>
                </select>
              </div>
              <div class="flex" style="align-items:flex-end;">
                <button class="btn primary" type="submit">Zapisz zmiany</button>
                <a class="btn" href="{{ url_for('customers') }}">PowrĂłt</a>
              </div>
            </form>
          </div>
        {% endblock %}
        """
        return render_template_string(tpl, title="Edycja klienta", base_url=BASE_URL, db_path=DB_PATH, row=row)



    @app.post("/customers/<int:customer_id>/update")
    def customers_update(customer_id):
        name = norm(request.form.get("name"))
        address = norm(request.form.get("address"))
        phone = norm(request.form.get("phone"))
        email = norm(request.form.get("email"))
        nip = norm(request.form.get("nip"))
        language = normalize_client_language(request.form.get("language"))
        price_list = price_list_for_language(language)
        if not name:
            return "Brak nazwy klienta", 400

        c = conn()
        cur = c.cursor()
        cur.execute("""
          UPDATE customers
          SET name=?, address=?, phone=?, email=?, nip=?, language=?, price_list=?
          WHERE id=?
        """, (name, address, phone, email, nip, language, price_list, customer_id))
        c.commit()
        c.close()

        if supabase_enabled():
            supabase_update_rows("customers", {
                "name": name,
                "address": address,
                "phone": phone,
                "email": email,
                "nip": nip,
                "language": language,
                "price_list": price_list,
            }, {"id": customer_id})

        try:
            link_orders_to_customers_by_email(sync_remote=True)
        except Exception:
            pass
        return redirect(url_for("customers"))



    @app.post("/customers/<int:customer_id>/delete")
    def customers_delete(customer_id):
        if supabase_enabled():
            supabase_delete_rows("customers", {"id": customer_id})

        c = conn()
        cur = c.cursor()
        cur.execute("DELETE FROM customers WHERE id=?", (customer_id,))
        c.commit()
        c.close()
        return redirect(url_for("customers"))




    @app.get("/api/client_stock_catalog")
    def api_client_stock_catalog():
        if not supabase_enabled():
            return jsonify(ok=False, error="Brak konfiguracji Supabase na serwerze"), 503

        try:
            profile = _client_profile_for_email(g.client_user.get("email"))
            rows = _client_stock_catalog_rows(profile)
        except Exception as exc:
            app.logger.error(
                "Nie udało się pobrać aktualnych stanów lub cennika bezpośrednio z Supabase: %s",
                type(exc).__name__,
            )
            return jsonify(
                ok=False,
                error="Nie udało się pobrać aktualnych stanów i cen z Supabase",
            ), 503

        response = jsonify(
            ok=True,
            rows=rows,
            source="supabase",
            price_list=profile.get("price_list", "pln"),
            currency=profile.get("currency", "PLN"),
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response




    @app.route("/api/client_search_log", methods=["POST", "OPTIONS"])
    def api_client_search_log():
        if request.method == "OPTIONS":
            return ("", 204)

        data = request.get_json(silent=True) or {}
        query = norm(data.get("query"))[:120]
        if len(query) < 2:
            return jsonify(ok=True, skipped=True)

        # Tożsamość pochodzi wyłącznie ze zweryfikowanego tokenu Supabase,
        # nigdy z danych przesłanych przez przeglądarkę.
        email = norm(g.client_user.get("email")).lower()[:180]
        name = ""
        try:
            profile_name = norm(_client_profile_for_email(email).get("name"))
            if profile_name and "@" not in profile_name and not _order_name_is_fallback(profile_name, email):
                name = profile_name
        except Exception as exc:
            app.logger.warning("Nie udalo sie ustalic nazwy klienta dla wyszukiwania %s: %s", email, type(exc).__name__)
        if not name:
            auth_name = norm(g.client_user.get("name"))
            if auth_name and "@" not in auth_name and not _order_name_is_fallback(auth_name, email):
                name = auth_name
        source = norm(data.get("source"))[:40] or "stock"
        results_count = to_int(data.get("results_count"), 0)
        if results_count < 0:
            results_count = 0
        matches = data.get("matches") if isinstance(data.get("matches"), list) else []
        created_at = now_iso()
        model_candidates = {}
        exact_model = None
        for item in matches[:30]:
            if not isinstance(item, dict):
                continue
            product_model = norm(item.get("model"))[:120]
            product_name = norm(item.get("name"))[:180]
            if not product_model:
                continue
            model_candidates.setdefault(product_model.casefold(), (product_model, product_name))
            if query.casefold() in {product_model.casefold(), product_name.casefold()}:
                exact_model = (product_model, product_name)

        selected_model = exact_model
        if selected_model is None and len(model_candidates) == 1:
            selected_model = next(iter(model_candidates.values()))
        rows_to_save = [{
            "customer_email": email,
            "customer_name": name,
            "query": query,
            "product_sku": "",
            "product_model": selected_model[0] if selected_model else "",
            "product_name": selected_model[1] if selected_model else "",
            "results_count": results_count,
            "source": source,
            "created_at": created_at,
        }]

        cutoff = (app_now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        deduped_rows = []
        c = conn()
        cur = c.cursor()
        for row in rows_to_save:
            cur.execute("""
                  SELECT 1
                  FROM client_search_logs
                  WHERE LOWER(COALESCE(customer_email,''))=?
                    AND LOWER(COALESCE(query,''))=?
                    AND COALESCE(source,'stock')=?
                    AND created_at>=?
                  LIMIT 1
                """, (
                    row.get("customer_email", "").lower(),
                    row.get("query", "").lower(),
                    row.get("source", "stock"),
                    cutoff,
                ))
            if cur.fetchone():
                continue
            deduped_rows.append(row)
        c.close()

        if not deduped_rows:
            return jsonify(ok=True, skipped=True, duplicate=True)

        cloud_ok = False
        cloud_saved = 0
        for row in deduped_rows:
            try:
                if save_client_search_log_supabase(row):
                    cloud_saved += 1
            except Exception:
                pass
            save_client_search_log_local(row)

        cloud_ok = cloud_saved == len(deduped_rows)
        return jsonify(ok=True, cloud=bool(cloud_ok), rows=len(deduped_rows))




    @app.route("/api/client/profile", methods=["GET", "PATCH", "OPTIONS"])
    def api_client_profile():
        if request.method == "OPTIONS":
            return ("", 204)

        email = _email_key(g.client_user.get("email"))
        try:
            profile = _client_profile_for_email(email)
        except Exception as exc:
            app.logger.error("Nie udało się pobrać profilu klienta: %s", type(exc).__name__)
            return jsonify(ok=False, error="Nie udało się pobrać profilu klienta"), 503

        if request.method == "GET":
            return jsonify(ok=True, customer=profile)
        # Język określa również cennik (PLN albo EUR), dlatego klient nie może
        # zmieniać go samodzielnie. Ustawienie jest dostępne tylko administratorowi
        # w panelu magazynu. Blokada po stronie serwera chroni również przed ręcznym
        # wywołaniem endpointu poza interfejsem.
        return jsonify(ok=False, error="Język i cennik konta może zmienić wyłącznie administrator"), 403



    exported = {'client_searches': client_searches, 'customers': customers, 'customers_create': customers_create, 'customers_edit': customers_edit, 'customers_update': customers_update, 'customers_delete': customers_delete, 'api_client_stock_catalog': api_client_stock_catalog, 'api_client_search_log': api_client_search_log, 'api_client_profile': api_client_profile}
    globals().update(exported)
    return exported
