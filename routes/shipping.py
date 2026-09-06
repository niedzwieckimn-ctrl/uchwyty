"""Mechanically extracted Flask routes; business logic is unchanged."""

def register_routes(context):
    globals().update(context)


    def _inpost_status_is_collected(status):
        value = norm(status).lower()
        return value in {
            "collected_by_courier", "taken_by_courier", "adopted_at_source_branch",
            "sent_from_source_branch", "adopted_at_sorting_center",
            "sent_from_sorting_center", "out_for_delivery", "ready_to_pickup",
            "pickup_reminder_sent", "delivered", "returned_to_sender",
        }


    def _inpost_event_is_collected(event_code):
        value = norm(event_code).upper()
        if not value:
            return False
        # FMD.1001 oznacza jedynie gotowość do odbioru. Dopiero FMD.1002
        # potwierdza przejęcie paczki przez kuriera. Późniejsze etapy również
        # dowodzą, że przesyłka opuściła magazyn.
        return value in {"FMD.1002", "FMD.1003", "FMD.1004", "FMD.1005"} or value.startswith(("MMD.", "LMD.", "EOL."))


    @app.post("/webhooks/inpost")
    def inpost_tracking_webhook():
        if not _rate_limit("inpost_webhook", 240, 60):
            return jsonify(ok=False, error="rate_limit"), 429
        data = request.get_json(silent=True) or {}
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else data
        nested_shipment = payload.get("shipment") if isinstance(payload.get("shipment"), dict) else {}
        shipment_id = norm(
            payload.get("shipment_id") or payload.get("shipmentId")
            or nested_shipment.get("id") or data.get("shipment_id")
        )
        tracking_no = re.sub(r"\s+", "", norm(
            payload.get("tracking_number") or payload.get("trackingNumber")
            or data.get("tracking_number") or data.get("trackingNumber")
        ))
        event_code = norm(payload.get("eventCode") or payload.get("event_code") or data.get("eventCode"))

        maybe_pull_shared_from_supabase(force=True)
        c = conn()
        try:
            cur = c.cursor()
            row = None
            if shipment_id.isdigit():
                row = cur.execute(
                    "SELECT * FROM orders WHERE inpost_shipment_id=? ORDER BY id LIMIT 1",
                    (shipment_id,),
                ).fetchone()
            if not row and tracking_no:
                row = cur.execute(
                    "SELECT * FROM orders WHERE REPLACE(COALESCE(tracking_no,''),' ','')=? ORDER BY id LIMIT 1",
                    (tracking_no,),
                ).fetchone()
            if not row:
                # Nieznane zdarzenie potwierdzamy, ale nie dotykamy zamówień.
                return jsonify(ok=True, ignored="shipment_not_found")
            order = dict(row)
            authoritative_id = norm(order.get("inpost_shipment_id"))
            try:
                remote_shipment = inpost_get_shipment(authoritative_id)
            except Exception as exc:
                app.logger.warning("Webhook InPost: nie udało się potwierdzić przesyłki %s: %s", authoritative_id, exc)
                return jsonify(ok=False, error="inpost_verification_failed"), 503
            remote_status = norm(remote_shipment.get("status"))
            remote_tracking = re.sub(r"\s+", "", norm(remote_shipment.get("tracking_number"))) or tracking_no
            # eventCode jest tylko wskazówką. Decyzję podejmujemy wyłącznie na
            # podstawie statusu ponownie pobranego z uwierzytelnionego API.
            if not _inpost_status_is_collected(remote_status):
                return jsonify(ok=True, ignored="not_collected", status=remote_status)

            package_rows = cur.execute(
                "SELECT * FROM orders WHERE inpost_shipment_id=? ORDER BY id",
                (authoritative_id,),
            ).fetchall()
            package_orders = [dict(item) for item in package_rows] or [order]
            package_order_ids = [to_int(item.get("id"), 0) for item in package_orders]
            tracking_hash = hashlib.sha256(remote_tracking.encode("utf-8")).hexdigest()[:16]
            event_keys = [f"order_shipped:{order_id}:inpost:{tracking_hash}" for order_id in package_order_ids]
            if event_keys and all(_email_event_already_ok(key) for key in event_keys):
                return jsonify(ok=True, duplicate=True, status=remote_status)

            try:
                packing_attachment = _order_packing_list_email_attachment(order)
            except Exception as exc:
                app.logger.exception("Webhook InPost: nie udało się przygotować listy pakowania")
                return jsonify(ok=False, error=("packing_list_failed: " + str(exc))[:300]), 503

            shipped_at = now_iso()
            placeholders = ",".join("?" for _ in package_order_ids)
            cur.execute(
                f"""UPDATE orders SET
                    status=CASE
                      WHEN LOWER(COALESCE(status,'')) IN ('issued','completed') THEN status
                      WHEN LOWER(COALESCE(status,''))='packed_partial' THEN 'partially_shipped'
                      ELSE 'shipped'
                    END,
                    tracking_no=CASE WHEN ?<>'' THEN ? ELSE tracking_no END,
                    carrier='inpost', shipped_at=?
                    WHERE id IN ({placeholders})""",
                (remote_tracking, remote_tracking, shipped_at, *package_order_ids),
            )
            c.commit()
            package_orders = [dict(item) for item in cur.execute(
                f"SELECT * FROM orders WHERE id IN ({placeholders}) ORDER BY id",
                tuple(package_order_ids),
            ).fetchall()]
        finally:
            c.close()

        if supabase_enabled():
            try:
                for package_order in package_orders:
                    supabase_update_rows(
                        "orders",
                        {
                            "status": package_order.get("status"),
                            "tracking_no": remote_tracking,
                            "carrier": "inpost",
                            "shipped_at": shipped_at,
                            "warehouse_issued": int(package_order.get("warehouse_issued") or 0),
                        },
                        {"id": int(package_order["id"])},
                    )
            except Exception as exc:
                app.logger.exception("Webhook InPost: błąd synchronizacji zamówień: %s", exc)
                return jsonify(ok=False, error="supabase_sync_failed"), 503

        try:
            result = _send_orders_shipped_email(package_orders, remote_tracking, "inpost", packing_attachment)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        for package_order, event_key in zip(package_orders, event_keys):
            _record_email_event(
                event_key, "order_shipped", package_order.get("id"),
                package_order.get("customer_email"), result,
            )
        if not result.get("ok"):
            return jsonify(ok=False, error=norm(result.get("error")) or "email_failed"), 503
        return jsonify(ok=True, shipped=True, orders=package_order_ids, status=remote_status)


    @app.route("/orders/<int:order_id>/inpost", methods=["GET", "POST"])
    def order_inpost_create(order_id):
        # Po utworzeniu przesyłki nie pobieramy natychmiast starszej kopii rekordu
        # z Supabase. Dzięki temu zapisany identyfikator i przycisk PDF są widoczne
        # od razu również wtedy, gdy synchronizacja zdalna potrzebuje chwili.
        just_created = request.args.get("created") == "1"
        if not just_created:
            maybe_pull_shared_from_supabase(force=True)
        c = conn()
        try:
            cur = c.cursor()
            cur.execute("SELECT * FROM orders WHERE id=?", (order_id,))
            row = cur.fetchone()
            if not row:
                abort(404)
            order = dict(row)
            package_orders = _packed_package_orders(cur, order)
        finally:
            c.close()

        cfg = inpost_config_summary()
        bundle = request.form.get("bundle") == "1" or request.args.get("bundle") == "1"
        error = norm(request.args.get("inpost_error"))
        if request.method == "POST":
            if not cfg["configured"]:
                error = "Brak konfiguracji InPost na Renderze: " + ", ".join(cfg["missing"])
            elif norm(order.get("inpost_shipment_id")):
                return redirect(url_for("order_inpost_label", order_id=order_id, bundle="1" if bundle else None))
            elif not inpost_label_allowed_for_status(order.get("status")):
                error = "Najpierw wybierz zawartość paczki w kreatorze Pakuj."
            else:
                address_source = norm(order.get("customer_address"))
                phone = norm(order.get("customer_phone"))
                try:
                    profile = _client_profile_for_email(order.get("customer_email"))
                    address_source = norm(profile.get("address")) or address_source
                    phone = norm(profile.get("phone")) or phone
                except Exception:
                    pass
                street, post_code, city = split_address(address_source)
                receiver = {
                    "name": order.get("customer_name"), "street": street,
                    "post_code": post_code, "city": city, "phone": phone,
                    "email": order.get("customer_email"),
                }
                try:
                    allowed_services = {
                        "inpost_courier_standard", "inpost_courier_express_1700",
                        "inpost_courier_express_1200", "inpost_courier_express_1000",
                    }
                    service = norm(request.form.get("service"))
                    if service not in allowed_services:
                        raise InPostError("Wybierz poprawny serwis kurierski")
                    parcel = {
                        "length": max(1, round(to_float(request.form.get("length"), 40) * 10, 1)),
                        "width": max(1, round(to_float(request.form.get("width"), 30) * 10, 1)),
                        "height": max(1, round(to_float(request.form.get("height"), 20) * 10, 1)),
                        "weight": max(0.01, to_float(request.form.get("weight"), 5)),
                        "quantity": max(1, min(99, to_int(request.form.get("quantity"), 1))),
                        "non_standard": request.form.get("non_standard") == "1",
                        "comments": norm(request.form.get("comments")),
                    }
                    additional_services = [
                        key for key in ("sms", "email", "rod", "saturday")
                        if request.form.get(key) == "1"
                    ]
                    options = {
                        "additional_services": additional_services,
                        "insurance": max(0, to_float(request.form.get("insurance"), 0)),
                        "cod": max(0, to_float(request.form.get("cod"), 0)),
                    }
                    reference = ", ".join(
                        canonical_order_no(item["id"], item["created_at"], item["order_no"])
                        for item in package_orders
                    )
                    shipment = create_courier_shipment(receiver, parcel, reference, service, options)
                    shipment_id = norm(shipment.get("id"))
                    tracking_number = norm(shipment.get("tracking_number"))
                    if not shipment_id:
                        raise InPostError("API nie zwróciło identyfikatora przesyłki")
                    # ShipX przygotowuje ofertę i potwierdza przesyłkę
                    # asynchronicznie. Pierwsza odpowiedź często nie ma jeszcze
                    # numeru śledzenia, dlatego przez kilka sekund odpytujemy
                    # utworzony zasób zamiast zostawiać puste pole w zamówieniu.
                    if not tracking_number:
                        for attempt in range(6):
                            if attempt:
                                time.sleep(1)
                            try:
                                current_shipment = inpost_get_shipment(shipment_id)
                            except InPostError:
                                continue
                            tracking_number = norm(current_shipment.get("tracking_number"))
                            if tracking_number:
                                shipment = current_shipment
                                break
                    package_ids = [int(item["id"]) for item in package_orders]
                    c = conn()
                    try:
                        placeholders = ",".join(["?"] * len(package_ids))
                        c.execute(
                            f"""UPDATE orders SET inpost_shipment_id=?, inpost_label_format='pdf',
                                tracking_no=CASE WHEN ?<>'' THEN ? ELSE tracking_no END, carrier='inpost'
                                WHERE id IN ({placeholders})""",
                            (shipment_id, tracking_number, tracking_number, *package_ids),
                        )
                        c.commit()
                    finally:
                        c.close()
                    if supabase_enabled():
                        try:
                            sync_local_rows_to_supabase("orders", "id", package_ids)
                        except Exception as exc:
                            # Przesyłka w InPost już istnieje. Błąd synchronizacji
                            # nie może ukryć identyfikatora ani prowokować ponownego
                            # utworzenia płatnej przesyłki.
                            app.logger.exception("Przesyłka InPost %s utworzona, ale synchronizacja zamówień nie powiodła się: %s", shipment_id, exc)
                    return redirect(url_for(
                        "order_inpost_create", order_id=order_id, created="1",
                        bundle="1" if bundle else None,
                    ))
                except InPostError as exc:
                    error = str(exc)

        tpl = r"""
        {% extends "base.html" %}{% block content %}
          <div class="card"><div class="flex"><div><h1 style="margin:0 0 8px;">Etykieta InPost</h1><div class="muted">Jedna przesyłka dla zamówień: {{ package_labels|join(', ') }}</div></div><a class="btn right" href="{{ url_for('order_view', order_id=o.id) }}">← Zamówienie</a></div></div>
          <div class="card">
            {% if created and o.inpost_shipment_id %}<div class="hint" style="border-color:#a7e8cf;background:#edfbf6;color:#17684e;margin-bottom:15px;"><b>Przesyłka InPost została utworzona z odbiorem przez kuriera.</b>{% if o.tracking_no %} Numer: <b>{{ o.tracking_no }}</b>.{% else %} InPost przygotowuje jeszcze numer przesyłki.{% endif %} PDF pobierzesz przyciskiem poniżej.</div>{% endif %}
            {% if error %}<div class="hint" style="border-color:#fecaca;background:#fff1f2;margin-bottom:15px;">{{ error }}</div>{% endif %}
            {% if not cfg.configured %}<div class="hint">Dodaj na Renderze zmienną <b>INPOST_API_TOKEN</b>. ID organizacji aplikacja pobierze automatycznie.</div>{% endif %}
            {% if o.inpost_shipment_id %}<div class="flex"><span class="badge">Przesyłka już utworzona</span><a class="btn primary" href="{{ url_for('order_inpost_label', order_id=o.id, bundle='1' if bundle else None) }}">{% if bundle %}Pobierz listę A4 + etykietę A6 (PDF){% else %}Pobierz etykietę A6 (PDF){% endif %}</a><a class="btn" href="{{ url_for('order_view', order_id=o.id) }}">Wróć do zamówienia</a></div>{% else %}
            <form method="post" class="row">
              {% if bundle %}<input type="hidden" name="bundle" value="1">{% endif %}
              <div><label class="muted small">Serwis</label><select name="service" required><option value="inpost_courier_standard">Kurier Standard</option><option value="inpost_courier_express_1700">Doręczenie 17:00</option><option value="inpost_courier_express_1200">Doręczenie 12:00</option><option value="inpost_courier_express_1000">Doręczenie 10:00</option></select></div>
              <div><label class="muted small">Liczba paczek</label><input type="number" name="quantity" value="1" min="1" max="99" required></div>
              <div><label class="muted small">Długość (cm)</label><input type="number" name="length" value="40" min="0.1" max="350" step="0.1" required></div>
              <div><label class="muted small">Szerokość (cm)</label><input type="number" name="width" value="30" min="0.1" max="240" step="0.1" required></div>
              <div><label class="muted small">Wysokość (cm)</label><input type="number" name="height" value="20" min="0.1" max="240" step="0.1" required></div>
              <div><label class="muted small">Waga (kg)</label><input type="number" name="weight" value="5" min="0.01" max="50" step="0.01" required></div>
              <div><label class="muted small">Rodzaj</label><select name="non_standard"><option value="0">Standardowa</option><option value="1">Niestandardowa</option></select></div>
              <div><label class="muted small">Dodatkowa ochrona (PLN)</label><input type="number" name="insurance" value="0" min="0" step="0.01"></div>
              <div><label class="muted small">Pobranie COD (PLN)</label><input type="number" name="cod" value="0" min="0" step="0.01"><div class="muted small">Ochrona musi być ≥ pobraniu.</div></div>
              <div><label class="muted small">Uwagi dla InPost</label><input name="comments" maxlength="100"></div>
              <div style="grid-column:1/-1" class="flex"><label><input type="checkbox" name="sms" value="1"> Serwis SMS</label><label><input type="checkbox" name="email" value="1"> Serwis Email</label><label><input type="checkbox" name="rod" value="1"> Zwrot dokumentów</label><label><input type="checkbox" name="saturday" value="1"> Doręczenie w sobotę</label></div>
              <div style="grid-column:1/-1"><button class="btn primary" type="submit" onclick="return confirm('Utworzyć płatną przesyłkę InPost dla tej paczki?')">Utwórz przesyłkę i pobierz PDF A6</button></div>
            </form>{% endif %}
          </div>
        {% endblock %}
        """
        labels = [canonical_order_no(item["id"], item["created_at"], item["order_no"]) for item in package_orders]
        return render_template_string(tpl, title="Etykieta InPost", base_url=BASE_URL, db_path=DB_PATH, o=order, cfg=cfg, error=error, package_labels=labels, bundle=bundle, created=just_created)




    @app.get("/orders/<int:order_id>/inpost/label")
    def order_inpost_label(order_id):
        maybe_pull_shared_from_supabase(force=True)
        c = conn()
        try:
            row = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        finally:
            c.close()
        if not row:
            abort(404)
        shipment_id = norm(row["inpost_shipment_id"])
        if not shipment_id:
            return redirect(url_for("order_inpost_create", order_id=order_id))
        # Przy okazji pobrania uzupełnij numer, jeśli pierwsza odpowiedź ShipX
        # podczas tworzenia przesyłki jeszcze go nie zawierała.
        if not norm(row["tracking_no"]):
            try:
                current_shipment = inpost_get_shipment(shipment_id)
                refreshed_tracking = norm(current_shipment.get("tracking_number"))
                if refreshed_tracking:
                    c = conn()
                    try:
                        c.execute("UPDATE orders SET tracking_no=?, carrier='inpost' WHERE inpost_shipment_id=?", (refreshed_tracking, shipment_id))
                        c.commit()
                        changed_ids = [int(item["id"]) for item in c.execute("SELECT id FROM orders WHERE inpost_shipment_id=?", (shipment_id,)).fetchall()]
                    finally:
                        c.close()
                    if supabase_enabled() and changed_ids:
                        try:
                            sync_local_rows_to_supabase("orders", "id", changed_ids)
                        except Exception as exc:
                            app.logger.warning("Nie udało się zsynchronizować numeru InPost: %s", exc)
            except InPostError:
                pass
        label_error = None
        pdf = None
        for attempt in range(3):
            try:
                pdf = inpost_get_label(shipment_id, "pdf", "A6")
                break
            except InPostError as exc:
                label_error = exc
                if attempt < 2:
                    time.sleep(1)
        if pdf is None:
            return redirect(url_for(
                "order_inpost_create", order_id=order_id,
                inpost_error=(str(label_error)[:240] if label_error else "Etykieta nie jest jeszcze gotowa. Spróbuj ponownie."),
                bundle="1" if request.args.get("bundle") == "1" else None,
            ))
        filename_root = safe_filename(canonical_order_no(row["id"], row["created_at"], row["order_no"]))
        if request.args.get("bundle") == "1":
            pack_path = norm(session.get(f"inpost_pack_path_{order_id}"))
            if pack_path and os.path.isfile(pack_path):
                try:
                    writer = PdfWriter()
                    writer.append(pack_path)
                    writer.append(io.BytesIO(pdf))
                    combined = io.BytesIO()
                    writer.write(combined)
                    writer.close()
                    combined.seek(0)
                    return send_file(
                        combined, mimetype="application/pdf", as_attachment=True,
                        download_name=filename_root + "_pakiet_A4_lista_A6_InPost.pdf", max_age=0,
                    )
                except Exception as exc:
                    app.logger.exception("Nie udało się połączyć listy A4 z etykietą A6: %s", exc)
        filename = filename_root + "_InPost_A6.pdf"
        return send_file(io.BytesIO(pdf), mimetype="application/pdf", as_attachment=True, download_name=filename, max_age=0)




    @app.route("/inpost/dispatch", methods=["GET", "POST"])
    def inpost_dispatch_order():
        maybe_pull_shared_from_supabase(force=True)
        c = conn()
        try:
            cur = c.cursor()
            cur.execute("""SELECT inpost_shipment_id, MIN(id) AS order_id,
                                  GROUP_CONCAT(order_no, ', ') AS order_numbers,
                                  MAX(tracking_no) AS tracking_no, MAX(created_at) AS created_at
                           FROM orders
                           WHERE TRIM(COALESCE(inpost_shipment_id,''))<>''
                             AND TRIM(COALESCE(inpost_dispatch_order_id,''))=''
                           GROUP BY inpost_shipment_id ORDER BY MAX(id) DESC""")
            pending = [dict(row) for row in cur.fetchall()]
            company_row = cur.execute("SELECT * FROM company_profile WHERE id=1").fetchone()
            company = dict(company_row) if company_row else {}
        finally:
            c.close()
        street, post_code, city = split_address(company.get("address") or "")
        error = norm(request.args.get("error"))
        created = norm(request.args.get("created"))
        if request.method == "POST":
            selected = list(dict.fromkeys(norm(value) for value in request.form.getlist("shipment_id") if norm(value)))
            allowed = {norm(row["inpost_shipment_id"]) for row in pending}
            selected = [value for value in selected if value in allowed]
            pickup = {
                "name": norm(request.form.get("name")), "street": norm(request.form.get("street")),
                "post_code": norm(request.form.get("post_code")), "city": norm(request.form.get("city")),
                "phone": norm(request.form.get("phone")), "email": norm(request.form.get("email")),
                "comment": norm(request.form.get("comment")),
            }
            try:
                result = inpost_create_dispatch_order(selected, pickup)
                dispatch_id = norm(result.get("id"))
                if not dispatch_id:
                    raise InPostError("API nie zwróciło identyfikatora zlecenia odbioru")
                c = conn()
                try:
                    placeholders = ",".join(["?"] * len(selected))
                    c.execute(
                        f"UPDATE orders SET inpost_dispatch_order_id=? WHERE inpost_shipment_id IN ({placeholders})",
                        (dispatch_id, *selected),
                    )
                    c.commit()
                    cur = c.cursor()
                    cur.execute(f"SELECT id FROM orders WHERE inpost_shipment_id IN ({placeholders})", tuple(selected))
                    changed_ids = [int(row["id"]) for row in cur.fetchall()]
                finally:
                    c.close()
                if supabase_enabled() and changed_ids:
                    sync_local_rows_to_supabase("orders", "id", changed_ids)
                return redirect(url_for("inpost_dispatch_order", created=dispatch_id))
            except InPostError as exc:
                error = str(exc)
        tpl = r"""
        {% extends "base.html" %}{% block content %}
          <div class="card"><div class="flex"><div><h1 style="margin:0 0 8px;">Zamów kuriera InPost</h1><div class="muted">Zlecenie odbioru powstaje dopiero dla zaznaczonych, wcześniej utworzonych przesyłek.</div></div><a class="btn right" href="{{ url_for('orders') }}">← Zamówienia</a></div></div>
          <div class="card">
            {% if created %}<div class="hint" style="margin-bottom:14px;">Zamówiono kuriera. ID zlecenia: <b>{{ created }}</b></div>{% endif %}
            {% if error %}<div class="hint" style="border-color:#fecaca;background:#fff1f2;margin-bottom:14px;">{{ error }}</div>{% endif %}
            <form method="post">
              <h2>Przesyłki oczekujące na odbiór</h2>
              <table><thead><tr><th></th><th>Zamówienia</th><th>Tracking</th><th>ID ShipX</th></tr></thead><tbody>
              {% for row in pending %}<tr><td><input type="checkbox" name="shipment_id" value="{{ row.inpost_shipment_id }}"></td><td>{{ row.order_numbers }}</td><td>{{ row.tracking_no or '-' }}</td><td>{{ row.inpost_shipment_id }}</td></tr>{% endfor %}
              {% if not pending %}<tr><td colspan="4" class="muted">Brak przesyłek oczekujących na zamówienie kuriera.</td></tr>{% endif %}
              </tbody></table>
              {% if pending %}<div class="row" style="margin-top:18px;">
                <div><label class="muted small">Nazwa punktu odbioru</label><input name="name" value="{{ company.company_name or 'Magazyn' }}" required></div>
                <div><label class="muted small">Ulica i numer</label><input name="street" value="{{ street }}" required></div>
                <div><label class="muted small">Kod pocztowy</label><input name="post_code" value="{{ post_code }}" required></div>
                <div><label class="muted small">Miasto</label><input name="city" value="{{ city }}" required></div>
                <div><label class="muted small">Telefon</label><input name="phone" value="{{ company.phone or '' }}" required></div>
                <div><label class="muted small">Email</label><input name="email" value="{{ company.email or '' }}"></div>
                <div><label class="muted small">Komentarz</label><input name="comment" maxlength="100"></div>
                <div style="grid-column:1/-1"><button class="btn primary" type="submit" onclick="return confirm('Zamówić kuriera po zaznaczone przesyłki?')">Zamów kuriera</button></div>
              </div>{% endif %}
            </form>
          </div>
        {% endblock %}
        """
        return render_template_string(tpl, title="Zamów kuriera InPost", base_url=BASE_URL, db_path=DB_PATH, pending=pending, company=company, street=street, post_code=post_code, city=city, error=error, created=created)




    @app.post("/orders/<int:order_id>/shipped")
    def order_mark_shipped(order_id):
        tracking_no = re.sub(r"\s+", "", norm(request.form.get("tracking_no")))
        carrier = norm(request.form.get("carrier")).lower()
        notify_customer = request.form.get("notify_customer", "1") == "1"
        if not tracking_no or len(tracking_no) > 120:
            return "Podaj poprawny numer przesyłki", 400

        if carrier not in {"inpost", "dpd", "fedex", "dhl", "ups"}:
            return "Wybierz poprawnego kuriera", 400

        maybe_pull_shared_from_supabase(force=True)
        c = conn()
        try:
            cur = c.cursor()
            cur.execute("SELECT * FROM orders WHERE id=?", (order_id,))
            row = cur.fetchone()
            if not row:
                abort(404)
            order = dict(row)
            package_orders = [order]
            packed_at = norm(order.get("packed_at"))
            recipient_key = _email_key(order.get("customer_email"))
            if packed_at and recipient_key:
                cur.execute(
                    """SELECT * FROM orders
                       WHERE packed_at=?
                         AND LOWER(TRIM(COALESCE(customer_email,'')))=?
                         AND LOWER(COALESCE(status,'')) NOT IN ('cancelled','issued','completed')
                       ORDER BY id""",
                    (packed_at, recipient_key),
                )
                grouped_rows = [dict(item) for item in cur.fetchall()]
                if grouped_rows:
                    package_orders = grouped_rows
            package_order_ids = [to_int(item.get("id"), 0) for item in package_orders]
            try:
                packing_attachment = _order_packing_list_email_attachment(order)
            except Exception as exc:
                return redirect(url_for(
                    "order_view",
                    order_id=order_id,
                    shipment_email_error=("Nie udało się przygotować listy pakowania: " + str(exc))[:240],
                ))
            placeholders = ",".join(["?"] * len(package_order_ids))
            shipped_at = now_iso()
            # Status wysyłki nie zmienia magazynu. Stan schodzi dopiero podczas
            # pełnego zafakturowania zamówienia w finalize_fully_invoiced_orders().
            cur.execute(
                f"""UPDATE orders
                    SET status=CASE
                          WHEN LOWER(COALESCE(status,'')) IN ('issued','completed') THEN status
                          WHEN LOWER(COALESCE(status,''))='packed_partial' THEN 'partially_shipped'
                          ELSE 'shipped'
                        END,
                        tracking_no=?, carrier=?, shipped_at=?
                    WHERE id IN ({placeholders})""",
                (tracking_no, carrier, shipped_at, *package_order_ids),
            )
            c.commit()
            cur.execute(f"SELECT * FROM orders WHERE id IN ({placeholders}) ORDER BY id", tuple(package_order_ids))
            package_orders = [dict(item) for item in cur.fetchall()]
            order = next((item for item in package_orders if to_int(item.get("id"), 0) == order_id), package_orders[0])
        finally:
            c.close()

        if supabase_enabled():
            try:
                # Nie wysyłamy całego rekordu. Jedna brakująca w chmurze kolumna
                # opcjonalnego modułu mogłaby odrzucić PATCH i po kolejnym pullu
                # cofnąć status oraz tracking do wartości sprzed wysyłki.
                for package_order in package_orders:
                    supabase_update_rows(
                        "orders",
                        {
                            "status": package_order.get("status"),
                            "tracking_no": tracking_no,
                            "carrier": carrier,
                            "shipped_at": shipped_at,
                            "warehouse_issued": int(package_order.get("warehouse_issued") or 0),
                        },
                        {"id": int(package_order["id"])},
                    )
            except Exception as exc:
                app.logger.exception("Nie udało się zsynchronizować wysyłki zamówienia %s: %s", order_id, exc)
                return redirect(url_for(
                    "order_view", order_id=order_id,
                    shipment_email_error="Nie zapisano statusu wysyłki w chmurze. E-mail nie został ponownie wysłany. Spróbuj ponownie po sprawdzeniu połączenia.",
                ))

        try:
            result = _send_orders_shipped_email(package_orders, tracking_no, carrier, packing_attachment)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        tracking_hash = hashlib.sha256(tracking_no.encode("utf-8")).hexdigest()[:16]
        for package_order in package_orders:
            package_order_id = to_int(package_order.get("id"), 0)
            event_key = f"order_shipped:{package_order_id}:{carrier}:{tracking_hash}"
            _record_email_event(event_key, "order_shipped", package_order_id, package_order.get("customer_email"), result)
        if result.get("ok"):
            return redirect(url_for("order_view", order_id=order_id, shipment_sent="1"))
        return redirect(url_for(
            "order_view",
            order_id=order_id,
            shipment_email_error=norm(result.get("error"))[:240] or "nieznany błąd",
        ))




    @app.route("/orders/<int:order_id>/packing-list", methods=["GET", "POST"])
    def order_packing_list_download_admin(order_id):
        """Generuje wspolna liste pakowania dla zamowien tego samego klienta."""
        selected_carrier = norm(request.form.get("carrier") or request.args.get("carrier")).lower()
        if selected_carrier not in {"inpost", "other"}:
            tpl = r"""
            {% extends "base.html" %}{% block content %}
              <style>
                .carrier-options{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}
                .carrier-option{display:flex;flex-direction:column;min-height:190px;text-decoration:none;color:inherit;overflow:hidden;transition:transform .16s ease,border-color .16s ease,box-shadow .16s ease}
                .carrier-option:hover{transform:translateY(-2px);border-color:#bdcaf5;box-shadow:0 18px 42px rgba(31,55,105,.13)}
                .carrier-option-head{display:flex;align-items:center;gap:14px;margin-bottom:14px}
                .carrier-icon{display:grid;place-items:center;flex:0 0 48px;width:48px;height:48px;border-radius:15px;background:#edf3ff;color:#4166d3;font-size:22px;font-weight:900}
                .carrier-option.other .carrier-icon{background:#f2f4f8;color:#65728a}
                .carrier-option h2{margin:0;font-size:20px}
                .carrier-option p{margin:0;line-height:1.6;max-width:520px}
                .carrier-action{margin-top:auto;padding-top:24px}.carrier-action .btn{pointer-events:none}
                @media(max-width:760px){.carrier-options{grid-template-columns:1fr}.carrier-option{min-height:170px}}
              </style>
              <div class="card">
                <div class="flex">
                  <div><h1 style="margin:0 0 8px;">Pakuj zamówienie</h1><div class="muted">Najpierw wybierz sposób wysyłki, a następnie zawartość paczki.</div></div>
                  <a class="btn right" href="{{ url_for('order_view', order_id=order_id) }}">← Zamówienie</a>
                </div>
              </div>
              <div class="carrier-options">
                <a class="card carrier-option" href="{{ url_for('order_packing_list_download_admin', order_id=order_id, carrier='inpost') }}">
                  <div class="carrier-option-head"><span class="carrier-icon">I</span><h2>InPost</h2></div>
                  <p class="muted">Wybierz zamówienia i ilości, określ rodzaj paczki, a następnie wygeneruj etykietę A6 oraz wspólną listę pakową A4.</p>
                  <div class="carrier-action"><span class="btn primary">Wybierz InPost →</span></div>
                </a>
                <a class="card carrier-option other" href="{{ url_for('order_packing_list_download_admin', order_id=order_id, carrier='other') }}">
                  <div class="carrier-option-head"><span class="carrier-icon">↗</span><h2>Inny przewoźnik</h2></div>
                  <p class="muted">Wybierz zamówienia i ilości, a następnie pobierz jedną zbiorczą listę pakową A4 — bez zamawiania kuriera.</p>
                  <div class="carrier-action"><span class="btn">Wybierz innego przewoźnika →</span></div>
                </a>
              </div>
            {% endblock %}
            """
            return render_template_string(tpl, title="Pakuj", base_url=BASE_URL, db_path=DB_PATH, order_id=order_id)
        maybe_pull_shared_from_supabase()
        c = conn()
        cur = c.cursor()
        cur.execute("SELECT * FROM orders WHERE id=?", (order_id,))
        order_row = cur.fetchone()
        if not order_row:
            c.close()
            return "Nie znaleziono zamowienia", 404
        candidate_orders = [dict(order_row)]
        recipient = _email_key(order_row["customer_email"])
        if recipient:
            # Jedno klikniecie „Pakuj” obejmuje pozostale potwierdzone zamowienia
            # tego samego klienta. Dzieki temu powstaje jeden dokument i jeden
            # zbiorczy e-mail, zamiast osobnej wiadomosci dla kazdego zamowienia.
            cur.execute("""
              SELECT *
              FROM orders
              WHERE id<>?
                AND LOWER(TRIM(COALESCE(customer_email,'')))=?
                AND LOWER(COALESCE(status,'')) IN ('confirmed','packed','packed_partial')
              ORDER BY created_at, id
            """, (order_id, recipient))
            candidate_orders.extend(dict(row) for row in cur.fetchall())

        candidate_by_id = {int(order["id"]): order for order in candidate_orders}
        candidate_ids = sorted(candidate_by_id)
        placeholders = ",".join(["?"] * len(candidate_ids))
        cur.execute(f"""
          SELECT oi.id, oi.order_id, oi.product_id, oi.qty, p.sku, p.model, p.name,
                 COALESCE(s.qty,0) AS stock_qty
          FROM order_items oi
          JOIN products p ON p.id=oi.product_id
          LEFT JOIN stock s ON s.product_id=oi.product_id
          WHERE oi.order_id IN ({placeholders})
          ORDER BY oi.order_id, oi.id
        """, tuple(candidate_ids))
        order_items = [dict(row) for row in cur.fetchall()]
        c.close()
        if not order_items:
            return "Brak pozycji zamowienia", 400

        # Wspólna pula dla product_id zapobiega wybraniu tego samego stanu
        # kilka razy, gdy produkt występuje w kilku zamówieniach klienta.
        stock_pool = {}
        selection_rows = []
        items = []
        for item in order_items:
            product_id = int(item.get("product_id") or 0)
            available = stock_pool.setdefault(product_id, max(0, int(item.get("stock_qty") or 0)))
            max_pack_qty = min(max(0, int(item.get("qty") or 0)), available)
            pack_qty = max_pack_qty if request.method == "GET" else min(
                max_pack_qty,
                max(0, to_int(request.form.get(f"pack_qty_{item['id']}"), 0)),
            )
            stock_pool[product_id] = available - pack_qty
            source_order = candidate_by_id.get(int(item.get("order_id") or 0), {})
            item["source_order_id"] = int(item.get("order_id") or 0)
            item["source_order_no"] = canonical_order_no(
                source_order.get("id"), source_order.get("created_at"), source_order.get("order_no")
            )
            item["source_order_note"] = norm(source_order.get("note"))
            item["max_pack_qty"] = max_pack_qty
            item["selected_pack_qty"] = pack_qty
            selection_rows.append(item)
            if pack_qty <= 0:
                continue
            packed_item = dict(item)
            packed_item["qty"] = pack_qty
            items.append(packed_item)

        if request.method == "GET":
            tpl = r"""
            {% extends "base.html" %}{% block content %}
              <div class="card">
                <div class="flex"><div><h1 style="margin:0 0 8px;">Wybierz zawartość paczki</h1>
                  <div class="muted">{% if carrier == 'inpost' %}InPost: po zatwierdzeniu wybierzesz paczkę i wygenerujesz etykietę A6.{% else %}Inny przewoźnik: zostanie pobrana wyłącznie zbiorcza lista pakowa A4.{% endif %}</div>
                </div><a class="btn right" href="{{ url_for('order_view', order_id=order_id) }}">← Zamówienie</a></div>
              </div>
              <div class="card"><form method="post">
                <input type="hidden" name="carrier" value="{{ carrier }}">
                <table><thead><tr><th>Zamówienie</th><th>Notatka</th><th>SKU</th><th>Model / nazwa</th><th>Zamówiono</th><th>Dostępne do paczki</th><th>Pakuj</th></tr></thead><tbody>
                {% for item in rows %}<tr>
                  <td><b>{{ item.source_order_no }}</b></td><td>{{ item.source_order_note or '-' }}</td>
                  <td><b>{{ item.sku }}</b></td><td>{{ item.model or item.name or '' }}</td>
                  <td>{{ item.qty }}</td><td>{{ item.max_pack_qty }}</td>
                  <td><input type="number" min="0" max="{{ item.max_pack_qty }}" name="pack_qty_{{ item.id }}" value="{{ item.selected_pack_qty }}" style="width:110px;"></td>
                </tr>{% endfor %}</tbody></table>
                <button class="btn primary" type="submit" style="margin-top:16px;">
                  {% if carrier == 'inpost' %}Dalej: paczka i etykieta InPost{% else %}Pobierz zbiorczą listę A4{% endif %}
                </button>
              </form></div>
            {% endblock %}
            """
            return render_template_string(
                tpl, title="Zawartość paczki", base_url=BASE_URL, db_path=DB_PATH,
                rows=selection_rows, carrier=selected_carrier, order_id=order_id,
            )

        # Dopiero zatwierdzenie formularza zapisuje status pakowania.
        if not items:
            return "Wybierz co najmniej jedną sztukę do spakowania", 400

        packed_order_ids = sorted({int(item["source_order_id"]) for item in items})
        packing_state = {
            "root_order_id": int(order_id),
            "order_ids": packed_order_ids,
            "items": [
                [int(item.get("id") or item.get("order_item_id") or 0), int(item.get("qty") or 0)]
                for item in items
                if int(item.get("id") or item.get("order_item_id") or 0) > 0 and int(item.get("qty") or 0) > 0
            ],
        }
        packing_state["batch_id"] = save_packing_selection(order_id, items)
        session["latest_packing_selection"] = packing_state
        order_no = canonical_order_no(order_row["id"], order_row["created_at"], order_row["order_no"])
        meta = {
            "invoice_no": order_no,
            "document_label_key": "order",
            "buyer_name": norm(order_row["customer_name"]),
            "buyer_email": norm(order_row["customer_email"]),
        }
        pack_path = generate_invoice_packing_list_pdf(order_row, items, meta)
        mark_orders_packed(packed_order_ids, packing_path=pack_path, packing_items=items)
        filename_suffix = "_zbiorcza" if len(packed_order_ids) > 1 else ""
        if selected_carrier == "inpost":
            session[f"inpost_pack_path_{order_id}"] = pack_path
            return redirect(url_for("order_inpost_create", order_id=order_id, bundle="1"))
        return send_file(
            pack_path,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"{safe_filename(order_no)}{filename_suffix}_lista_pakowania.pdf",
        )




    @app.get("/invoices/<int:invoice_id>/packing-list")
    def invoice_packing_list_download_admin(invoice_id):
        inv = load_invoice_with_meta(invoice_id)
        if not inv:
            return "Nie znaleziono faktury", 404

        c = conn()
        cur = c.cursor()
        cur.execute("SELECT * FROM orders WHERE id=?", (inv["order_id"],))
        o = cur.fetchone()
        c.close()
        if not o:
            return "Brak powiązanego zamówienia", 404

        items = invoice_items_from_saved_json(invoice_id)
        if not items:
            return "Brak pozycji faktury", 400

        ok_pdf, invoice_abs_path = invoice_pdf_exists(inv.get("pdf_path", ""), inv.get("invoice_no", ""))
        pack_path = packing_list_pdf_path_for_invoice(invoice_abs_path if ok_pdf else "", inv.get("invoice_no") or f"FV_{invoice_id}")
        pack_path = generate_invoice_packing_list_pdf(o, items, invoice_meta_payload(inv), invoice_abs_path if ok_pdf else "")
        mark_orders_packed([
            int(item.get("source_order_id") or item.get("order_id") or inv.get("order_id") or 0)
            for item in items
        ], packing_path=pack_path, packing_items=items)
        if supabase_enabled():
            try:
                packing_ref = supabase_storage_upload_file(
                    pack_path,
                    invoice_packing_storage_object_path(invoice_id, inv.get("invoice_no") or f"FV_{invoice_id}"),
                    content_type="application/pdf",
                )
                data, filename = supabase_storage_download_bytes(packing_ref)
                return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=True, download_name=filename)
            except Exception:
                pass

        return send_file(pack_path, mimetype="application/pdf", as_attachment=True, download_name=os.path.basename(pack_path))


    exported = {'order_inpost_create': order_inpost_create, 'order_inpost_label': order_inpost_label, 'inpost_dispatch_order': inpost_dispatch_order, 'order_mark_shipped': order_mark_shipped, 'order_packing_list_download_admin': order_packing_list_download_admin, 'invoice_packing_list_download_admin': invoice_packing_list_download_admin}
    globals().update(exported)
    return exported
