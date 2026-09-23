"""Mechanically extracted Flask routes; business logic is unchanged."""

from china_delivery_attention import delivery_attention_states, delivery_has_problem

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
        if supabase_enabled() and not document_rows:
            hydrate_china_table("china_documents")
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
            if problem_only and not delivery_has_problem(pack): continue
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
            for attention in delivery_attention_states(p, current_time=now):
                alerts.append((p, attention["label"]))
                if attention["metric"]:
                    alert_metrics[attention["metric"]] += 1

        suppliers = sorted({norm(p.get("supplier")) for p in all_packs if norm(p.get("supplier"))})
        return render_template("china_list.html", title="Import", packs=filtered, kpis=kpis,
            alerts=alerts, alert_metrics=alert_metrics, suppliers=suppliers, contents=contents,
            tracking_api_enabled=tracking_enabled())




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
        if (os.environ.get("RENDER") and not supabase_enabled()
                and (not os.environ.get("APP_DATA_DIR")
                     or os.environ.get("REMANENT_PERSISTENCE_READY") != "1")):
            return redirect(url_for("china", document_error="Przed zapisem PDF potwierdź trwały dysk APP_DATA_DIR lub skonfiguruj prywatny Supabase Storage."))
        uploaded = request.files.get("document")
        if not uploaded or not norm(uploaded.filename):
            return redirect(url_for("china", document_error="Wybierz dokument PDF."))
        original_name = os.path.basename(norm(uploaded.filename))[:180]
        document_type = norm(request.form.get("document_type")).lower()
        if document_type not in {"invoice", "zc429", "order"}:
            return redirect(url_for("china", document_error="Wybierz typ dokumentu: Faktura, ZC429 lub Zamówienie."))
        if not original_name.lower().endswith(".pdf"):
            return redirect(url_for("china", document_error="Do przesyłki można dodać wyłącznie dokument PDF."))
        data = uploaded.read(10 * 1024 * 1024 + 1)
        if not data or len(data) > 10 * 1024 * 1024:
            return redirect(url_for("china", document_error="Dokument PDF musi mieć maksymalnie 10 MB."))
        if not data.startswith(b"%PDF-"):
            return redirect(url_for("china", document_error="Wybrany plik nie jest prawidłowym dokumentem PDF."))
        c = conn()
        if not c.execute("SELECT 1 FROM china_packages WHERE id=?", (package_id,)).fetchone():
            c.close(); abort(404)
        stored_name = f"po_{package_id}_{uuid.uuid4().hex}.pdf"
        docs_dir = os.path.join(os.path.dirname(DB_PATH), "china_documents")
        os.makedirs(docs_dir, exist_ok=True)
        local_path = os.path.join(docs_dir, stored_name)
        stored_path = ""
        document_id = None
        try:
            with open(local_path, "wb") as handle:
                handle.write(data)
            stored_path = (supabase_storage_upload_file(local_path, f"china_documents/{stored_name}")
                           if supabase_enabled() else local_path)
            c.execute("INSERT INTO china_documents(package_id,original_name,document_type,stored_path,size_bytes,created_at) VALUES(?,?,?,?,?,?)",
                      (package_id, original_name, document_type, stored_path, len(data), now_iso()))
            document_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.commit()
            if supabase_enabled():
                sync_local_rows_to_supabase("china_documents", "id", [document_id])
                try:
                    os.remove(local_path)
                except OSError:
                    app.logger.warning("Nie udało się usunąć tymczasowej kopii PDF %s", local_path)
        except Exception:
            c.rollback()
            if document_id is not None:
                c.execute("DELETE FROM china_documents WHERE id=?", (document_id,))
                c.commit()
            if parse_supabase_storage_ref(stored_path):
                try:
                    supabase_storage_delete(stored_path)
                except Exception:
                    app.logger.exception("Nie udało się wycofać osieroconego pliku %s", stored_name)
            if os.path.isfile(local_path):
                try:
                    os.remove(local_path)
                except OSError:
                    app.logger.exception("Nie udało się usunąć lokalnej kopii %s", stored_name)
            app.logger.exception("Nie udało się zapisać dokumentu przesyłki %s", package_id)
            return redirect(url_for("china", document_error="Nie udało się trwale zapisać dokumentu. Sprawdź konfigurację Storage i migrację bazy."))
        finally:
            c.close()
        return redirect(url_for("china", document_uploaded=1))

    def local_document_path(stored_path):
        if os.path.isfile(stored_path):
            return stored_path
        # Existing rows may refer to the old instance path. An operator can
        # restore those files into APP_DATA_DIR/china_documents with the same names.
        restored = os.path.join(os.path.dirname(DB_PATH), "china_documents", os.path.basename(stored_path))
        return restored if os.path.isfile(restored) else stored_path

    @app.get("/china/documents/<int:document_id>")
    def china_document_download(document_id):
        c = conn(); row = c.execute("SELECT * FROM china_documents WHERE id=?", (document_id,)).fetchone(); c.close()
        if not row:
            abort(404)
        if parse_supabase_storage_ref(row["stored_path"]):
            try:
                content, _ = supabase_storage_download_bytes(row["stored_path"])
            except Exception:
                app.logger.exception("Nie udało się pobrać dokumentu przesyłki %s", document_id)
                return "Nie udało się pobrać dokumentu z magazynu plików", 503
            return send_file(io.BytesIO(content), mimetype="application/pdf", as_attachment=False,
                             download_name=row["original_name"])
        local_path = local_document_path(row["stored_path"])
        if not os.path.isfile(local_path):
            return "Plik dokumentu nie jest dostępny na tym serwerze", 503
        return send_file(local_path, mimetype="application/pdf", as_attachment=False,
                         download_name=row["original_name"])

    @app.post("/china/documents/<int:document_id>/delete")
    def china_document_delete(document_id):
        c = conn(); row = c.execute("SELECT stored_path FROM china_documents WHERE id=?", (document_id,)).fetchone()
        if not row:
            c.close(); abort(404)
        if supabase_enabled():
            try:
                supabase_delete_rows("china_documents", {"id": document_id})
            except Exception:
                c.close()
                return "Nie udało się usunąć dokumentu z trwałej bazy", 503
        c.execute("DELETE FROM china_documents WHERE id=?", (document_id,)); c.commit(); c.close()
        try:
            if parse_supabase_storage_ref(row["stored_path"]):
                supabase_storage_delete(row["stored_path"])
            elif os.path.isfile(local_document_path(row["stored_path"])):
                os.remove(local_document_path(row["stored_path"]))
        except Exception:
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
        try:
            import xlrd
            from xlutils.copy import copy as copy_xls
        except ImportError:
            return "Brakuje bibliotek generatora Excel", 503
        template_path = os.path.join(app.root_path, "assets", "nurlin_order_template.xls")
        if not os.path.isfile(template_path):
            app.logger.error("Brak wzoru Nurlin: %s", template_path)
            return "Brakuje pliku assets/nurlin_order_template.xls w repozytorium", 503
        source = xlrd.open_workbook(template_path, formatting_info=True)
        source_sheet = source.sheet_by_index(0)
        item_start_row = 8
        item_capacity = max(0, source_sheet.nrows - item_start_row)
        if len(items) > item_capacity:
            return f"Wzór Nurlin mieści maksymalnie {item_capacity} pozycji", 409
        output = copy_xls(source)
        sheet = output.get_sheet(0)
        # Wzorzec zapamiętał przewinięcie do odległego wiersza. Każdy nowy
        # dokument ma otwierać się od lewego górnego rogu arkusza.
        sheet.set_first_visible_row(0)
        sheet.set_first_visible_col(0)
        # xlutils nie kopiuje osadzonych grafik ze starego formatu XLS.
        # Wstawiamy ponownie znak NURLIN z danych BMP osadzonych w aplikacji,
        # dzięki czemu do wdrożenia wystarcza china.py i właściwy wzorzec XLS.
        import base64
        nurlin_logo_bmp = base64.b64decode(
            "Qk2qTwAAAAAAADYAAAAoAAAAlgAAAC0AAAABABgAAAAAAHRPAADEDgAAxA4AAAAAAAAAAAAAj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQAACPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVAAAI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUAAAj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQAACPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVAAAI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUAAAj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQAACPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVAAAI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUAAAj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQAACPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVAAAI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUAAAj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQAACPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVAAAI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUJ97af///////////6iHdo9lUI9lUI9lUI9lUI9lUJFnU+3n5P///////////5RsWI9lUI9lUI9lUI9lUI9lUJZvXMeyqObd2fbz8f39/Pz7+/Xx7+PZ1MOtopVtWo9lUI9lUI9lUI9lUI9lUKKAbv///////////8CpnY9lUI9lUI9lUI9lUI9lUI9lUI9lUOHW0P///////////9bGvo9lUJhxXv///////////////////////////////////////////////8m1q49lULCThP///////////7SYio9lUI9lUJ97af///////////6iHdo9lUI9lUI9lUI9lUI9lUJFnU+3n5P///////////5RsWI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUAAAj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQn3tp////////////qId2j2VQj2VQj2VQj2VQj2VQv6eb////////////////lGxYj2VQj2VQj2VQj2VQq4t79/Ty////////////////////////////////9fLwqId2j2VQj2VQj2VQj2VQooBu////////////wKmdj2VQj2VQj2VQj2VQj2VQj2VQu6KV////////////+ff2mnNgj2VQmHFe////////////////////////////////////////////////ybWrj2VQsJOE////////////tJiKj2VQj2VQn3tp////////////qId2j2VQj2VQj2VQj2VQj2VQv6eb////////////////lGxYj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQAACPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCfe2n///////////+oh3aPZVCPZVCPZVCPZVCWb1v39PL///////////////+UbFiPZVCPZVCPZVCfe2n7+fj////////////////////////////////////////49fOadGGPZVCPZVCPZVCigG7////////////AqZ2PZVCPZVCPZVCPZVCPZVCbdmP59/X////////////CrKCPZVCPZVCYcV7////////////////////////////////////////////////JtauPZVCwk4T///////////+0mIqPZVCPZVCfe2n///////////+oh3aPZVCPZVCPZVCPZVCWb1v39PL///////////////+UbFiPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVAAAI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUJ97af///////////6iHdo9lUI9lUI9lUI9lUM27sf///////////////////5RsWI9lUI9lUI9lUNfHwP///////////+jf26yNfpZuWpVtWamJeebc1////////////8q2rI9lUI9lUI9lUKKAbv///////////8CpnY9lUI9lUI9lUI9lUI9lUN7SzP///////////+7n5JFoU49lUI9lUJhxXv///////////8u3rY9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lULCThP///////////7SYio9lUI9lUJ97af///////////6iHdo9lUI9lUI9lUI9lUM27sf///////////////////5RsWI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUAAAj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQn3tp////////////qId2j2VQj2VQj2VQn3tp/fz8////////////////////lGxYj2VQj2VQj2VQ9/Ty////////8+/slW1aj2VQj2VQj2VQj2VQlW1Z9/Ty////////6uLej2VQj2VQj2VQooBu////////////wKmdj2VQj2VQj2VQj2VQtZmL////////////////ro+Aj2VQj2VQj2VQmHFe////////////y7etj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQsJOE////////////tJiKj2VQj2VQn3tp////////////qId2j2VQj2VQj2VQn3tp/fz8////////////////////lGxYj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQAACPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCfe2n///////////+oh3aPZVCPZVCPZVDcz8n////////h1tD///////////+UbFiPZVCPZVCWb1z////////////NurGPZVCPZVCPZVCPZVCPZVCPZVDazMX////////59/WPZVCPZVCPZVCigG7////////////AqZ2PZVCPZVCPZVCVbVnz7+z////////////XycGPZVCPZVCPZVCPZVCYcV7////////////Lt62PZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCwk4T///////////+0mIqPZVCPZVCfe2n///////////+oh3aPZVCPZVCPZVDcz8n////////h1tD///////////+UbFiPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVAAAI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUJ97af///////////6iHdo9lUI9lUKuMfP///////+7o5bOWiP///////////5RsWI9lUI9lUJ97af///////////8Gpno9lUI9lUI9lUI9lUI9lUI9lUNC/tv///////////5BnUo9lUI9lUKKAbv///////////8CpnY9lUI9lUI9lUNPCuv////////////Hr6JZvXI9lUI9lUI9lUI9lUJhxXv///////////8u3rY9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lULCThP///////////7SYio9lUI9lUJ97af///////////6iHdo9lUI9lUKuMfP///////+7o5bOWiP///////////5RsWI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUAAAj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQn3tp////////////qId2j2VQkGZR6eHd////////sZSGsJOE////////////lGxYj2VQj2VQpIJx////////////vaSYj2VQj2VQj2VQj2VQj2VQj2VQzbux////////////lGxYj2VQj2VQooBu////////////wKmdkWdTnXhlz720////////////7+nmnXhmj2VQj2VQj2VQj2VQj2VQmHFe////////////y7etj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQsJOE////////////tJiKj2VQj2VQn3tp////////////qId2j2VQkGZR6eHd////////sZSGsJOE////////////lGxYj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQAACPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCfe2n///////////+oh3aPZVC6oJP////////i19KPZVCwk4T///////////+UbFiPZVCPZVClhHP///////////+9pJePZVCPZVCPZVCPZVCPZVCPZVDMurD///////////+VbVqPZVCPZVCigG7////////////////////////////////////8+/rBqp6bdWOPZVCPZVCPZVCPZVCPZVCYcV7////////////Lt62PZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCwk4T///////////+0mIqPZVCPZVCfe2n///////////+oh3aPZVC6oJP////////i19KPZVCwk4T///////////+UbFiPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVAAAI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUJ97af///////////6iHdpRsWPTw7v////7+/qSBcI9lULCThP///////////5RsWI9lUI9lUKaEdP///////////72kl49lUI9lUI9lUI9lUI9lUI9lUMy6sP///////////5ZvW49lUI9lUKKAbv////////////////////////////////////////////79/dC/tpJpVI9lUI9lUI9lUJhxXv///////////8u3rY9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lULCThP///////////7SYio9lUI9lUJ97af///////////6iHdpRsWPTw7v////7+/qSBcI9lULCThP///////////5RsWI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUAAAj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQn3tp////////////qId2ybSq////////1MO7j2VQj2VQsJOE////////////lGxYj2VQj2VQpoR0////////////vaSXj2VQj2VQj2VQj2VQj2VQj2VQzLqw////////////lm9bj2VQj2VQooBu////////////////////////////////////////////////////1se/j2VQj2VQj2VQmHFe////////////y7etj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQsJOE////////////tJiKj2VQj2VQn3tp////////////qId2ybSq////////1MO7j2VQj2VQsJOE////////////lGxYj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQAACPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCfe2n///////////+0mIr7+vn////6+Peac2CPZVCPZVCwk4T///////////+UbFiPZVCPZVCmhHT///////////+9pJePZVCPZVCPZVCPZVCPZVCPZVDMurD///////////+Wb1uPZVCPZVCigG7////////////AqZ2PZVCPZVCPZVCRZ1OXcFytj3/x7Or///////////+hfm2PZVCPZVCYcV7////////////Lt62PZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCwk4T///////////+0mIqPZVCPZVCfe2n///////////+0mIr7+vn////6+Peac2CPZVCPZVCwk4T///////////+UbFiPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVAAAI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUJ97af///////////+3n5P///////8WvpI9lUI9lUI9lULCThP///////////5RsWI9lUI9lUKaEdP///////////72kl49lUI9lUI9lUI9lUI9lUI9lUMy6sP///////////5ZvW49lUI9lUKKAbv///////////8CpnY9lUI9lUI9lUI9lUI9lUI9lULecjv///////////7yjl49lUI9lUJhxXv///////////8u3rY9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lULCThP///////////7SYio9lUI9lUJ97af///////////+3n5P///////8WvpI9lUI9lUI9lULCThP///////////5RsWI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUAAAj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQn3tp////////////////////8u3qk2pVj2VQj2VQj2VQsJOE////////////lGxYj2VQj2VQpoR0////////////vaSXj2VQj2VQj2VQj2VQj2VQj2VQzLqw////////////lm9bj2VQj2VQooBu////////////wKmdj2VQj2VQj2VQj2VQj2VQj2VQtZqM////////////vqaaj2VQj2VQmHFe////////////y7etj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQsJOE////////////tJiKj2VQj2VQn3tp////////////////////8u3qk2pVj2VQj2VQj2VQsJOE////////////lGxYj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQAACPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCfe2n///////////////////+2m42PZVCPZVCPZVCPZVCwk4T///////////+UbFiPZVCPZVCmhHT///////////+9pJePZVCPZVCPZVCPZVCPZVCPZVDMurD///////////+Wb1uPZVCPZVCigG7////////////AqZ2PZVCPZVCPZVCPZlGSaVWqinrv6eb///////////+tjn+PZVCPZVCYcV7////////////Lt62PZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCwk4T///////////+0mIqPZVCPZVCfe2n///////////////////+2m42PZVCPZVCPZVCPZVCwk4T///////////+UbFiPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVAAAI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUJ97af///////////////+fe2Y9mUY9lUI9lUI9lUI9lULCThP///////////5RsWI9lUI9lUKaEdP///////////72kl49lUI9lUI9lUI9lUI9lUI9lUMy6sP///////////5ZvW49lUI9lUKKAbv///////////////////////////////////////////////////+/p5pBmUY9lUI9lUJhxXv///////////8u3rY9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lULCThP///////////7SYio9lUI9lUJ97af///////////////+fe2Y9mUY9lUI9lUI9lUI9lULCThP///////////5RsWI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUAAAj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQn3tp////////////////qId3j2VQj2VQj2VQj2VQj2VQsJOE////////////lGxYj2VQj2VQpoR0////////////vaSXj2VQj2VQj2VQj2VQj2VQj2VQzLqw////////////lm9bj2VQj2VQooBu////////////////////////////////////////////////+Pb1pIJxj2VQj2VQj2VQmHFe////////////y7etj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQsJOE////////////tJiKj2VQj2VQn3tp////////////////qId3j2VQj2VQj2VQj2VQj2VQsJOE////////////lGxYj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQAACPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCfe2n////////////YysOPZVCPZVCPZVCPZVCPZVCPZVCwk4T///////////+UbFiPZVCPZVCmhHT///////////+9pJePZVCPZVCPZVCPZVCPZVCPZVDMurD///////////+Wb1uPZVCPZVCigG7//////////////////////////////v78+/r18vDn3trMuK+Zc2CPZVCPZVCPZVCPZVCYcV7////////////Lt62PZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCwk4T///////////+0mIqPZVCPZVCfe2n////////////YysOPZVCPZVCPZVCPZVCPZVCPZVCwk4T///////////+UbFiPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVAAAI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUAAAj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQAACPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVAAAI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUAAAj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQAACPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVAAAI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUAAAj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQAACPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVAAAI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUAAAj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQj2VQAACPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVCPZVAAAI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUI9lUAAA"
        )
        # OpenOffice skaluje pionowo bitmapę BIFF inaczej niż Excel. Korekta
        # skali utrzymuje znak w obrębie pierwszego wiersza bez nachodzenia na
        # instrukcję i dane nadawcy.
        logo_scale_x = 0.96
        logo_scale_y = 0.30
        logo_width = 150 * logo_scale_x
        logo_height = 45 * logo_scale_y
        merged_header_width = sum(sheet.col_width(column) for column in range(5))
        logo_x = max(0, int((merged_header_width - logo_width) / 2))
        logo_y = max(0, int((sheet.row_height(0) - logo_height) / 2))
        sheet.insert_bitmap_data(
            nurlin_logo_bmp,
            0,
            0,
            x=logo_x,
            y=logo_y,
            scale_x=logo_scale_x,
            scale_y=logo_scale_y,
        )
        # Długi adres nadawcy w B3:E3 zawija się w OpenOffice do dwóch linii.
        # Wzorzec ma wysokość jednej linii, przez co druga była ucinana.
        sender_row = sheet.row(2)
        sender_row.height = max(sender_row.height, 480)
        sender_row.height_mismatch = True

        def write_with_template_style(row, column, value):
            """Zapisuje wartość bez usuwania formatowania komórki wzorca XLS."""
            output_row = sheet._Worksheet__rows.get(row)
            output_cell = output_row._Row__cells.get(column) if output_row else None
            output_style_index = output_cell.xf_idx if output_cell is not None else None
            sheet.write(row, column, value)
            # Indeksy XF w skoroszycie zapisanym przez xlutils nie odpowiadają
            # indeksom xlrd 1:1. Zachowujemy więc indeks stylu istniejącej
            # komórki z już skopiowanego arkusza, a nie numer z pliku źródłowego.
            if output_style_index is not None:
                sheet._Worksheet__rows[row]._Row__cells[column].xf_idx = output_style_index

        sender_details = norm(source_sheet.cell_value(2, 1)).replace(
            "PL8661754936", "PL8661754935"
        )
        write_with_template_style(2, 1, sender_details)
        write_with_template_style(5, 1, norm(pack["shipping_method"]) or "AIR FedEx Express DAP")
        for index in range(item_capacity):
            row = item_start_row + index
            write_with_template_style(row, 0, index + 1)
            # Wzór przekazany przez dostawcę zawiera przykładowe wcześniejsze
            # pozycje, ceny i wagi. Nowe zamówienie nie może ich odziedziczyć.
            for column in range(1, 6):
                write_with_template_style(row, column, "")
            write_with_template_style(row, 1, items[index]["sku"] if index < len(items) else "")
            write_with_template_style(row, 3, int(items[index]["qty"]) if index < len(items) else "")
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
