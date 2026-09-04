"""Mechanically extracted Flask routes; business logic is unchanged."""

def register_routes(context):
    globals().update(context)

    valid_statuses = {"planned", "ordered", "shipped", "arrived", "problem"}

    def tracking_enabled():
        return seventeentrack_is_enabled(SEVENTEENTRACK_ENABLED, SEVENTEENTRACK_API_KEY)

    def receive_into_stock(cur, package_id, current_status):
        """Przyjmuje P/O dokładnie raz; historycznego arrived nie dotyka."""
        row = cur.execute("SELECT warehouse_received FROM china_packages WHERE id=?", (package_id,)).fetchone()
        if not row or row["warehouse_received"] == 1:
            return []
        if cur.execute("SELECT 1 FROM china_stock_receipts WHERE package_id=?", (package_id,)).fetchone():
            cur.execute("UPDATE china_packages SET status='arrived',warehouse_received=1 WHERE id=?", (package_id,))
            return []
        if norm(current_status).lower() == "arrived" and row["warehouse_received"] is None:
            return []
        items = cur.execute("SELECT product_id,qty FROM china_items WHERE package_id=?", (package_id,)).fetchall()
        if not items:
            return None
        received_at = now_iso()
        receipt_payload = json.dumps(
            [{"product_id": int(item["product_id"]), "qty": int(item["qty"])} for item in items],
            ensure_ascii=False,
        )
        cur.execute("INSERT OR IGNORE INTO china_stock_receipts(package_id,received_at,quantities_json) VALUES(?,?,?)",
                    (package_id, received_at, receipt_payload))
        if cur.rowcount != 1:
            cur.execute("UPDATE china_packages SET status='arrived',warehouse_received=1 WHERE id=?", (package_id,))
            return []
        for item in items:
            cur.execute("INSERT OR IGNORE INTO stock(product_id,qty) VALUES(?,0)", (item["product_id"],))
            cur.execute("UPDATE stock SET qty=qty+? WHERE product_id=?", (int(item["qty"]), item["product_id"]))
        cur.execute("UPDATE china_packages SET warehouse_received=1,warehouse_received_at=? WHERE id=?", (received_at, package_id))
        return list({int(item["product_id"]) for item in items})

    def sync_china_rows(table, conflict_col, ids):
        """Best-effort, kierunkowy zapis zmienionych rekordów bez pełnego push."""
        if not supabase_enabled():
            return
        try:
            sync_local_rows_to_supabase(table, conflict_col, ids)
        except Exception:
            app.logger.exception("Nie udało się zsynchronizować %s z Supabase", table)

    def hydrate_china_table(table, conflict_col="id", filters=None):
        """Odtwarza potrzebny fragment po zimnym starcie lokalnego SQLite."""
        if not supabase_enabled():
            return 0
        try:
            rows = supabase_select_rows(table, order_by=conflict_col, extra_params=filters)
            return sqlite_upsert_rows(table, rows, conflict_col)
        except Exception:
            app.logger.exception("Nie udało się odtworzyć tabeli %s z Supabase", table)
            return 0

    def apply_tracking_update(package_id, payload):
        """Aktualizuje wyłącznie pola logistyczne; nigdy nie dotyka stock."""
        info = parse_tracking_payload(payload)
        c = conn()
        cur = c.cursor()
        cur.execute("SELECT status FROM china_packages WHERE id=?", (package_id,))
        row = cur.fetchone()
        if not row:
            c.close()
            return False
        next_status = monotonic_status(row["status"], map_package_status(info["status"]))
        now = now_iso()
        changed_stock_ids = receive_into_stock(cur, package_id, row["status"]) if next_status == "arrived" and norm(row["status"]).lower() != "arrived" else []
        cur.execute("""UPDATE china_packages SET status=?, tracking_carrier=?, tracking_carrier_code=?,
          tracking_status=?, tracking_substatus=?, tracking_last_event=?, tracking_last_update=?,
          tracking_synced_at=?, tracking_error=NULL, tracking_events_json=?, tracking_eta=?,
          shipped_at=CASE WHEN ?='shipped' AND shipped_at IS NULL THEN ? ELSE shipped_at END,
          arrived_at=CASE WHEN ?='arrived' AND arrived_at IS NULL THEN ? ELSE arrived_at END
          WHERE id=?""", (next_status, info["carrier"], info["carrier_code"], info["status"],
          info["substatus"], info["last_event"], info["last_update"], now,
          json.dumps(info["events"], ensure_ascii=False), info["eta"], next_status, now,
          next_status, now, package_id))
        c.commit()
        c.close()
        if changed_stock_ids:
            sync_china_rows("stock", "product_id", changed_stock_ids)
            sync_china_rows("china_stock_receipts", "package_id", [package_id])
        sync_china_rows("china_packages", "id", [package_id])
        return True


    @app.get("/china")
    def china():
        # WyĹ‚Ä…czony pull z Supabase tylko dla moduĹ‚u Chiny.
        # Tu pracujemy na lokalnej bazie, ĹĽeby POST -> redirect nie cofaĹ‚ zmian.
        c = conn()
        cur = c.cursor()
        cur.execute("SELECT * FROM china_packages ORDER BY id DESC LIMIT 200")
        packs = cur.fetchall()
        if not packs and hydrate_china_table("china_packages"):
            hydrate_china_table("products")
            hydrate_china_table("china_items")
            cur.execute("SELECT * FROM china_packages ORDER BY id DESC LIMIT 200")
            packs = cur.fetchall()

        all_packs = [dict(row) for row in packs]
        item_rows = [dict(row) for row in cur.execute("""SELECT ci.package_id,ci.sku,ci.qty,p.model,p.name
          FROM china_items ci LEFT JOIN products p ON p.id=ci.product_id ORDER BY ci.id""").fetchall()]
        document_rows = [dict(row) for row in cur.execute(
            "SELECT * FROM china_documents ORDER BY id DESC"
        ).fetchall()]
        c.close()

        contents = {}
        for item in item_rows:
            contents.setdefault(int(item["package_id"]), []).append(item)
        documents = {}
        for document in document_rows:
            documents.setdefault(int(document["package_id"]), []).append(document)
        query = norm(request.args.get("q")).lower()
        status_filter = norm(request.args.get("status")).lower()
        tracking_filter = norm(request.args.get("tracking_filter")).lower()
        receipt_filter = norm(request.args.get("receipt_filter")).lower()
        scope = norm(request.args.get("scope")).lower()
        supplier_filter = norm(request.args.get("supplier")).lower()
        date_from = norm(request.args.get("date_from"))
        date_to = norm(request.args.get("date_to"))
        problem_only = request.args.get("problems") == "1"

        def has_problem(pack):
            return norm(pack.get("status")).lower() == "problem" or bool(norm(pack.get("tracking_error"))) or norm(pack.get("tracking_status")).lower() in {"deliveryfailure","exception","expired","failure"}

        filtered = []
        tracking_status_labels = {
            "notfound": "Brak danych",
            "info_received": "Dane przesyłki przekazane",
            "inforeceived": "Dane przesyłki przekazane",
            "intransit": "W drodze",
            "outfordelivery": "W doręczeniu",
            "availableforpickup": "Gotowa do odbioru",
            "pickup": "Gotowa do odbioru",
            "delivered": "Dostarczona",
            "deliveryfailure": "Nieudane doręczenie",
            "exception": "Problem z przesyłką",
            "expired": "Tracking wygasł",
        }
        for pack in all_packs:
            haystack = " ".join((norm(pack.get("package_no")), norm(pack.get("supplier")), norm(pack.get("tracking")))).lower()
            created_day = norm(pack.get("created_at"))[:10]
            if query and query not in haystack: continue
            if status_filter and norm(pack.get("status")).lower() != status_filter: continue
            if supplier_filter and norm(pack.get("supplier")).lower() != supplier_filter: continue
            if tracking_filter == "yes" and not norm(pack.get("tracking")): continue
            if tracking_filter == "no" and norm(pack.get("tracking")): continue
            if receipt_filter == "yes" and pack.get("warehouse_received") != 1: continue
            if receipt_filter == "no" and pack.get("warehouse_received") == 1: continue
            if scope == "active" and norm(pack.get("status")).lower() == "arrived": continue
            if scope == "arrived" and norm(pack.get("status")).lower() != "arrived": continue
            if date_from and created_day < date_from: continue
            if date_to and created_day > date_to: continue
            if problem_only and not has_problem(pack): continue
            pack["items"] = contents.get(int(pack["id"]), [])
            pack["documents"] = documents.get(int(pack["id"]), [])
            pack["item_count"] = len(pack["items"])
            pack["units"] = sum(int(item.get("qty") or 0) for item in pack["items"])
            try: pack["age_days"] = max(0, (app_now().date() - datetime.fromisoformat(created_day).date()).days)
            except Exception: pack["age_days"] = 0
            shipped_day = norm(pack.get("shipped_at"))[:10]
            try: pack["transit_days"] = max(0, (app_now().date() - datetime.fromisoformat(shipped_day).date()).days)
            except Exception: pack["transit_days"] = 0
            raw_tracking_status = norm(pack.get("tracking_status"))
            status_key = "".join(ch for ch in raw_tracking_status.lower() if ch.isalnum())
            pack["tracking_status_pl"] = "Dostarczona" if norm(pack.get("status")).lower() == "arrived" else tracking_status_labels.get(status_key)
            if not pack["tracking_status_pl"]:
                pack["tracking_status_pl"] = {
                    "arrived": "Dostarczona", "shipped": "W drodze",
                    "ordered": "Zamówiona", "planned": "Planowana",
                    "problem": "Problem z przesyłką",
                }.get(norm(pack.get("status")).lower(), "Brak statusu")
            # 17TRACK potrafi zwrócić ETA jako obiekt {source, from, to}.
            # W bazie starszych wdrożeń taki obiekt bywa zapisany jako tekst;
            # do widoku wyciągamy wyłącznie czytelną datę graniczną.
            raw_eta = norm(pack.get("tracking_eta"))
            eta_dates = re.findall(r"\d{4}-\d{2}-\d{2}", raw_eta)
            pack["tracking_eta_display"] = eta_dates[-1] if eta_dates else (raw_eta if len(raw_eta) <= 20 else "")
            filtered.append(pack)

        status_counts = {key: sum(1 for p in all_packs if norm(p.get("status")).lower() == key) for key in valid_statuses}
        active = [p for p in all_packs if norm(p.get("status")).lower() in {"planned","ordered","shipped","problem"}]
        kpis = {
            "all": len(all_packs), **status_counts,
            "in_transit_value": sum(float(p.get("cost_amount") or 0) for p in all_packs if norm(p.get("status")).lower() in {"ordered","shipped","problem"}),
            "active_value": sum(float(p.get("cost_amount") or 0) for p in active),
            "without_tracking": sum(1 for p in active if not norm(p.get("tracking"))),
        }
        now = app_now()
        alerts = []
        alert_metrics = {"missing_tracking": 0, "long_transit": 0, "stale_tracking": 0, "missing_cost": 0}
        for p in active:
            try: age = (now.date() - datetime.fromisoformat(norm(p.get("created_at")).replace("Z", "+00:00")).date()).days
            except Exception: age = 0
            status = norm(p.get("status")).lower()
            if not norm(p.get("tracking")) and age > 5:
                alerts.append((p, "Brak trackingu od ponad 5 dni")); alert_metrics["missing_tracking"] += 1
            if status == "shipped" and not norm(p.get("tracking")): alerts.append((p, "Wysłana, ale bez trackingu"))
            if status == "shipped" and age > 20:
                alerts.append((p, f"W drodze co najmniej {age} dni")); alert_metrics["long_transit"] += 1
            if float(p.get("cost_amount") or 0) <= 0:
                alerts.append((p, "Brak kosztu")); alert_metrics["missing_cost"] += 1
            if norm(p.get("tracking")) and norm(p.get("tracking_synced_at")):
                try: stale_days = (now.date() - datetime.fromisoformat(norm(p.get("tracking_synced_at"))[:10]).date()).days
                except Exception: stale_days = 0
                if stale_days > 10:
                    alerts.append((p, f"Tracking bez aktualizacji od {stale_days} dni")); alert_metrics["stale_tracking"] += 1
            if has_problem(p): alerts.append((p, norm(p.get("tracking_error")) or "Problem trackingowy"))

        suppliers = sorted({norm(p.get("supplier")) for p in all_packs if norm(p.get("supplier"))})
        return render_template("china_list.html", title="Chiny (P/O)", packs=filtered, kpis=kpis,
            alerts=alerts, alert_metrics=alert_metrics, suppliers=suppliers, contents=contents,
            tracking_api_enabled=tracking_enabled())

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
        supplier = norm(request.form.get("supplier"))
        shipping_method = norm(request.form.get("shipping_method"))
        cost_amount = to_float(request.form.get("cost_amount"), 0)
        cost_document_no = norm(request.form.get("cost_document_no")) or package_no

        if not package_no or cost_amount <= 0:
            return "Podaj numer P/O oraz koszt większy od zera", 400
        if status not in valid_statuses or status == "arrived":
            return "Nowa paczka nie może być od razu oznaczona jako arrived", 400

        c = conn()
        cur = c.cursor()
        try:
            cur.execute("""
              INSERT INTO china_packages(package_no,status,tracking,note,cost_amount,cost_document_no,
                supplier,shipping_method,ordered_at,shipped_at,warehouse_received,created_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """, (package_no,status,tracking,note,cost_amount,cost_document_no,supplier,
              shipping_method,
              now_iso() if status in {"ordered","shipped"} else None,
              now_iso() if status == "shipped" else None,0,now_iso()))
            package_id = cur.lastrowid
            c.commit()
        except sqlite3.IntegrityError:
            return "Paczka o tym numerze już istnieje", 409
        finally:
            if c:
                c.close()

        sync_china_rows("china_packages", "id", [package_id])

        return redirect(url_for("china"))



    @app.post("/china/<int:package_id>/status")
    def china_status(package_id):
        status = norm(request.form.get("status"))
        if status not in valid_statuses:
            return "NieprawidĹ‚owy status", 400

        c = conn()
        cur = c.cursor()

        cur.execute("SELECT status FROM china_packages WHERE id=?", (package_id,))
        pack = cur.fetchone()
        if not pack:
            c.close()
            abort(404)

        old_status = pack["status"]

        if norm(old_status).lower() == "arrived" and status != "arrived":
            c.close()
            return "Dostarczonej i przyjętej paczki nie można cofnąć do towaru w drodze", 409

        now = now_iso()
        changed_stock_ids = receive_into_stock(cur, package_id, old_status) if status == "arrived" and norm(old_status).lower() != "arrived" else []
        if status == "arrived" and norm(old_status).lower() != "arrived" and changed_stock_ids is None:
            c.close()
            return "Nie można przyjąć pustej paczki", 409
        cur.execute("""UPDATE china_packages SET status=?,manual_status_at=?,
          ordered_at=CASE WHEN ? IN ('ordered','shipped') AND ordered_at IS NULL THEN ? ELSE ordered_at END,
          shipped_at=CASE WHEN ?='shipped' AND shipped_at IS NULL THEN ? ELSE shipped_at END,
          arrived_at=CASE WHEN ?='arrived' AND arrived_at IS NULL THEN ? ELSE arrived_at END
          WHERE id=?""", (status,now,status,now,status,now,status,now,package_id))
        c.commit()
        c.close()
        if changed_stock_ids:
            sync_china_rows("stock", "product_id", changed_stock_ids)
        sync_china_rows("china_packages", "id", [package_id])
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
        sync_china_rows("china_packages", "id", [package_id])

        ref = request.referrer or ""
        if ref.endswith(f"/china/{package_id}"):
            return redirect(url_for("china_package", package_id=package_id))
        return redirect(url_for("china"))

    @app.post("/china/<int:package_id>/tracking/register")
    def china_tracking_register(package_id):
        if not _rate_limit("17track_admin", 40, 60):
            return "Zbyt wiele żądań do 17TRACK", 429
        if not tracking_enabled():
            return redirect(url_for("china", tracking_error="Integracja 17TRACK jest wyłączona lub brakuje klucza API."))
        c = conn()
        row = c.execute("SELECT status,tracking,tracking_carrier_code,tracking_registered_at FROM china_packages WHERE id=?", (package_id,)).fetchone()
        c.close()
        if not row or not norm(row["tracking"]):
            return "Brak numeru trackingowego", 400
        if norm(row["status"]).lower() == "arrived":
            return redirect(url_for("china", tracking_error="Dostarczone P/O jest historyczne — nie zużyto limitu 17TRACK."))
        try:
            client = SeventeenTrackClient(SEVENTEENTRACK_API_KEY, SEVENTEENTRACK_TIMEOUT_SEC)
            result = client.register(
                row["tracking"], row["tracking_carrier_code"]
            )
            parcel = SeventeenTrackClient._parcel(row["tracking"], result.get("carrier") or row["tracking_carrier_code"])
            push_requested = False
            try:
                client.request_push([parcel])
                push_requested = True
            except Exception:
                # Sama rejestracja jest sukcesem. Niedostępny push nie może jej
                # cofnąć; automatyczny batch pobierze status przy kolejnym cyklu.
                app.logger.exception("17TRACK: numer zarejestrowany, ale nie udało się zlecić push")
            c = conn()
            c.execute("UPDATE china_packages SET tracking_registered_at=?,tracking_carrier_code=COALESCE(?,tracking_carrier_code),tracking_error=NULL WHERE id=?",
                      (now_iso(), result.get("carrier"), package_id))
            c.commit(); c.close()
            sync_china_rows("china_packages", "id", [package_id])
            return redirect(url_for("china", tracking_registered=1, tracking_push=int(push_requested)))
        except Exception as exc:
            c = conn(); c.execute("UPDATE china_packages SET tracking_error=? WHERE id=?", (str(exc)[:500], package_id)); c.commit(); c.close()
            return redirect(url_for("china", tracking_error=str(exc)[:200]))

    @app.post("/china/<int:package_id>/tracking/sync")
    def china_tracking_sync(package_id):
        if not _rate_limit("17track_admin", 40, 60):
            return "Zbyt wiele żądań do 17TRACK", 429
        if not tracking_enabled():
            return redirect(url_for("china", tracking_error="Integracja 17TRACK jest wyłączona lub brakuje klucza API."))
        c = conn()
        row = c.execute("SELECT status,tracking,tracking_carrier_code FROM china_packages WHERE id=?", (package_id,)).fetchone()
        c.close()
        if not row or not norm(row["tracking"]):
            return "Brak numeru trackingowego", 400
        if norm(row["status"]).lower() == "arrived":
            return redirect(url_for("china", tracking_error="Dostarczone P/O jest historyczne — nie zużyto limitu 17TRACK."))
        try:
            parcel = SeventeenTrackClient._parcel(row["tracking"], row["tracking_carrier_code"])
            updates = SeventeenTrackClient(SEVENTEENTRACK_API_KEY, SEVENTEENTRACK_TIMEOUT_SEC).get_tracking_info([parcel])
            if updates:
                apply_tracking_update(package_id, updates[0])
            return redirect(url_for("china", tracking_synced=1))
        except Exception as exc:
            c = conn(); c.execute("UPDATE china_packages SET tracking_error=?,tracking_synced_at=? WHERE id=?", (str(exc)[:500], now_iso(), package_id)); c.commit(); c.close()
            return redirect(url_for("china", tracking_error=str(exc)[:200]))

    @app.post("/china/tracking/sync-active")
    def china_tracking_sync_active():
        """Jedno zbiorcze, limitowane sprawdzenie aktywnych przesyłek."""
        if not tracking_enabled():
            return jsonify(ok=False, error="Integracja 17TRACK jest wyłączona"), 503
        if not _rate_limit("17track_auto_batch", 12, 60 * 60):
            return jsonify(ok=True, checked=0, updated=0, status="rate_limited")
        cutoff = (app_now() - timedelta(minutes=10)).isoformat(timespec="seconds")
        c = conn()
        rows = c.execute("""SELECT id,tracking,tracking_carrier_code FROM china_packages
          WHERE status!='arrived' AND COALESCE(tracking,'')!=''
            AND (tracking_synced_at IS NULL OR tracking_synced_at<?)
          ORDER BY COALESCE(tracking_synced_at,'') ASC LIMIT 40""", (cutoff,)).fetchall()
        c.close()
        if not rows:
            return jsonify(ok=True, checked=0, updated=0, status="fresh")
        try:
            parcels = [SeventeenTrackClient._parcel(row["tracking"], row["tracking_carrier_code"]) for row in rows]
            updates = SeventeenTrackClient(SEVENTEENTRACK_API_KEY, SEVENTEENTRACK_TIMEOUT_SEC).get_tracking_info(parcels)
            by_number = {norm(update.get("number") or (update.get("data") or {}).get("number")): update
                         for update in updates if isinstance(update, dict)}
            updated = 0
            checked_at = now_iso()
            for row in rows:
                update = by_number.get(norm(row["tracking"]))
                if update:
                    updated += int(apply_tracking_update(int(row["id"]), update))
                else:
                    c = conn()
                    c.execute("UPDATE china_packages SET tracking_synced_at=? WHERE id=?", (checked_at, row["id"]))
                    c.commit(); c.close()
                    sync_china_rows("china_packages", "id", [row["id"]])
            ids = [int(row["id"]) for row in rows]
            c = conn()
            placeholders = ",".join("?" for _ in ids)
            current = [dict(row) for row in c.execute(
                f"SELECT id,status,tracking_status,tracking_eta FROM china_packages WHERE id IN ({placeholders})", ids
            ).fetchall()]
            c.close()
            return jsonify(ok=True, checked=len(rows), updated=updated, checked_at=checked_at, packages=current)
        except Exception as exc:
            app.logger.exception("Automatyczne zbiorcze sprawdzenie 17TRACK nie powiodło się")
            return jsonify(ok=False, error=str(exc)[:200]), 502

    @app.post("/webhooks/17track")
    def seventeentrack_webhook():
        if request.content_length and request.content_length > 1024 * 1024:
            return jsonify(ok=False, error="payload_too_large"), 413
        if not _rate_limit("17track_webhook", 300, 60):
            return jsonify(ok=False, error="rate_limit"), 429
        if not tracking_enabled():
            return jsonify(ok=False, error="disabled"), 503
        raw = request.get_data(cache=True)
        if not verify_webhook_signature(raw, request.headers.get("sign", ""), SEVENTEENTRACK_API_KEY):
            return jsonify(ok=False, error="invalid_signature"), 401
        try:
            payload = request.get_json(force=True)
        except Exception:
            return jsonify(ok=False, error="invalid_json"), 400
        data = payload.get("data") or {}
        updates = data.get("accepted") if isinstance(data, dict) and isinstance(data.get("accepted"), list) else [data]
        changed = 0
        for update in updates:
            number = norm(update.get("number")) if isinstance(update, dict) else ""
            if not number:
                continue
            c = conn(); rows = c.execute("SELECT id FROM china_packages WHERE tracking=?", (number,)).fetchall(); c.close()
            if not rows:
                app.logger.warning("Webhook 17TRACK dla nieznanego numeru %s", number)
                continue
            for row in rows:
                changed += int(apply_tracking_update(int(row["id"]), update))
        return jsonify(ok=True, updated=changed)

    @app.post("/china/<int:package_id>/receive")
    def china_receive(package_id):
        """Jedyna świadoma akcja P/O, która może zwiększyć fizyczny stan."""
        c = conn(); cur = c.cursor()
        cur.execute("SELECT status,warehouse_received,tracking_synced_at,manual_status_at FROM china_packages WHERE id=?", (package_id,))
        pack = cur.fetchone()
        if not pack:
            c.close(); abort(404)
        if norm(pack["status"]).lower() != "arrived":
            c.close(); return "Najpierw oznacz przesyłkę jako dostarczoną", 409
        # Historyczne arrived bez nowych znaczników traktujemy jako już przyjęte.
        if pack["warehouse_received"] == 1 or cur.execute("SELECT 1 FROM china_stock_receipts WHERE package_id=?", (package_id,)).fetchone() or (pack["warehouse_received"] is None and not pack["tracking_synced_at"] and not pack["manual_status_at"]):
            c.close(); return "Ta dostawa została już przyjęta lub jest historyczna", 409
        items = cur.execute("SELECT product_id,qty FROM china_items WHERE package_id=?", (package_id,)).fetchall()
        if not items:
            c.close(); return "Nie można przyjąć pustej paczki", 409
        changed_stock_ids = receive_into_stock(cur, package_id, pack["status"])
        if not changed_stock_ids:
            c.close(); return "Ta dostawa została już przyjęta", 409
        c.commit(); c.close()
        sync_china_rows("stock", "product_id", changed_stock_ids)
        sync_china_rows("china_stock_receipts", "package_id", [package_id])
        sync_china_rows("china_packages", "id", [package_id])
        return redirect(url_for("china", received=1))

    @app.post("/china/<int:package_id>/documents")
    def china_document_upload(package_id):
        uploaded = request.files.get("document")
        if not uploaded or not norm(uploaded.filename):
            return "Wybierz dokument PDF", 400
        original_name = os.path.basename(norm(uploaded.filename))[:180]
        document_type = norm(request.form.get("document_type")).lower()
        if document_type not in {"invoice", "zc429", "order"}:
            return "Wybierz typ dokumentu: Faktura, ZC429 lub Zamówienie", 400
        if not original_name.lower().endswith(".pdf"):
            return "Do przesyłki można dodać wyłącznie dokument PDF", 400
        data = uploaded.read(10 * 1024 * 1024 + 1)
        if not data or len(data) > 10 * 1024 * 1024:
            return "Dokument PDF musi mieć maksymalnie 10 MB", 413
        if not data.startswith(b"%PDF-"):
            return "Wybrany plik nie jest prawidłowym dokumentem PDF", 400
        c = conn()
        if not c.execute("SELECT 1 FROM china_packages WHERE id=?", (package_id,)).fetchone():
            c.close(); abort(404)
        docs_dir = os.path.join(os.path.dirname(DB_PATH), "china_documents")
        os.makedirs(docs_dir, exist_ok=True)
        stored_name = f"po_{package_id}_{uuid.uuid4().hex}.pdf"
        stored_path = os.path.join(docs_dir, stored_name)
        with open(stored_path, "wb") as handle:
            handle.write(data)
        c.execute("INSERT INTO china_documents(package_id,original_name,document_type,stored_path,size_bytes,created_at) VALUES(?,?,?,?,?,?)",
                  (package_id, original_name, document_type, stored_path, len(data), now_iso()))
        c.commit(); c.close()
        return redirect(url_for("china", document_uploaded=1))

    @app.get("/china/documents/<int:document_id>")
    def china_document_download(document_id):
        c = conn(); row = c.execute("SELECT * FROM china_documents WHERE id=?", (document_id,)).fetchone(); c.close()
        if not row or not os.path.isfile(row["stored_path"]):
            abort(404)
        return send_file(row["stored_path"], mimetype="application/pdf", as_attachment=False,
                         download_name=row["original_name"])

    @app.post("/china/documents/<int:document_id>/delete")
    def china_document_delete(document_id):
        c = conn(); row = c.execute("SELECT stored_path FROM china_documents WHERE id=?", (document_id,)).fetchone()
        if not row:
            c.close(); abort(404)
        c.execute("DELETE FROM china_documents WHERE id=?", (document_id,)); c.commit(); c.close()
        try:
            if os.path.isfile(row["stored_path"]):
                os.remove(row["stored_path"])
        except OSError:
            app.logger.exception("Nie udało się usunąć dokumentu P/O %s", document_id)
        return redirect(url_for("china", document_deleted=1))

    @app.get("/china/<int:package_id>/nurlin-order.xls")
    def china_nurlin_order(package_id):
        c = conn()
        pack = c.execute("SELECT * FROM china_packages WHERE id=?", (package_id,)).fetchone()
        items = c.execute("SELECT sku,qty FROM china_items WHERE package_id=? ORDER BY id", (package_id,)).fetchall()
        c.close()
        if not pack:
            abort(404)
        if "nurlin" not in norm(pack["supplier"]).lower():
            return "Generator jest dostępny tylko dla dostawcy Nurlin", 409
        if len(items) > 44:
            return "Wzór Nurlin mieści maksymalnie 44 pozycje", 409
        try:
            import xlrd
            from xlutils.copy import copy as copy_xls
        except ImportError:
            return "Brakuje bibliotek generatora Excel", 503
        template_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "nurlin_order_template.xls")
        source = xlrd.open_workbook(template_path, formatting_info=True)
        output = copy_xls(source)
        sheet = output.get_sheet(0)
        sheet.write(5, 1, norm(pack["shipping_method"]) or "AIR FedEx Express DAP")
        for index in range(44):
            row = 8 + index
            sheet.write(row, 0, index + 1)
            # Wzór przekazany przez dostawcę zawiera przykładowe wcześniejsze
            # pozycje, ceny i wagi. Nowe zamówienie nie może ich odziedziczyć.
            for column in range(1, 6):
                sheet.write(row, column, "")
            sheet.write(row, 1, items[index]["sku"] if index < len(items) else "")
            sheet.write(row, 3, int(items[index]["qty"]) if index < len(items) else "")
        buffer = io.BytesIO()
        output.save(buffer); buffer.seek(0)
        filename = f"Nurlin_{safe_filename(pack['package_no'])}.xls"
        return send_file(buffer, mimetype="application/vnd.ms-excel", as_attachment=True, download_name=filename)



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
        if not pack and hydrate_china_table("china_packages", filters={"id": f"eq.{package_id}"}):
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
        c = conn()
        cur = c.cursor()
        cur.execute("SELECT * FROM china_packages WHERE id=?", (package_id,))
        pack = cur.fetchone()
        pack_hydrated = False
        if not pack and hydrate_china_table("china_packages", filters={"id": f"eq.{package_id}"}):
            cur.execute("SELECT * FROM china_packages WHERE id=?", (package_id,))
            pack = cur.fetchone()
            pack_hydrated = bool(pack)
        if not pack:
            c.close()
            abort(404)

        cur.execute("SELECT id, sku, model, name FROM products WHERE COALESCE(archived,0)=0 ORDER BY sku LIMIT 5000")
        products_rows = cur.fetchall()

        # Na Renderze lokalny SQLite może wystartować pusty. Pełny pull wszystkich
        # tabel nie powinien blokować tego widoku, ale bez katalogu nie da się
        # dodać zawartości paczki. W takim przypadku pobieramy synchronicznie
        # wyłącznie tabelę products, jeden raz na zimnym starcie.
        if not products_rows and supabase_enabled():
            try:
                remote_products = supabase_select_rows("products", order_by="id")
                if remote_products:
                    sqlite_upsert_rows("products", remote_products, "id")
                    cur.execute("SELECT id, sku, model, name FROM products WHERE COALESCE(archived,0)=0 ORDER BY sku LIMIT 5000")
                    products_rows = cur.fetchall()
            except Exception as exc:
                app.logger.warning("Nie udało się pobrać katalogu produktów dla paczki z Chin: %s", type(exc).__name__)

        cur.execute("""
          SELECT ci.*, p.model, p.name
          FROM china_items ci
          JOIN products p ON p.id=ci.product_id
          WHERE ci.package_id=?
          ORDER BY ci.id DESC
        """, (package_id,))
        items = cur.fetchall()
        if pack_hydrated and not items and hydrate_china_table("china_items", filters={"package_id": f"eq.{package_id}"}):
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

        cur.execute("SELECT id, status FROM china_packages WHERE id=?", (package_id,))
        pack = cur.fetchone()
        if not pack:
            c.close()
            return "Paczka nie istnieje", 404
        if norm(pack["status"]).lower() == "arrived":
            c.close()
            return "Nie można zmieniać zawartości przyjętej paczki", 409

        cur.execute("SELECT id, qty FROM china_items WHERE package_id=? AND product_id=? ORDER BY id LIMIT 1", (package_id, product_id))
        existing = cur.fetchone()
        if existing:
            item_id = int(existing["id"])
            cur.execute("UPDATE china_items SET qty=qty+? WHERE id=?", (qty, item_id))
        else:
            cur.execute(
                "INSERT INTO china_items(package_id, product_id, sku, qty, created_at) VALUES (?,?,?,?,?)",
                (package_id, product_id, p["sku"], qty, now_iso())
            )
            item_id = cur.lastrowid
        c.commit()
        c.close()
        sync_china_rows("china_items", "id", [item_id])
        return redirect(url_for("china_package", package_id=package_id))



    @app.post("/china/<int:package_id>/items/<int:item_id>/delete")
    def china_item_delete(package_id, item_id):
        c = conn()
        cur = c.cursor()
        cur.execute("SELECT status FROM china_packages WHERE id=?", (package_id,))
        pack = cur.fetchone()
        if not pack:
            c.close()
            return "Paczka nie istnieje", 404
        if norm(pack["status"]).lower() == "arrived":
            c.close()
            return "Nie można zmieniać zawartości przyjętej paczki", 409
        cur.execute("SELECT id FROM china_items WHERE id=? AND package_id=?", (item_id, package_id))
        if not cur.fetchone():
            c.close()
            return "Pozycja nie istnieje", 404

        if supabase_enabled():
            supabase_delete_rows("china_items", {"id": item_id})

        cur.execute("DELETE FROM china_items WHERE id=? AND package_id=?", (item_id, package_id))
        c.commit()
        c.close()
        return redirect(url_for("china_package", package_id=package_id))



    exported = {'china': china, 'china_create': china_create, 'china_status': china_status, 'china_tracking': china_tracking, 'china_cost': china_cost, 'china_package': china_package, 'china_delete': china_delete, 'china_item_add': china_item_add, 'china_item_delete': china_item_delete}
    globals().update(exported)
    return exported
