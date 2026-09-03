"""Mechanically extracted Flask routes; business logic is unchanged."""

def register_routes(context):
    globals().update(context)


    @app.get("/china")
    def china():
        # WyĹ‚Ä…czony pull z Supabase tylko dla moduĹ‚u Chiny.
        # Tu pracujemy na lokalnej bazie, ĹĽeby POST -> redirect nie cofaĹ‚ zmian.
        c = conn()
        cur = c.cursor()
        cur.execute("SELECT * FROM china_packages ORDER BY id DESC LIMIT 200")
        packs = cur.fetchall()
        c.close()

        tpl = r"""
        {% extends "base.html" %}
        {% block content %}
          <div class="card">
            <div class="flex">
              <h1 style="margin:0;">Chiny (P/O)</h1>
            </div>
            <div class="muted">ZarzÄ…dzaj przesyĹ‚kami: status, tracking i zawartoĹ›Ä‡ paczki. Tracking otwiera 17TRACK.</div>
          </div>

          <div class="card">
            <h2>Nowa paczka</h2>
            <form method="post" action="{{ url_for('china_create') }}" class="row">
              <div>
                <label class="muted small">Numer paczki / P/O</label>
                <input name="package_no" placeholder="np. PO-2026-02-01" required>
              </div>
              <div>
                <label class="muted small">Tracking</label>
                <input name="tracking" placeholder="UPS / DHL...">
              </div>
              <div>
                <label class="muted small">Status</label>
                <select name="status">
                  <option value="planned">planned</option>
                  <option value="ordered">ordered</option>
                  <option value="shipped">shipped</option>
                  <option value="arrived">arrived</option>
                </select>
              </div>
              <div>
                <label class="muted small">Notatka</label>
                <input name="note">
              </div>
              <div>
                <label class="muted small">Koszt paczki / P/O (PLN)</label>
                <input type="number" name="cost_amount" min="0.01" step="0.01" required>
              </div>
              <div>
                <label class="muted small">Numer dokumentu kosztowego</label>
                <input name="cost_document_no" placeholder="domyślnie numer P/O">
              </div>
              <div class="flex" style="align-items:flex-end;">
                <button class="btn primary" type="submit">Zapisz</button>
              </div>
            </form>
          </div>

          <div class="card">
            <h2>Paczki (max 200)</h2>
            <table>
              <thead>
                <tr><th>Nr</th><th>Status</th><th>Tracking</th><th>Koszt</th><th>Dokument</th><th>Notatka</th><th>Data</th><th>Akcje</th></tr>
              </thead>
              <tbody>
                {% for p in packs %}
                  <tr>
                    <td><b>{{ p['package_no'] }}</b></td>
                    <td>
                      <form method="post" action="{{ url_for('china_status', package_id=p['id']) }}" class="flex">
                        <select name="status" style="width:140px;">
                          <option value="planned" {% if p['status']=='planned' %}selected{% endif %}>planned</option>
                          <option value="ordered" {% if p['status']=='ordered' %}selected{% endif %}>ordered</option>
                          <option value="shipped" {% if p['status']=='shipped' %}selected{% endif %}>shipped</option>
                          <option value="arrived" {% if p['status']=='arrived' %}selected{% endif %}>arrived</option>
                        </select>
                        <button class="btn" type="submit">ZmieĹ„</button>
                      </form>
                    </td>
                    <td>
                      <form method="post" action="{{ url_for('china_tracking', package_id=p['id']) }}" class="flex">
                        <input name="tracking" value="{{ p['tracking'] or '' }}" placeholder="nr trackingu" style="width:180px;">
                        <button class="btn" type="submit">Zapisz</button>
                        {% if p['tracking'] %}
                          <a class="btn" target="_blank" href="https://t.17track.net/en#nums={{ p['tracking']|urlencode }}">17TRACK</a>
                        {% endif %}
                      </form>
                    </td>
                    <td><b>{{ "%.2f"|format(p['cost_amount'] or 0) }} PLN</b></td>
                    <td>{{ p['cost_document_no'] or p['package_no'] }}</td>
                    <td>{{ p['note'] or "-" }}</td>
                    <td class="muted">{{ p['created_at'] }}</td>
                    <td class="flex">
                      <a class="btn primary" href="{{ url_for('china_package', package_id=p['id']) }}">ZawartoĹ›Ä‡</a>
                      <form method="post" action="{{ url_for('china_delete', package_id=p['id']) }}" onsubmit="return confirm('UsunÄ…Ä‡ paczkÄ™?')">
                        <button class="btn danger" type="submit">UsuĹ„</button>
                      </form>
                    </td>
                  </tr>
                {% endfor %}
                {% if not packs %}
                  <tr><td colspan="8" class="muted">Brak paczek.</td></tr>
                {% endif %}
              </tbody>
            </table>
          </div>
        {% endblock %}
        """
        return render_template_string(tpl, title="Chiny (P/O)", base_url=BASE_URL, db_path=DB_PATH, packs=packs)



    @app.post("/china/create")
    def china_create():
        package_no = norm(request.form.get("package_no"))
        status = norm(request.form.get("status")) or "planned"
        tracking = norm(request.form.get("tracking"))
        note = norm(request.form.get("note"))
        cost_amount = to_float(request.form.get("cost_amount"), 0)
        cost_document_no = norm(request.form.get("cost_document_no")) or package_no

        if not package_no or cost_amount <= 0:
            return "Podaj numer P/O oraz koszt większy od zera", 400

        c = conn()
        cur = c.cursor()
        try:
            cur.execute("""
              INSERT INTO china_packages(package_no, status, tracking, note, cost_amount, cost_document_no, created_at)
              VALUES(?,?,?,?,?,?,?)
            """, (package_no, status, tracking, note, cost_amount, cost_document_no, now_iso()))
            c.commit()
        except sqlite3.IntegrityError:
            pass
        finally:
            c.close()

        return redirect(url_for("china"))



    @app.post("/china/<int:package_id>/status")
    def china_status(package_id):
        status = norm(request.form.get("status"))
        if status not in {"planned", "ordered", "shipped", "arrived"}:
            return "NieprawidĹ‚owy status", 400

        c = conn()
        cur = c.cursor()

        cur.execute("SELECT status FROM china_packages WHERE id=?", (package_id,))
        pack = cur.fetchone()
        if not pack:
            c.close()
            abort(404)

        old_status = pack["status"]

        cur.execute("SELECT product_id, qty FROM china_items WHERE package_id=?", (package_id,))
        items = cur.fetchall()

        # PrzejĹ›cie NA arrived: fizycznie przyjÄ™to towar -> dodaj na stan.
        if old_status != "arrived" and status == "arrived":
            for it in items:
                pid = it["product_id"]
                qty = int(it["qty"])
                cur.execute("INSERT OR IGNORE INTO stock(product_id, qty) VALUES (?, 0)", (pid,))
                cur.execute("UPDATE stock SET qty = qty + ? WHERE product_id=?", (qty, pid))

        # CofniÄ™cie Z arrived na inny status: towar wraca jako "w drodze" -> odejmij ze stanu.
        elif old_status == "arrived" and status != "arrived":
            for it in items:
                pid = it["product_id"]
                qty = int(it["qty"])
                cur.execute("INSERT OR IGNORE INTO stock(product_id, qty) VALUES (?, 0)", (pid,))
                cur.execute("UPDATE stock SET qty = qty - ? WHERE product_id=?", (qty, pid))

        cur.execute("UPDATE china_packages SET status=? WHERE id=?", (status, package_id))
        c.commit()
        c.close()
        return redirect(url_for("china"))



    @app.post("/china/<int:package_id>/tracking")
    def china_tracking(package_id):
        tracking = norm(request.form.get("tracking"))

        c = conn()
        cur = c.cursor()
        cur.execute("SELECT id FROM china_packages WHERE id=?", (package_id,))
        if not cur.fetchone():
            c.close()
            abort(404)

        cur.execute("UPDATE china_packages SET tracking=? WHERE id=?", (tracking, package_id))
        c.commit()
        c.close()

        ref = request.referrer or ""
        if ref.endswith(f"/china/{package_id}"):
            return redirect(url_for("china_package", package_id=package_id))
        return redirect(url_for("china"))



    @app.post("/china/<int:package_id>/cost")
    def china_cost(package_id):
        cost_amount = to_float(request.form.get("cost_amount"), 0)
        cost_document_no = norm(request.form.get("cost_document_no"))
        if cost_amount <= 0 or not cost_document_no:
            return redirect(url_for("china_package", package_id=package_id, cost_error=1))

        c = conn()
        cur = c.cursor()
        cur.execute("SELECT * FROM china_packages WHERE id=?", (package_id,))
        pack = cur.fetchone()
        if not pack:
            c.close()
            abort(404)
        cur.execute(
            "UPDATE china_packages SET cost_amount=?, cost_document_no=? WHERE id=?",
            (cost_amount, cost_document_no, package_id),
        )
        c.commit()
        cur.execute("SELECT * FROM china_packages WHERE id=?", (package_id,))
        cloud_row = dict(cur.fetchone())
        c.close()
        if supabase_enabled():
            supabase_upsert_rows("china_packages", [cloud_row], "id")
        return redirect(url_for("china_package", package_id=package_id, cost_saved=1))



    @app.get("/china/<int:package_id>")
    def china_package(package_id):
        # WyĹ‚Ä…czony pull z Supabase tylko dla moduĹ‚u Chiny.
        c = conn()
        cur = c.cursor()
        cur.execute("SELECT * FROM china_packages WHERE id=?", (package_id,))
        pack = cur.fetchone()
        if not pack:
            c.close()
            abort(404)

        cur.execute("SELECT id, sku, model, name FROM products WHERE COALESCE(archived,0)=0 ORDER BY sku LIMIT 5000")
        products_rows = cur.fetchall()

        cur.execute("""
          SELECT ci.*, p.model, p.name
          FROM china_items ci
          JOIN products p ON p.id=ci.product_id
          WHERE ci.package_id=?
          ORDER BY ci.id DESC
        """, (package_id,))
        items = cur.fetchall()
        c.close()

        tpl = r"""
        {% extends "base.html" %}
        {% block content %}
          <div class="card">
            <div class="flex">
              <h1 style="margin:0;">Paczka {{ pack['package_no'] }}</h1>
              <span class="badge">{{ pack['status'] }}</span>
              <a class="btn right" href="{{ url_for('china') }}">â† Lista paczek</a>
            </div>
            <div class="muted">Tracking: {{ pack['tracking'] or '-' }}</div>
            <form method="post" action="{{ url_for('china_tracking', package_id=pack['id']) }}" class="flex" style="margin-top:10px;">
              <input name="tracking" value="{{ pack['tracking'] or '' }}" placeholder="nr trackingu" style="width:260px;">
              <button class="btn" type="submit">ZmieĹ„ tracking</button>
              {% if pack['tracking'] %}
                <a class="btn" target="_blank" href="https://t.17track.net/en#nums={{ pack['tracking']|urlencode }}">OtwĂłrz 17TRACK</a>
              {% endif %}
            </form>
            <form method="post" action="{{ url_for('china_cost', package_id=pack['id']) }}" class="flex" style="margin-top:10px;align-items:flex-end;">
              <div>
                <label class="muted small">Koszt paczki / P/O (PLN)</label>
                <input type="number" name="cost_amount" min="0.01" step="0.01" value="{{ pack['cost_amount'] or '' }}" required style="width:220px;">
              </div>
              <div>
                <label class="muted small">Numer dokumentu kosztowego</label>
                <input name="cost_document_no" value="{{ pack['cost_document_no'] or pack['package_no'] }}" required style="width:280px;">
              </div>
              <button class="btn primary" type="submit">Zapisz koszt</button>
              {% if request.args.get('cost_saved') %}<span class="badge">Koszt zapisany</span>{% endif %}
              {% if request.args.get('cost_error') %}<span class="muted" style="color:#b00020;">Podaj kwotę większą od zera i numer dokumentu.</span>{% endif %}
            </form>
          </div>

          <div class="card">
            <h2>Dodaj zawartoĹ›Ä‡ paczki</h2>
            <form method="post" action="{{ url_for('china_item_add', package_id=pack['id']) }}" class="items-row">
              <div>
                <label class="muted small">Produkt</label>
                <select name="product_id" required>
                  <option value="">-- wybierz --</option>
                  {% for p in products %}
                    <option value="{{ p['id'] }}">{{ p['sku'] }}{% if p['model'] %} â€˘ {{ p['model'] }}{% endif %}{% if p['name'] %} â€˘ {{ p['name'] }}{% endif %}</option>
                  {% endfor %}
                </select>
              </div>
              <div>
                <label class="muted small">IloĹ›Ä‡</label>
                <input name="qty" value="1" required>
              </div>
              <div class="flex" style="align-items:flex-end;">
                <button class="btn primary" type="submit">Dodaj</button>
              </div>
            </form>
          </div>

          <div class="card">
            <h2>ZawartoĹ›Ä‡ paczki</h2>
            <table>
              <thead>
                <tr><th>SKU</th><th>Model / Nazwa</th><th>IloĹ›Ä‡</th><th>Data</th><th>Akcje</th></tr>
              </thead>
              <tbody>
                {% for it in items %}
                  <tr>
                    <td><b>{{ it['sku'] }}</b></td>
                    <td>{{ it['model'] or '' }}{% if it['name'] %}<div class="muted">{{ it['name'] }}</div>{% endif %}</td>
                    <td><span class="badge">{{ it['qty'] }}</span></td>
                    <td class="muted">{{ it['created_at'] }}</td>
                    <td>
                      <form method="post" action="{{ url_for('china_item_delete', package_id=pack['id'], item_id=it['id']) }}" onsubmit="return confirm('UsunÄ…Ä‡ pozycjÄ™?')">
                        <button class="btn danger" type="submit">UsuĹ„</button>
                      </form>
                    </td>
                  </tr>
                {% endfor %}
                {% if not items %}
                  <tr><td colspan="5" class="muted">Brak pozycji w paczce.</td></tr>
                {% endif %}
              </tbody>
            </table>
          </div>
        {% endblock %}
        """
        return render_template_string(tpl, title=f"Paczka {pack['package_no']}", base_url=BASE_URL, db_path=DB_PATH,
                                      pack=pack, products=products_rows, items=items)




    @app.post("/china/<int:package_id>/delete")
    def china_delete(package_id):
        c = conn()
        cur = c.cursor()
        cur.execute("SELECT status FROM china_packages WHERE id=?", (package_id,))
        pack = cur.fetchone()
        if not pack:
            c.close()
            abort(404)

        if norm(pack["status"]).lower() == "arrived":
            c.close()
            return "Nie moĹĽna usunÄ…Ä‡ paczki ARRIVED", 400

        if supabase_enabled():
            try:
                cur.execute("SELECT id FROM china_items WHERE package_id=?", (package_id,))
                item_ids = [int(r["id"]) for r in cur.fetchall()]
                for iid in item_ids:
                    supabase_delete_rows("china_items", {"id": iid})
                supabase_delete_rows("china_packages", {"id": package_id})
            except Exception:
                pass

        cur.execute("DELETE FROM china_items WHERE package_id=?", (package_id,))
        cur.execute("DELETE FROM china_packages WHERE id=?", (package_id,))
        c.commit()
        c.close()
        return redirect(url_for("china"))



    @app.post("/china/<int:package_id>/items/add")
    def china_item_add(package_id):
        product_id = to_int(request.form.get("product_id"), 0)
        qty = to_int(request.form.get("qty"), 0)
        if product_id <= 0 or qty <= 0:
            return "NieprawidĹ‚owy produkt lub iloĹ›Ä‡", 400

        c = conn()
        cur = c.cursor()
        cur.execute("SELECT sku FROM products WHERE id=? AND COALESCE(archived,0)=0", (product_id,))
        p = cur.fetchone()
        if not p:
            c.close()
            return "Produkt nie istnieje", 404

        cur.execute("SELECT id FROM china_packages WHERE id=?", (package_id,))
        if not cur.fetchone():
            c.close()
            return "Paczka nie istnieje", 404

        cur.execute(
            "INSERT INTO china_items(package_id, product_id, sku, qty, created_at) VALUES (?,?,?,?,?)",
            (package_id, product_id, p["sku"], qty, now_iso())
        )
        c.commit()
        c.close()
        return redirect(url_for("china_package", package_id=package_id))



    @app.post("/china/<int:package_id>/items/<int:item_id>/delete")
    def china_item_delete(package_id, item_id):
        if supabase_enabled():
            supabase_delete_rows("china_items", {"id": item_id})

        c = conn()
        cur = c.cursor()
        cur.execute("DELETE FROM china_items WHERE id=? AND package_id=?", (item_id, package_id))
        c.commit()
        c.close()
        return redirect(url_for("china_package", package_id=package_id))



    exported = {'china': china, 'china_create': china_create, 'china_status': china_status, 'china_tracking': china_tracking, 'china_cost': china_cost, 'china_package': china_package, 'china_delete': china_delete, 'china_item_add': china_item_add, 'china_item_delete': china_item_delete}
    globals().update(exported)
    return exported
