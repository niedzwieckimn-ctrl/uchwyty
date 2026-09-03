"""Mechanically extracted Flask routes; business logic is unchanged."""

def register_routes(context):
    globals().update(context)


    @app.get("/pricing")
    def pricing():
        maybe_pull_shared_from_supabase()
        q = norm(request.args.get("q"))
        eur_imported = max(0, int(to_float(request.args.get("eur_imported"), 0)))
        eur_import_error = norm(request.args.get("eur_import_error"))
        eur_local_saved = max(0, int(to_float(request.args.get("eur_local_saved"), 0)))
        c = conn()
        cur = c.cursor()
        if q:
            like = f"%{q}%"
            cur.execute("SELECT * FROM pricing WHERE model LIKE ? ORDER BY model LIMIT 2000", (like,))
        else:
            cur.execute("SELECT * FROM pricing ORDER BY model LIMIT 2000")
        rows = cur.fetchall()
        if q:
            like = f"%{q}%"
            cur.execute(
                "SELECT * FROM pricing_eur WHERE sku LIKE ? OR ean LIKE ? ORDER BY sku LIMIT 2000",
                (like, like),
            )
        else:
            cur.execute("SELECT * FROM pricing_eur ORDER BY sku LIMIT 2000")
        eur_rows = cur.fetchall()
        c.close()

        tpl = r"""
        {% extends "base.html" %}
        {% block content %}
          {% if eur_imported %}
            <div class="card" style="border-color:#9ad9c4;background:#f0fff9;">
              <b>Cennik UE zapisany w Supabase: {{ eur_imported }} pozycji.</b>
            </div>
          {% endif %}
          {% if eur_import_error %}
            <div class="card" style="border-color:#f3b8b8;background:#fff4f4;">
              <b>Cennik zapisano lokalnie ({{ eur_local_saved }} pozycji), ale Supabase odrzucił synchronizację.</b>
              <div class="muted" style="margin-top:8px;word-break:break-word;">{{ eur_import_error }}</div>
            </div>
          {% endif %}
          <div class="card">
            <h1>Cennik</h1>
            <div class="muted">Import pliku cen (kolumny: model, netto, brutto). ObsĹ‚uga CSV i XLSX (jeĹ›li dostÄ™pny openpyxl).</div>
          </div>

          <div class="card">
            <h2>Import cennika</h2>
            <form method="post" action="{{ url_for('pricing_import') }}" enctype="multipart/form-data" class="row">
              <div>
                <input type="file" name="file" accept=".csv,.xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/csv" required>
              </div>
              <div class="flex" style="align-items:flex-end;">
                <button class="btn primary" type="submit">Importuj cennik</button>
              </div>
            </form>
          </div>

          <div class="card">
            <h2>Cennik UE — EUR</h2>
            <div class="muted" style="margin-bottom:12px;">
              Import arkusza Preisliste.xlsx. Wymagane kolumny: Articel (SKU), PREIS EUR i UVP; GTIN/EAN jest opcjonalny.
              Import nie zmienia polskiego cennika ani stanów magazynowych.
            </div>
            <form method="post" action="{{ url_for('pricing_eur_import') }}" enctype="multipart/form-data" class="row">
              <div>
                <input type="file" name="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" required>
              </div>
              <div class="flex" style="align-items:flex-end;">
                <button class="btn primary" type="submit">Importuj cennik UE</button>
              </div>
            </form>
            <div class="muted small" style="margin-top:10px;">Aktualnie zapisanych pozycji: {{ eur_rows|length }}</div>
          </div>

          <div class="card">
            <form method="get" class="grid3" style="margin-bottom:10px;">
              <input name="q" value="{{ q }}" placeholder="Szukaj modelu">
              <button class="btn primary" type="submit">Szukaj</button>
              <a class="btn" href="{{ url_for('pricing') }}">WyczyĹ›Ä‡</a>
            </form>
            <h2>Pozycje cennika</h2>
            <table>
              <thead><tr><th>Model</th><th>Netto</th><th>Brutto</th></tr></thead>
              <tbody>
                {% for r in rows %}
                  <tr>
                    <td><b>{{ r['model'] }}</b></td>
                    <td>{{ "%.2f"|format(r['net_price']) }}</td>
                    <td>{{ "%.2f"|format(r['gross_price']) }}</td>
                  </tr>
                {% endfor %}
                {% if not rows %}
                  <tr><td colspan="3" class="muted">Brak pozycji cennika.</td></tr>
                {% endif %}
              </tbody>
            </table>
          </div>

          <div class="card">
            <h2>Pozycje cennika UE</h2>
            <table>
              <thead><tr><th>SKU</th><th>EAN</th><th>Cena EUR</th><th>UVP EUR</th></tr></thead>
              <tbody>
                {% for r in eur_rows %}
                  <tr>
                    <td><b>{{ r['sku'] }}</b></td>
                    <td>{{ r['ean'] or '-' }}</td>
                    <td>{{ "%.2f"|format(r['price_eur']) }} EUR</td>
                    <td>{{ "%.2f"|format(r['uvp_eur']) }} EUR</td>
                  </tr>
                {% endfor %}
                {% if not eur_rows %}
                  <tr><td colspan="4" class="muted">Cennik UE nie został jeszcze zaimportowany.</td></tr>
                {% endif %}
              </tbody>
            </table>
          </div>
        {% endblock %}
        """
        return render_template_string(
            tpl,
            title="Cennik",
            base_url=BASE_URL,
            db_path=DB_PATH,
            rows=rows,
            eur_rows=eur_rows,
            q=q,
            eur_imported=eur_imported,
            eur_import_error=eur_import_error,
            eur_local_saved=eur_local_saved,
        )



    @app.post("/pricing/import")
    def pricing_import():
        f = request.files.get("file")
        if not f:
            return "Brak pliku", 400

        filename = norm(f.filename).lower()
        parsed_rows = []

        if filename.endswith(".xlsx"):
            try:
                from openpyxl import load_workbook
            except Exception:
                return "Brak biblioteki openpyxl do odczytu XLSX. UĹĽyj CSV albo doinstaluj openpyxl.", 400

            wb = load_workbook(f, data_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                return "Pusty plik", 400
            headers = [norm(x) for x in rows[0]]
            data = rows[1:]
            i_model = guess_col(headers, ["model"])
            i_sku = guess_col(headers, ["sku", "symbol", "index", "indeks", "kod", "code"])
            i_name = guess_col(headers, ["nazwa", "name", "produkt", "product"])
            i_ean = guess_col(headers, ["ean", "gtin"])
            i_net = guess_col(headers, ["netto", "net", "cena netto"])
            i_gross = guess_col(headers, ["brutto", "gross", "cena brutto"])
            if i_model is None or i_net is None or i_gross is None:
                return "Plik musi mieÄ‡ kolumny: model, netto, brutto", 400
            for r in data:
                if not r:
                    continue
                model = norm(r[i_model]) if len(r) > i_model else ""
                if not model:
                    continue
                sku = norm(r[i_sku]) if i_sku is not None and len(r) > i_sku else model
                name = norm(r[i_name]) if i_name is not None and len(r) > i_name else ""
                ean = norm(r[i_ean]) if i_ean is not None and len(r) > i_ean else ""
                net = to_float(r[i_net] if len(r) > i_net else "", 0.0)
                gross = to_float(r[i_gross] if len(r) > i_gross else "", 0.0)
                parsed_rows.append((sku, model, name, ean, net, gross))

        else:
            raw = f.read()
            try:
                text = raw.decode("utf-8-sig")
            except Exception:
                text = raw.decode("latin2", errors="replace")
            sample = text[:5000]
            delim = ";" if sample.count(";") >= sample.count(",") else ","
            rdr = csv.reader(io.StringIO(text), delimiter=delim)
            rows = list(rdr)
            if not rows:
                return "Pusty plik", 400
            headers = rows[0]
            data = rows[1:]
            i_model = guess_col(headers, ["model"])
            i_sku = guess_col(headers, ["sku", "symbol", "index", "indeks", "kod", "code"])
            i_name = guess_col(headers, ["nazwa", "name", "produkt", "product"])
            i_ean = guess_col(headers, ["ean", "gtin"])
            i_net = guess_col(headers, ["netto", "net", "cena netto"])
            i_gross = guess_col(headers, ["brutto", "gross", "cena brutto"])
            if i_model is None or i_net is None or i_gross is None:
                return "Plik musi mieÄ‡ kolumny: model, netto, brutto", 400
            for r in data:
                if not r:
                    continue
                model = norm(r[i_model]) if len(r) > i_model else ""
                if not model:
                    continue
                sku = norm(r[i_sku]) if i_sku is not None and len(r) > i_sku else model
                name = norm(r[i_name]) if i_name is not None and len(r) > i_name else ""
                ean = norm(r[i_ean]) if i_ean is not None and len(r) > i_ean else ""
                net = to_float(r[i_net] if len(r) > i_net else "", 0.0)
                gross = to_float(r[i_gross] if len(r) > i_gross else "", 0.0)
                parsed_rows.append((sku, model, name, ean, net, gross))

        c = conn()
        cur = c.cursor()
        changed_product_ids = []
        for sku, model, name, ean, net, gross in parsed_rows:
            cur.execute("""
              INSERT INTO pricing(model, net_price, gross_price, created_at)
              VALUES(?,?,?,?)
              ON CONFLICT(model) DO UPDATE SET
                net_price=excluded.net_price,
                gross_price=excluded.gross_price,
                created_at=excluded.created_at
            """, (model, net, gross, now_iso()))
            if sku:
                cur.execute("SELECT id FROM products WHERE sku=? LIMIT 1", (sku,))
                existing = cur.fetchone()
                if existing:
                    cur.execute("""
                      UPDATE products
                      SET model=COALESCE(NULLIF(?, ''), model),
                          ean=COALESCE(NULLIF(?, ''), ean),
                          name=COALESCE(NULLIF(?, ''), name),
                          archived=0
                      WHERE sku=?
                    """, (model, ean, name, sku))
                    pid = int(existing["id"])
                else:
                    cur.execute(
                        "INSERT INTO products(sku, model, ean, name, created_at) VALUES (?,?,?,?,?)",
                        (sku, model, ean, name, now_iso())
                    )
                    pid = int(cur.lastrowid)
                changed_product_ids.append(pid)
                cur.execute("INSERT OR IGNORE INTO stock(product_id, qty) VALUES (?, 0)", (pid,))
        c.commit()
        c.close()
        if supabase_enabled():
            try:
                sync_local_table_to_supabase("pricing", "model")
            except Exception:
                pass
            try:
                sync_local_rows_to_supabase("products", "id", changed_product_ids)
            except Exception:
                pass
            try:
                sync_local_rows_to_supabase("stock", "product_id", changed_product_ids)
            except Exception:
                pass
        return redirect(url_for("pricing"))




    @app.post("/pricing/eur/import")
    def pricing_eur_import():
        uploaded = request.files.get("file")
        if not uploaded or not norm(uploaded.filename).lower().endswith(".xlsx"):
            return "Wybierz plik XLSX z cennikiem UE", 400
        try:
            parsed_rows = parse_eur_pricing_xlsx(uploaded)
        except ValueError as exc:
            return str(exc), 400

        timestamp = now_iso()
        c = conn()
        try:
            cur = c.cursor()
            for sku, ean, price_eur, uvp_eur in parsed_rows:
                cur.execute(
                    """
                    INSERT INTO pricing_eur(sku, ean, price_eur, uvp_eur, created_at, updated_at)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(sku) DO UPDATE SET
                      ean=excluded.ean,
                      price_eur=excluded.price_eur,
                      uvp_eur=excluded.uvp_eur,
                      updated_at=excluded.updated_at
                    """,
                    (sku, ean, price_eur, uvp_eur, timestamp, timestamp),
                )
            c.commit()
        finally:
            c.close()

        if supabase_enabled():
            try:
                sync_local_table_to_supabase("pricing_eur", "sku")
            except Exception as exc:
                error_detail = norm(str(exc))[:600] or type(exc).__name__
                app.logger.exception("Nie udało się zsynchronizować cennika UE z Supabase")
                return redirect(url_for(
                    "pricing",
                    eur_local_saved=len(parsed_rows),
                    eur_import_error=error_detail,
                ))
        return redirect(url_for("pricing", eur_imported=len(parsed_rows)))




    @app.get("/products")
    def products():
        maybe_pull_shared_from_supabase()
        q = norm(request.args.get("q"))
        c = conn()
        cur = c.cursor()
        if q:
            like = f"%{q}%"
            cur.execute("""
              SELECT p.*, COALESCE(s.qty,0) AS stock
              FROM products p
              LEFT JOIN stock s ON s.product_id=p.id
              WHERE COALESCE(p.archived,0)=0
                AND (p.sku LIKE ? OR p.model LIKE ? OR p.ean LIKE ? OR p.name LIKE ?)
              ORDER BY p.sku
              LIMIT 1000
            """, (like, like, like, like))
        else:
            cur.execute("""
              SELECT p.*, COALESCE(s.qty,0) AS stock
              FROM products p
              LEFT JOIN stock s ON s.product_id=p.id
              WHERE COALESCE(p.archived,0)=0
              ORDER BY p.sku
              LIMIT 1000
            """)
        rows = cur.fetchall()
        c.close()

        tpl = r"""
        {% extends "base.html" %}
        {% block content %}
          <div class="card">
            <div class="flex">
              <h1 style="margin:0;">Produkty</h1>
              <div class="right"></div>
            </div>
            {% if request.args.get('product_deleted') %}<div class="notice" style="margin-top:10px;color:#067a2d;">Usunięto produkt {{ request.args.get('product_deleted') }}.</div>{% endif %}
            {% if request.args.get('product_error') %}<div class="notice" style="margin-top:10px;color:#b00020;">{{ request.args.get('product_error') }}</div>{% endif %}
            <form method="get" class="grid3" style="margin-top:10px;">
              <input name="q" value="{{ q }}" placeholder="Szukaj: SKU / model / EAN / nazwa">
              <button class="btn primary" type="submit">Szukaj</button>
              <a class="btn" href="{{ url_for('products') }}">WyczyĹ›Ä‡</a>
            </form>
          </div>

          <div class="card">
            <h2>Import CSV (478 pozycji)</h2>
            <div class="muted">Wybierz plik CSV z Excela. Minimalnie: kolumna SKU (unikalna). PozostaĹ‚e: model, ean, name/nazwa.</div>
            <form method="post" action="{{ url_for('products_import') }}" enctype="multipart/form-data" class="row" style="margin-top:10px;">
              <div>
                <input type="file" name="file" accept=".csv,text/csv" required>
                <div class="muted small" style="margin-top:6px;">Kodowanie: najlepiej UTF-8. Separator zwykle â€ž;â€ť lub â€ž,â€ť â€“ program sam sprĂłbuje.</div>
              </div>
              <div class="flex" style="align-items:flex-end;">
                <button class="btn primary" type="submit">Importuj</button>
              </div>
            </form>
          </div>

          <div class="card">
            <h2>Lista (max 1000)</h2>
            <table>
              <thead>
                <tr>
                  <th>SKU</th>
                  <th>Model</th>
                  <th>EAN</th>
                  <th>Nazwa</th>
                  <th>Stan</th>
                  <th>Akcje</th>
                </tr>
              </thead>
              <tbody>
                {% for r in rows %}
                <tr>
                  <td><b>{{ r["sku"] }}</b></td>
                  <td>{{ r["model"] or "" }}</td>
                  <td>{{ r["ean"] or "" }}</td>
                  <td>{{ r["name"] or "" }}</td>
                  <td><span class="badge">{{ r["stock"] }}</span></td>
                  <td>
                    <form method="post" action="{{ url_for('product_delete', product_id=r['id']) }}" onsubmit="return confirm('Usunąć wybrany produkt? Tej operacji nie można cofnąć.');">
                      <button class="btn danger" type="submit">Usuń</button>
                    </form>
                  </td>
                </tr>
                {% endfor %}
                {% if not rows %}
                  <tr><td colspan="5" class="muted">Brak produktĂłw. ZrĂłb import CSV.</td></tr>
                {% endif %}
              </tbody>
            </table>
          </div>
        {% endblock %}
        """
        return render_template_string(tpl, title="Produkty", base_url=BASE_URL, db_path=DB_PATH, rows=rows, q=q)



    @app.post("/products/<int:product_id>/delete")
    def product_delete(product_id):
        c = conn()
        cur = c.cursor()
        cur.execute("""
          SELECT p.id, p.sku, COALESCE(s.qty,0) AS stock
          FROM products p
          LEFT JOIN stock s ON s.product_id=p.id
          WHERE p.id=?
        """, (product_id,))
        product = cur.fetchone()
        if not product:
            c.close()
            return redirect(url_for("products", product_error="Nie znaleziono produktu."))

        # Usuwanie z katalogu jest archiwizacją produktu. Historyczne dokumenty
        # nadal wskazują ten sam stabilny identyfikator, ale produkt nie jest już
        # dostępny w magazynie, panelu klienta ani formularzach nowych dokumentów.
        active_references = []
        cur.execute("""
          SELECT COUNT(*) AS n
          FROM order_items oi
          JOIN orders o ON o.id=oi.order_id
          WHERE oi.product_id=?
            AND LOWER(COALESCE(o.status,'')) IN
                ('new','pending','unconfirmed','confirmed','packed','in_delivery','shipped')
            AND COALESCE(o.warehouse_issued,0)=0
        """, (product_id,))
        if int(cur.fetchone()["n"] or 0) > 0:
            active_references.append("aktywnych zamówieniach")
        cur.execute("""
          SELECT COUNT(*) AS n
          FROM china_items ci
          JOIN china_packages cp ON cp.id=ci.package_id
          WHERE ci.product_id=?
            AND LOWER(COALESCE(cp.status,'')) IN ('planned','ordered','shipped')
        """, (product_id,))
        if int(cur.fetchone()["n"] or 0) > 0:
            active_references.append("aktywnych dostawach P/O")
        if int(product["stock"] or 0) != 0:
            active_references.append("niezerowym stanie magazynowym")

        sku = product["sku"]
        if active_references:
            c.close()
            return redirect(url_for(
                "products",
                q=sku,
                product_error="Nie można usunąć produktu, ponieważ jest używany w " + ", ".join(active_references) + ".",
            ))

        try:
            if supabase_enabled():
                # Kolejność ma znaczenie: najpierw ukrycie produktu, następnie dane
                # aktywnego katalogu. Stare order_items i invoice_allocations zostają.
                supabase_update_rows("products", {"archived": True}, {"id": product_id})
                supabase_delete_rows("stock", {"product_id": product_id})
                try:
                    supabase_delete_rows("pricing_eur", {"sku": sku})
                except Exception:
                    app.logger.warning("Nie udało się usunąć ceny EUR dla SKU %s", sku, exc_info=True)

            cur.execute("UPDATE products SET archived=1 WHERE id=?", (product_id,))
            cur.execute("DELETE FROM stock WHERE product_id=?", (product_id,))
            cur.execute("DELETE FROM pricing_eur WHERE sku=?", (sku,))
            c.commit()
        except Exception:
            c.rollback()
            c.close()
            app.logger.exception("Nie udało się zarchiwizować produktu %s", product_id)
            return redirect(url_for(
                "products",
                q=sku,
                product_error="Nie udało się usunąć produktu z aktywnego magazynu. Najpierw uruchom migrację Supabase.",
            ))
        c.close()
        return redirect(url_for("products", product_deleted=sku))

        references = []
        for table, label in (
            ("order_items", "zamówieniach"),
            ("china_items", "dostawach P/O"),
            ("invoice_allocations", "fakturach"),
        ):
            cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE product_id=?", (product_id,))
            if int(cur.fetchone()["n"] or 0) > 0:
                references.append(label)
        if int(product["stock"] or 0) != 0:
            references.append("stanie magazynowym")

        sku = product["sku"]
        if references:
            c.close()
            return redirect(url_for(
                "products",
                q=sku,
                product_error="Nie można usunąć produktu, ponieważ jest używany w " + ", ".join(references) + ".",
            ))

        try:
            if supabase_enabled():
                supabase_delete_rows("stock", {"product_id": product_id})
                try:
                    supabase_delete_rows("products", {"id": product_id})
                except Exception:
                    cur.execute("SELECT * FROM stock WHERE product_id=?", (product_id,))
                    stock_row = cur.fetchone()
                    if stock_row:
                        supabase_upsert_rows("stock", [dict(stock_row)], "product_id")
                    raise
                try:
                    supabase_delete_rows("pricing_eur", {"sku": sku})
                except Exception:
                    # Brak osobnej ceny EUR nie może cofnąć poprawnego usunięcia
                    # produktu. Osierocona cena nie jest widoczna bez produktu.
                    app.logger.warning("Nie udało się usunąć ceny EUR dla SKU %s", sku, exc_info=True)

            cur.execute("DELETE FROM pricing_eur WHERE sku=?", (sku,))
            cur.execute("DELETE FROM stock WHERE product_id=?", (product_id,))
            cur.execute("DELETE FROM products WHERE id=?", (product_id,))
            c.commit()
        except Exception:
            c.rollback()
            c.close()
            app.logger.exception("Nie udało się bezpiecznie usunąć produktu %s", product_id)
            return redirect(url_for("products", q=sku, product_error="Nie udało się usunąć produktu. Sprawdź synchronizację magazynu."))
        c.close()
        return redirect(url_for("products", product_deleted=sku))



    @app.post("/products/import")
    def products_import():
        f = request.files.get("file")
        if not f:
            return "Brak pliku", 400

        raw = f.read()
        # SprĂłbuj UTF-8, jak nie pĂłjdzie to latin2
        try:
            text = raw.decode("utf-8-sig")
        except:
            text = raw.decode("latin2", errors="replace")

        # SprĂłbuj wykryÄ‡ delimiter
        sample = text[:5000]
        delim = ";" if sample.count(";") >= sample.count(",") else ","

        rdr = csv.reader(io.StringIO(text), delimiter=delim)
        rows = list(rdr)
        if not rows:
            return "Pusty CSV", 400

        headers = rows[0]
        data = rows[1:]

        i_sku = guess_col(headers, ["sku", "symbol", "index", "indeks", "kod", "code"])
        i_model = guess_col(headers, ["model", "model_uchwytu", "nazwa_modelu"])
        i_ean = guess_col(headers, ["ean", "gtin"])
        i_name = guess_col(headers, ["name", "nazwa", "produkt", "product"])

        if i_sku is None:
            return "CSV musi mieÄ‡ kolumnÄ™ SKU / Symbol / Indeks", 400

        c = conn()
        cur = c.cursor()
        added = 0
        updated = 0

        for row in data:
            if not row or len(row) <= i_sku:
                continue
            sku = norm(row[i_sku])
            if not sku:
                continue
            model = norm(row[i_model]) if i_model is not None and len(row) > i_model else ""
            ean = norm(row[i_ean]) if i_ean is not None and len(row) > i_ean else ""
            name = norm(row[i_name]) if i_name is not None and len(row) > i_name else ""

            cur.execute("SELECT id FROM products WHERE sku=?", (sku,))
            exists = cur.fetchone()
            if exists:
                cur.execute("UPDATE products SET model=?, ean=?, name=?, archived=0 WHERE sku=?", (model, ean, name, sku))
                updated += 1
                pid = exists["id"]
            else:
                cur.execute(
                    "INSERT INTO products(sku, model, ean, name, created_at) VALUES (?,?,?,?,?)",
                    (sku, model, ean, name, now_iso())
                )
                pid = cur.lastrowid
                added += 1

            cur.execute("INSERT OR IGNORE INTO stock(product_id, qty) VALUES (?, 0)", (pid,))

        c.commit()
        changed_ids = [int(r["id"]) for r in cur.execute(
            "SELECT id FROM products WHERE COALESCE(archived,0)=0"
        ).fetchall()]
        c.close()

        if supabase_enabled() and changed_ids:
            try:
                sync_local_rows_to_supabase("products", "id", changed_ids)
                sync_local_rows_to_supabase("stock", "product_id", changed_ids)
            except Exception:
                app.logger.warning("Nie udało się zsynchronizować importu produktów", exc_info=True)

        return redirect(url_for("products", q=""))




    @app.get("/stock")
    def stock():
        maybe_pull_shared_from_supabase()
        q = norm(request.args.get("q"))
        rows = build_replenishment_analysis(conn, today=app_now().date(), horizon_days=60)
        stock_total = sum(to_int(row.get("stock_qty"), 0) for row in rows)
        stock_with_china_total = sum(
            to_int(row.get("stock_qty"), 0) + to_int(row.get("incoming_qty"), 0)
            for row in rows
        )
        reserved_total = sum(
            to_int(row.get("reserved_qty"), 0) + to_int(row.get("reserved_incoming"), 0)
            for row in rows
        )
        if q:
            query = q.casefold()
            rows = [
                row for row in rows
                if any(query in norm(row.get(field)).casefold() for field in ("sku", "model", "ean", "name"))
            ]
        rows = sorted(rows, key=lambda row: norm(row.get("sku")).casefold())[:1000]

        tpl = r"""
        {% extends "base.html" %}
        {% block content %}
          <style>
            .stock-summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin-bottom:16px}
            .stock-summary-card{background:#fff;border:1px solid #e7eaf2;border-radius:22px;padding:18px 20px;box-shadow:var(--shadow)}
            .stock-summary-card span{display:block;color:#718096;font-size:12px;font-weight:700}
            .stock-summary-card b{display:block;margin-top:5px;color:#17233c;font-size:28px;letter-spacing:-.6px}
            .stock-summary-card small{display:block;margin-top:4px;color:#2da176;font-size:11px}
            @media(max-width:760px){.stock-summary{grid-template-columns:1fr}}
          </style>

          <div class="stock-summary">
            <div class="stock-summary-card">
              <span>Na stanie łącznie</span>
              <b>{{ stock_total }} szt.</b>
              <small>Fizycznie w magazynie</small>
            </div>
            <div class="stock-summary-card">
              <span>Na stanie + CHINY</span>
              <b>{{ stock_with_china_total }} szt.</b>
              <small>Magazyn oraz towar w drodze</small>
            </div>
            <div class="stock-summary-card">
              <span>W zamówieniach</span>
              <b>{{ reserved_total }} szt.</b>
              <small>Rezerwacje aktywnych zamówień</small>
            </div>
          </div>

          <div class="card">
            <div class="flex">
              <h1 style="margin:0;">Magazyn</h1>
            </div>
            <form method="get" class="grid3" style="margin-top:10px;">
              <input name="q" value="{{ q }}" placeholder="Szukaj produktu: SKU / model / EAN / nazwa">
              <button class="btn primary" type="submit">Szukaj</button>
              <a class="btn" href="{{ url_for('stock') }}">WyczyĹ›Ä‡</a>
            </form>
          </div>

          <div class="card">
            <h2>Korekta stanu</h2>
            <div class="row">
              <div>
                <label class="muted small">Produkt (SKU)</label>
                <input list="skuList" id="skuInput" placeholder="np. CH010-BB-N28">
                <datalist id="skuList">
                  {% for r in rows %}
                    <option value="{{ r['sku'] }}">{{ r['sku'] }}</option>
                  {% endfor %}
                </datalist>
              </div>
              <div>
                <label class="muted small">Zmiana (np. +10 albo -3)</label>
                <input id="deltaInput" placeholder="+10">
              </div>
            </div>
            <div class="flex" style="margin-top:10px;">
              <button class="btn ok" onclick="applyDelta(); return false;">Zapisz korektÄ™</button>
              <div class="muted" id="deltaMsg"></div>
            </div>
          </div>

          <div class="card">
            <h2>Stany (max 1000)</h2>
            <div class="muted" style="margin-bottom:8px;">
              Rezerwacje obejmują wszystkie aktywne, niewydane zamówienia. Jeżeli bieżący stan nie wystarcza, brakująca część rezerwuje towar w drodze.
            </div>
            <table>
              <thead>
                <tr><th>SKU</th><th>Model</th><th>EAN</th><th>Nazwa</th><th>Stan</th><th>Rezerwacje</th><th>Dostępne</th><th>W drodze</th><th>Zarezerwowane w drodze</th><th>Dostępne w drodze</th></tr>
              </thead>
              <tbody>
                {% for r in rows %}
                  <tr>
                    <td><b>{{ r['sku'] }}</b></td>
                    <td>{{ r['model'] or "" }}</td>
                    <td>{{ r['ean'] or "" }}</td>
                    <td>{{ r['name'] or "" }}</td>
                    <td><span class="badge">{{ r['stock_qty'] }}</span></td>
                    <td><span class="badge">{{ r['reserved_qty'] }}</span></td>
                    <td><span class="badge">{{ r['available_qty'] }}</span></td>
                    <td><span class="badge">{{ r['incoming_qty'] }}</span></td>
                    <td><span class="badge">{{ r['reserved_incoming'] }}</span></td>
                    <td><span class="badge">{{ r['available_incoming'] }}</span></td>
                  </tr>
                {% endfor %}
                {% if not rows %}
                  <tr><td colspan="10" class="muted">Brak produktów.</td></tr>
                {% endif %}
              </tbody>
            </table>
          </div>

    <script>
    async function applyDelta(){
      const sku = document.getElementById("skuInput").value.trim();
      const delta = document.getElementById("deltaInput").value.trim();
      const msg = document.getElementById("deltaMsg");
      msg.innerText = "";
      if(!sku){ msg.innerText = "Podaj SKU"; return; }
      if(!delta){ msg.innerText = "Podaj zmianÄ™"; return; }

      const r = await fetch("/api/stock_delta", {
        method:"POST",
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({sku, delta})
      });
      const j = await r.json();
      if(!j.ok){ msg.innerText = "BĹ‚Ä…d: " + (j.error || ""); return; }
      msg.innerText = "OK. Nowy stan: " + j.new_qty;
      setTimeout(()=>location.reload(), 500);
    }
    </script>

        {% endblock %}
        """
        return render_template_string(
            tpl,
            title="Magazyn",
            base_url=BASE_URL,
            db_path=DB_PATH,
            rows=rows,
            q=q,
            stock_total=stock_total,
            stock_with_china_total=stock_with_china_total,
            reserved_total=reserved_total,
        )



    @app.post("/api/stock_delta")
    def api_stock_delta():
        data = request.get_json(force=True, silent=True) or {}
        sku = norm(data.get("sku"))
        delta_raw = norm(data.get("delta"))

        if not sku:
            return jsonify(ok=False, error="Brak SKU"), 400

        delta = to_int(delta_raw, None)
        if delta is None:
            # sprĂłbuj +10 / -3
            try:
                delta = int(delta_raw)
            except:
                return jsonify(ok=False, error="NieprawidĹ‚owa zmiana (np. +10 lub -3)"), 400

        c = conn()
        cur = c.cursor()
        cur.execute("SELECT id FROM products WHERE sku=? AND COALESCE(archived,0)=0", (sku,))
        p = cur.fetchone()
        if not p:
            c.close()
            return jsonify(ok=False, error="Nie ma takiego SKU"), 404
        pid = p["id"]
        cur.execute("INSERT OR IGNORE INTO stock(product_id, qty) VALUES (?, 0)", (pid,))
        cur.execute("UPDATE stock SET qty = qty + ? WHERE product_id=?", (delta, pid))
        cur.execute("SELECT qty FROM stock WHERE product_id=?", (pid,))
        new_qty = cur.fetchone()["qty"]
        c.commit()
        c.close()
        return jsonify(ok=True, new_qty=new_qty)



    @app.get("/api/product/<int:product_id>")
    def api_product(product_id):
        c = conn()
        cur = c.cursor()
        cur.execute("""
          SELECT p.*, COALESCE(s.qty,0) AS stock
          FROM products p
          LEFT JOIN stock s ON s.product_id=p.id
          WHERE p.id=? AND COALESCE(p.archived,0)=0
        """, (product_id,))
        r = cur.fetchone()
        c.close()
        if not r:
            return jsonify(ok=False), 404
        return jsonify(ok=True, id=r["id"], sku=r["sku"], model=r["model"], ean=r["ean"], name=r["name"], stock=r["stock"])



    exported = {'pricing': pricing, 'pricing_import': pricing_import, 'pricing_eur_import': pricing_eur_import, 'products': products, 'product_delete': product_delete, 'products_import': products_import, 'stock': stock, 'api_stock_delta': api_stock_delta, 'api_product': api_product}
    globals().update(exported)
    return exported
