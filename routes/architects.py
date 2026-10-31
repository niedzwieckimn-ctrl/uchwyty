"""Architect portal API. Only this module shapes data sent to architect browsers."""

import secrets
import json
import csv
import io
import re
import unicodedata
import urllib.error
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from architect_pricing import ProgramRules, money, price_line
from architect_account_email import request_attempt, read_delivery, delivery_state


def payout_timestamp(value, now):
    """A date selected today must not become an arbitrary future noon UTC."""
    try:
        day = datetime.strptime(value, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        raise ValueError('Podaj poprawną datę wypłaty') from None
    if day > now.date():
        raise ValueError('Data wypłaty nie może być w przyszłości')
    paid_at = now if day == now.date() else now.replace(
        year=day.year, month=day.month, day=day.day,
        hour=12, minute=0, second=0, microsecond=0)
    return paid_at.isoformat()


def register_routes(context):
    globals().update(context)

    def origin_ok():
        origin = norm(request.headers.get("Origin")).rstrip("/")
        return not origin or origin in ARCHITECT_ALLOWED_ORIGINS

    def profile():
        rows = supabase_request("/rest/v1/architect_profiles", params={
            "select": "id,first_name,last_name,studio_name,email,phone,instagram,website,shipping_address,nip,status,sample_entitlements,created_at",
            "id": f"eq.{g.architect_user['id']}", "limit": 1,
        }, timeout=15) or []
        return rows[0] if rows else None

    def approved():
        if not g.architect_user.get('email_confirmed'):
            return None
        row = profile()
        return row if row and row.get("status") == "approved" else None

    def audit(actor, action, entity_type, entity_id, details=None):
        supabase_request("/rest/v1/architect_audit_events", method="POST", payload={
            "actor_id": str(actor), "action": action, "entity_type": entity_type,
            "entity_id": str(entity_id), "details": details or {},
        }, prefer="return=minimal", timeout=15)

    @app.route("/api/architect/me", methods=["GET", "POST", "OPTIONS"])
    def architect_me():
        if request.method == "OPTIONS":
            return "", 204
        if request.method == "GET":
            return jsonify(ok=True, profile=profile())
        if not origin_ok() or not _rate_limit('architect_profile_create',3,3600):
            return jsonify(ok=False, error="Niedozwolone źródło żądania"), 403
        if not g.architect_user.get("email_confirmed"):
            return jsonify(ok=False, error="Potwierdź adres e-mail przed utworzeniem profilu"), 403
        existing = profile()
        if existing:
            return jsonify(ok=True, profile=existing)
        metadata = g.architect_user.get("metadata") or {}
        values = {key: norm(metadata.get(key)) for key in (
            "first_name", "last_name", "studio_name", "phone", "instagram",
            "website", "shipping_address", "nip"
        )}
        if any(not values[key] for key in (
            "first_name", "last_name", "studio_name", "phone", "instagram", "shipping_address"
        )) or len(values["shipping_address"]) < 8:
            return jsonify(ok=False, error="Uzupełnij wymagane dane rejestracji"), 400
        if any(len(value) > 250 for value in values.values()):
            return jsonify(ok=False, error="Dane rejestracji są zbyt długie"), 400
        try:
            created = supabase_request("/rest/v1/architect_profiles", method="POST", payload={
                "id": g.architect_user["id"], "email": g.architect_user["email"], **values,
            }, prefer="return=representation", timeout=15) or []
        except urllib.error.HTTPError as exc:
            # getSession and onAuthStateChange can start registration together.
            # A second request must receive the profile created by the first.
            if exc.code == 409:
                existing = profile()
                if existing:
                    return jsonify(ok=True, profile=existing)
            raise
        audit(g.architect_user["id"], "registered", "architect_profile", g.architect_user["id"])
        return jsonify(ok=True, profile=created[0] if created else profile()), 201

    @app.route("/api/architect/catalog", methods=["GET", "OPTIONS"])
    def architect_catalog():
        if request.method == "OPTIONS":
            return "", 204
        if not approved():
            return jsonify(ok=False, error="Konto oczekuje na zatwierdzenie"), 403
        # Select only public catalog fields from the common source. Never return
        # stock, costs, reservations, deliveries or the wholesale price.
        products = supabase_request("/rest/v1/client_stock_catalog_v", params={
            "select": "product_id,sku,model,name,net_price", "order": "sku.asc", "limit": 5000,
        }, timeout=30) or []
        details = supabase_request("/rest/v1/architect_product_details", params={
            "select": "product_id,description,material,mounting_spacing,dimensions,finish,technical_data,model_3d_url,vat_rate,retail_gross_override",
            "limit": 5000,
        }, timeout=20) or []
        by_id = {to_int(d.get("product_id"), 0): d for d in details}
        assignments = supabase_request("/rest/v1/product_image_assignments", params={
            "select": "product_id,image_id", "limit": 5000,
        }, timeout=20) or []
        image_by_product = {to_int(x.get("product_id"), 0): to_int(x.get("image_id"), 0) for x in assignments}
        images = supabase_request("/rest/v1/product_images", params={
            "select": "id,stored_path", "limit": 5000,
        }, timeout=20) or []
        path_by_id = {to_int(x.get("id"), 0): norm(x.get("stored_path")) for x in images}
        result = []
        for raw in products:
            pid = to_int(raw.get("product_id"), 0)
            base_net = Decimal(str(raw.get("net_price") or 0))
            if pid <= 0 or base_net <= 0:
                continue
            info = by_id.get(pid) or {}
            vat = Decimal(str(info.get("vat_rate") or "0.23"))
            retail = money(info.get("retail_gross_override") or base_net * Decimal("1.45") * (1 + vat))
            image_url = ""
            stored_path = path_by_id.get(image_by_product.get(pid, 0), "")
            storage_ref = parse_supabase_storage_ref(stored_path) if stored_path else None
            if storage_ref:
                try:
                    bucket, image_path = storage_ref
                    image_url = supabase_storage_create_signed_urls(bucket, [image_path], expires_in=3600).get(image_path, "")
                except Exception:
                    app.logger.warning("Architect catalog image unavailable: product_id=%s", pid)
            result.append({
                "product_id": pid, "sku": raw.get("sku"), "model": raw.get("model"),
                "name": raw.get("name"), "retail_gross_pln": str(retail),
                "vat_rate": str(vat), "image_url": image_url,
                "description": info.get("description") or "", "material": info.get("material") or "Lity mosiądz",
                "mounting_spacing": info.get("mounting_spacing") or "",
                "dimensions": info.get("dimensions") or "", "finish": info.get("finish") or "",
                "technical_data": info.get("technical_data") or {},
                "model_3d_url": info.get("model_3d_url") or "",
            })
        return jsonify(ok=True, products=result)

    @app.route("/api/architect/distributors", methods=["GET", "OPTIONS"])
    def architect_distributors():
        if request.method == "OPTIONS":
            return "", 204
        if not approved():
            return jsonify(ok=False, error="Konto oczekuje na zatwierdzenie"), 403
        rows = supabase_request("/rest/v1/distributor_program_memberships", params={
            "select": "customer_id,store_name,city,store_address,contact,latitude,longitude",
            "active": "eq.true", "order": "store_name.asc", "limit": 1000,
        }, timeout=20) or []
        return jsonify(ok=True, distributors=rows)

    @app.route("/api/architect/codes", methods=["GET", "POST", "OPTIONS"])
    def architect_codes():
        if request.method == "OPTIONS":
            return "", 204
        owner = approved()
        if not owner:
            return jsonify(ok=False, error="Konto oczekuje na zatwierdzenie"), 403
        if request.method == "GET":
            rows = supabase_request("/rest/v1/architect_codes", params={
                "select": "id,code,distributor_id,status,created_at,expires_at",
                "architect_id": f"eq.{owner['id']}", "order": "created_at.desc", "limit": 200,
            }, timeout=20) or []
            return jsonify(ok=True, codes=rows)
        if not origin_ok() or not _rate_limit("architect_code_create", 5, 3600):
            return jsonify(ok=False, error="Nie można teraz wygenerować kodu"), 429
        distributor_id = to_int((request.get_json(silent=True) or {}).get("distributor_id"), 0)
        if distributor_id <= 0:
            return jsonify(ok=False, error="Wybierz dystrybutora"), 400
        memberships = supabase_request("/rest/v1/distributor_program_memberships", params={
            "select": "customer_id", "customer_id": f"eq.{distributor_id}", "active": "eq.true", "limit": 1,
        }, timeout=15) or []
        if not memberships:
            return jsonify(ok=False, error="Dystrybutor nie uczestniczy w programie"), 400
        settings = supabase_request("/rest/v1/architect_program_settings", params={
            "select": "version,code_valid_days", "active": "eq.true", "limit": 1,
        }, timeout=15) or []
        if not settings:
            return jsonify(ok=False, error="Program jest niedostępny"), 503
        prefix = unicodedata.normalize("NFKD", owner["last_name"]).encode("ascii", "ignore").decode("ascii").upper()
        prefix = re.sub(r"[^A-Z0-9]+", "_", prefix).strip("_")[:28]
        if not prefix:
            return jsonify(ok=False, error="Nie można utworzyć nazwy kodu"), 400
        expires = datetime.now(timezone.utc) + timedelta(days=int(settings[0]["code_valid_days"]))
        for _ in range(20):
            code = f"{prefix}-{secrets.randbelow(10000):04d}"
            try:
                issued = supabase_request("/rest/v1/rpc/architect_issue_code", method="POST", payload={
                    "p_architect_id": owner["id"], "p_distributor_id": distributor_id,
                    "p_code": code,
                }, timeout=15)
                if issued:
                    return jsonify(ok=True, code=issued), 201
            except urllib.error.HTTPError as exc:
                if exc.code != 409:
                    raise
        return jsonify(ok=False, error="Nie udało się przydzielić kodu"), 503

    @app.route("/api/architect/samples", methods=["GET", "POST", "OPTIONS"])
    def architect_samples():
        if request.method == "OPTIONS":
            return "", 204
        owner = approved()
        if not owner:
            return jsonify(ok=False, error="Konto oczekuje na zatwierdzenie"), 403
        if request.method == "GET":
            rows = supabase_request("/rest/v1/architect_sample_orders", params={
                "select": "id,entitlement_no,status,tracking_no,carrier,created_at,shipped_at",
                "architect_id": f"eq.{owner['id']}", "order": "created_at.desc", "limit": 30,
            }, timeout=20) or []
            if rows:
                ids=','.join(x['id'] for x in rows)
                items=supabase_request('/rest/v1/architect_sample_items',params={
                    'select':'sample_order_id,product_id,qty',
                    'sample_order_id':f'in.({ids})','limit':100},timeout=15) or []
                product_ids=','.join(str(x) for x in sorted({to_int(x['product_id'],0) for x in items}))
                names=supabase_request('/rest/v1/products',params={
                    'select':'id,sku,name','id':f'in.({product_ids})','limit':100},timeout=15) or [] if product_ids else []
                names_by_id={to_int(x['id'],0):x for x in names}
                for row in rows:
                    row['items']=[{'product_id':x['product_id'],'qty':x['qty'],
                                   'sku':names_by_id.get(to_int(x['product_id'],0),{}).get('sku',''),
                                   'name':names_by_id.get(to_int(x['product_id'],0),{}).get('name','')}
                                  for x in items if x['sample_order_id']==row['id']]
            return jsonify(ok=True, samples=rows)
        if not origin_ok() or not _rate_limit("architect_sample_create", 3, 3600):
            return jsonify(ok=False, error="Nie można teraz złożyć wniosku"), 429
        data = request.get_json(silent=True) or {}
        product_ids = data.get("product_ids")
        if not isinstance(product_ids, list) or not 1 <= len(product_ids) <= 3 or any(
            isinstance(x, bool) or not isinstance(x, int) or x <= 0 for x in product_ids
        ) or len(set(product_ids)) != len(product_ids):
            return jsonify(ok=False, error="Wybierz 1–3 różne uchwyty"), 400
        result = supabase_request("/rest/v1/rpc/architect_submit_samples", method="POST", payload={
            "p_architect_id": owner["id"], "p_product_ids": product_ids,
            "p_shipping_address": owner["shipping_address"],
        }, timeout=20)
        return jsonify(ok=True, sample_order_id=result), 201

    @app.route("/api/architect/earnings", methods=["GET", "OPTIONS"])
    def architect_earnings():
        if request.method == "OPTIONS":
            return "", 204
        owner = profile()
        if not owner:
            return jsonify(ok=False, error="Profil nie istnieje"), 404
        rows = supabase_request("/rest/v1/architect_commission_ledger", params={
            "select": "id,amount_net,entry_type,status,reason,sale_date,created_at",
            "architect_id": f"eq.{owner['id']}", "order": "created_at.desc", "limit": 500,
        }, timeout=20) or []
        settlements = supabase_request('/rest/v1/architect_monthly_settlements',params={
            'select':'id,period_month,batch_no,status,total_net',
            'architect_id':f"eq.{owner['id']}",'order':'period_month.desc','limit':100},timeout=20) or []
        return jsonify(ok=True, ledger=rows, settlements=settlements)

    @app.route('/api/architect/dashboard',methods=['GET','OPTIONS'])
    def architect_dashboard():
        if request.method == 'OPTIONS': return '',204
        owner=profile()
        if not owner: return jsonify(ok=False,error='Profil nie istnieje'),404
        codes=supabase_request('/rest/v1/architect_codes',params={
            'select':'status,expires_at','architect_id':f"eq.{owner['id']}",'limit':5000},timeout=20) or []
        ledger=supabase_request('/rest/v1/architect_commission_ledger',params={
            'select':'entry_type,status,amount_net,retail_net_after,sale_line_id',
            'architect_id':f"eq.{owner['id']}",'limit':5000},timeout=20) or []
        now=datetime.now(timezone.utc)
        active=sum(1 for x in codes if x['status']=='active' and
                   datetime.fromisoformat(x['expires_at'].replace('Z','+00:00'))>now)
        accruals=[x for x in ledger if x['entry_type']=='accrual']
        totals={state:money(sum((Decimal(str(x['amount_net'])) for x in ledger
                                 if x['status']==state),Decimal(0)))
                for state in ('pending','approved','settled','paid')}
        return jsonify(ok=True,summary={
            'active_codes':active,
            'redeemed_codes':sum(x['status']=='redeemed' for x in codes),
            'confirmed_sale_lines':len({x['sale_line_id'] for x in accruals}),
            'retail_net':str(money(sum((Decimal(str(x.get('retail_net_after') or 0))
                                        for x in accruals),Decimal(0)))),
            'pending':str(totals['pending']),
            'approved':str(totals['approved']+totals['settled']),
            'paid':str(totals['paid'])})

    @app.route("/api/client/architect-code/quote", methods=["POST", "OPTIONS"])
    def client_architect_code_quote():
        if request.method == "OPTIONS":
            return "", 204
        if not _client_order_origin_allowed() or not _rate_limit("architect_code_quote", 12, 60):
            return jsonify(ok=False, error="Nie można teraz sprawdzić kodu"), 429
        data = request.get_json(silent=True) or {}
        code = norm(data.get("code")).upper()
        raw_items = data.get("items")
        if not re.fullmatch(r"[A-Z0-9_]{1,28}-[0-9]{4}", code) or not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 100:
            return jsonify(ok=False, error="Kod lub koszyk są nieprawidłowe"), 400
        customer = _client_profile_for_email(g.client_user["email"], customer_id=g.client_customer_id)
        if customer.get("price_list") != "pln":
            return jsonify(ok=False, error="Kod działa tylko dla cennika PLN"), 400
        codes = supabase_request("/rest/v1/architect_codes", params={
            "select": "id,architect_id,distributor_id,status,expires_at,program_version",
            "code": f"eq.{code}", "limit": 1,
        }, timeout=15) or []
        if not codes or codes[0].get("status") != "active" or to_int(codes[0].get("distributor_id"), 0) != to_int(customer.get("id"), 0):
            return jsonify(ok=False, error="Kod nie jest dostępny dla tego sklepu"), 400
        if datetime.fromisoformat(codes[0]["expires_at"].replace("Z", "+00:00")) <= datetime.now(timezone.utc):
            return jsonify(ok=False, error="Kod stracił ważność"), 400
        architect = supabase_request("/rest/v1/architect_profiles", params={
            "select": "id", "id": f"eq.{codes[0]['architect_id']}", "status": "eq.approved", "limit": 1,
        }, timeout=15) or []
        member = supabase_request("/rest/v1/distributor_program_memberships", params={
            "select": "customer_id", "customer_id": f"eq.{customer['id']}", "active": "eq.true", "limit": 1,
        }, timeout=15) or []
        if not architect or not member:
            return jsonify(ok=False, error="Kod nie jest dostępny dla tego sklepu"), 400
        settings = supabase_request("/rest/v1/architect_program_settings", params={
            "select": "version,customer_discount_rate,manufacturer_share,commission_rate",
            "version": f"eq.{codes[0]['program_version']}", "limit": 1,
        }, timeout=15) or []
        if not settings:
            return jsonify(ok=False, error="Nie można sprawdzić zasad programu"), 503
        s = settings[0]
        rules = ProgramRules(int(s["version"]), Decimal(str(s["customer_discount_rate"])),
                             Decimal(str(s["manufacturer_share"])), Decimal(str(s["commission_rate"])))
        product_ids = []
        for item in raw_items:
            if not isinstance(item, dict):
                return jsonify(ok=False, error="Nieprawidłowa pozycja koszyka"), 400
            pid, qty = item.get("product_id"), item.get("qty")
            if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
                return jsonify(ok=False, error="Nieprawidłowa pozycja koszyka"), 400
            product_ids.append(pid)
        if len(set(product_ids)) != len(product_ids):
            return jsonify(ok=False, error="Powtórzony produkt w koszyku"), 400
        filter_ids = ",".join(map(str, product_ids))
        catalog = supabase_request("/rest/v1/client_stock_catalog_v", params={
            "select": "product_id,sku,net_price", "product_id": f"in.({filter_ids})", "limit": len(product_ids),
        }, timeout=20) or []
        details = supabase_request("/rest/v1/architect_product_details", params={
            "select": "product_id,vat_rate,retail_gross_override", "product_id": f"in.({filter_ids})",
            "limit": len(product_ids),
        }, timeout=20) or []
        by_id = {to_int(x.get("product_id"), 0): x for x in catalog}
        detail_by_id = {to_int(x.get("product_id"), 0): x for x in details}
        lines = []
        for item in raw_items:
            pid = item["product_id"]
            price = by_id.get(pid)
            if not price or Decimal(str(price.get("net_price") or 0)) <= 0:
                return jsonify(ok=False, error="Koszyk zawiera produkt bez ceny"), 400
            detail = detail_by_id.get(pid) or {}
            vat = Decimal(str(detail.get("vat_rate") or "0.23"))
            if vat != Decimal('0.23'):
                return jsonify(ok=False, error="Produkt z inną stawką VAT wymaga ręcznej wyceny"), 409
            retail = detail.get("retail_gross_override") or money(Decimal(str(price["net_price"])) * Decimal("1.45") * (1 + vat))
            try:
                priced = price_line(wholesale_unit_net=price["net_price"], retail_unit_gross=retail,
                                    vat_rate=vat, qty=item["qty"], rules=rules)
            except ValueError:
                return jsonify(ok=False, error="Rabat dla tej pozycji wymaga weryfikacji"), 409
            lines.append({"product_id": pid, "sku": price.get("sku"), "qty": priced.qty,
                          "unit_net_price": str(priced.wholesale_unit_after),
                          "unit_gross_price": str(money(priced.wholesale_unit_after * (1 + vat))),
                          "retail_gross_after": str(priced.retail_unit_gross_after),
                          "manufacturer_discount_net": str(money(priced.manufacturer_discount_unit_net * priced.qty)),
                          "line_net": str(priced.wholesale_net_after),
                          "line_gross": str(money(priced.wholesale_net_after * (1 + vat)))})
        total_net = sum((Decimal(x["line_net"]) for x in lines), Decimal(0))
        total_discount = sum((Decimal(x["manufacturer_discount_net"]) for x in lines), Decimal(0))
        return jsonify(ok=True, code=code, program_version=rules.version,
                       lines=lines, total_net=str(money(total_net)),
                       total_gross=str(money(total_net * Decimal('1.23'))),
                       manufacturer_discount_net=str(money(total_discount)))

    def client_sale_context(sale_line_id):
        lines = supabase_request('/rest/v1/architect_sale_lines', params={
            'select':'id,redemption_id,qty,retail_gross_before,retail_gross_after,vat_rate',
            'id':f'eq.{sale_line_id}','limit':1}, timeout=15) or []
        if not lines:
            return None
        redemptions = supabase_request('/rest/v1/architect_code_redemptions', params={
            'select':'id,order_id,distributor_id',
            'id':f"eq.{lines[0]['redemption_id']}",'limit':1}, timeout=15) or []
        if not redemptions or to_int(redemptions[0]['distributor_id'],0) != g.client_customer_id:
            return None
        return lines[0], redemptions[0]

    @app.route('/api/client/architect-sales', methods=['GET','POST','OPTIONS'])
    def client_architect_sales():
        if request.method == 'OPTIONS':
            return '',204
        if request.method == 'GET':
            order_id = to_int(request.args.get('order_id'),0)
            if order_id <= 0:
                return jsonify(ok=False,error='Podaj numer zamówienia'),400
            redemptions = supabase_request('/rest/v1/architect_code_redemptions',params={
                'select':'id,order_id,distributor_id','order_id':f'eq.{order_id}',
                'distributor_id':f'eq.{g.client_customer_id}','limit':1},timeout=15) or []
            if not redemptions:
                return jsonify(ok=False,error='Brak kodu dla tego zamówienia'),404
            lines = supabase_request('/rest/v1/architect_sale_lines',params={
                'select':'id,order_item_id,product_id,qty,retail_gross_before,retail_gross_after,vat_rate',
                'redemption_id':f"eq.{redemptions[0]['id']}",'order':'id.asc'},timeout=15) or []
            line_ids = [to_int(line.get('id'), 0) for line in lines]
            if line_ids:
                ledger = supabase_request('/rest/v1/architect_commission_ledger', params={
                    'select':'sale_line_id,sold_qty',
                    'sale_line_id': 'in.(' + ','.join(map(str, line_ids)) + ')',
                    'entry_type':'eq.accrual', 'limit':1000,
                }, timeout=15) or []
                sold_by_line = {}
                for entry in ledger:
                    line_id = to_int(entry.get('sale_line_id'), 0)
                    sold_by_line[line_id] = sold_by_line.get(line_id, 0) + to_int(entry.get('sold_qty'), 0)
                for line in lines:
                    line['sold_qty'] = sold_by_line.get(to_int(line['id'], 0), 0)
            allocations = supabase_request('/rest/v1/invoice_allocations', params={
                'select':'invoice_id', 'order_id':f'eq.{order_id}', 'qty':'gt.0',
            }, timeout=15) or []
            invoice_ids = sorted({to_int(row.get('invoice_id'), 0) for row in allocations} - {0})
            invoices = []
            if invoice_ids:
                invoices = supabase_request('/rest/v1/invoices',params={
                    'select':'id,invoice_no','id':'in.(' + ','.join(map(str, invoice_ids)) + ')',
                    'publication_state':'eq.complete','order':'id.desc'},timeout=15) or []
            return jsonify(ok=True,lines=lines,invoices=invoices)
        if not _client_order_origin_allowed() or not _rate_limit('architect_sale',8,3600):
            return jsonify(ok=False,error='Nie można teraz potwierdzić sprzedaży'),429
        data=request.get_json(silent=True) or {}
        sale_line_id=to_int(data.get('sale_line_id'),0)
        invoice_id=to_int(data.get('invoice_id'),0)
        qty=to_int(data.get('sold_qty'),0)
        reference=norm(data.get('sale_reference'))
        sale_date=norm(data.get('sale_date'))
        if sale_line_id<=0 or invoice_id<=0 or qty<=0 or qty>10000 or len(reference)<3 or len(reference)>120:
            return jsonify(ok=False,error='Uzupełnij dane potwierdzenia sprzedaży'),400
        try:
            datetime.strptime(sale_date,'%Y-%m-%d')
            before=money(data.get('retail_gross_before'))
            after=money(data.get('retail_gross_after'))
        except (ValueError,TypeError,ArithmeticError):
            return jsonify(ok=False,error='Data lub kwoty sprzedaży są nieprawidłowe'),400
        if before<=0 or after<=0 or after>before:
            return jsonify(ok=False,error='Kwoty sprzedaży są nieprawidłowe'),400
        context=client_sale_context(sale_line_id)
        if not context:
            return jsonify(ok=False,error='Pozycja nie należy do Twojego sklepu'),403
        try:
            result=supabase_request('/rest/v1/rpc/architect_record_verified_sale',method='POST',payload={
                'p_sale_line_id':sale_line_id,'p_invoice_id':invoice_id,
                'p_distributor_id':g.client_customer_id,'p_sold_qty':qty,
                'p_sale_date':sale_date,'p_sale_reference':reference,
                'p_retail_gross_before':str(before),'p_retail_gross_after':str(after),
                'p_actor':g.client_user['id']},timeout=20)
        except urllib.error.HTTPError as exc:
            if exc.code not in (400, 409):
                raise
            try:
                detail = str(json.loads(exc.read().decode('utf-8')).get('message') or '')
            except (ValueError, UnicodeError, AttributeError):
                detail = ''
            if 'Sold units exceed invoiced units' in detail:
                error = 'Sprzedana ilość przekracza liczbę sztuk objętych fakturą.'
            elif 'duplicate key value' in detail:
                error = 'Ten dokument sprzedaży został już użyty.'
            elif 'Retail sale evidence required' in detail:
                error = 'Sprawdź datę i numer dokumentu sprzedaży.'
            elif 'Retail receipt totals differ' in detail:
                error = 'Kwoty nie zgadzają się z ceną zapisaną przy kodzie.'
            else:
                error = 'Nie można potwierdzić sprzedaży. Sprawdź fakturę, ilość i dokument.'
            return jsonify(ok=False,error=error),409
        return jsonify(ok=True,ledger_id=result),201

    @app.get("/api/admin/architects")
    def admin_architects_list():
        rows = supabase_request("/rest/v1/architect_profiles", params={
            "select": "id,first_name,last_name,studio_name,email,phone,instagram,website,status,created_at,reviewed_at",
            "order": "created_at.desc", "limit": 500,
        }, timeout=20) or []
        return jsonify(ok=True, architects=rows)

    @app.post("/api/admin/architects/<uuid:architect_id>/review")
    def admin_architect_review(architect_id):
        decision = norm((request.get_json(silent=True) or {}).get("status"))
        if decision not in {"approved", "rejected", "suspended"}:
            return jsonify(ok=False, error="Nieprawidłowa decyzja"), 400
        supabase_request('/rest/v1/rpc/architect_review_profile',method='POST',payload={
            'p_architect_id':str(architect_id),'p_status':decision,
            'p_actor':str(session.get('admin_username') or 'admin')},timeout=15)
        rows = supabase_request("/rest/v1/architect_profiles", params={
            "select":"id,status","id":f"eq.{architect_id}","limit":1,
        }, timeout=15) or []
        if not rows:
            return jsonify(ok=False, error="Nie znaleziono architekta"), 404
        return jsonify(ok=True, profile=rows[0])

    @app.get('/architects/export.csv')
    def architects_export_csv():
        kind=norm(request.args.get('kind'))
        definitions={
            'profiles':('architect_profiles',['id','first_name','last_name','studio_name','email','instagram','status','created_at']),
            'samples':('architect_sample_orders',['id','architect_id','status','tracking_no','created_at','shipped_at']),
            'codes':('architect_codes',['id','architect_id','distributor_id','code','status','created_at','expires_at']),
            'ledger':('architect_commission_ledger',['id','architect_id','sale_line_id','invoice_id','sale_date','entry_type','status','amount_net','reason','approved_at','settled_at','created_at']),
            'settlements':('architect_monthly_settlements',['id','architect_id','period_month','batch_no','status','total_net','created_at']),
        }
        if kind not in definitions: return 'Nieznany eksport',400
        table,fields=definitions[kind]
        rows=supabase_request(f'/rest/v1/{table}',params={
            'select':','.join(fields),'limit':5000},timeout=30) or []
        output=io.StringIO(newline='')
        writer=csv.writer(output,delimiter=';')
        writer.writerow(fields)
        for row in rows:
            values=['' if row.get(key) is None else str(row[key]) for key in fields]
            writer.writerow(["'"+value if value.startswith(('=','+','-','@')) else value
                             for value in values])
        payload=io.BytesIO(('\ufeff'+output.getvalue()).encode('utf-8'))
        return send_file(payload,mimetype='text/csv',as_attachment=True,
                         download_name=f'architekci-{kind}.csv',max_age=0)

    @app.get('/architects/<uuid:architect_id>')
    @require_permission('system.users_manage', operation='architect.program_admin')
    def architect_admin_profile(architect_id):
        profile_rows=supabase_request('/rest/v1/architect_profiles',params={
            'select':'*','id':f'eq.{architect_id}','limit':1},timeout=15) or []
        if not profile_rows: return 'Nie znaleziono architekta',404
        owner=profile_rows[0]
        welcome_jobs=supabase_request('/rest/v1/architect_order_email_outbox',params={
            'select':'id,status,provider_status,provider_id,created_at', 'architect_id':f'eq.{architect_id}',
            'notification_type':'in.(architect_approved,architect_welcome_resend)',
            'order':'created_at.desc','limit':1},timeout=15) or []
        codes=supabase_request('/rest/v1/architect_codes',params={
            'select':'id,code,distributor_id,status,created_at,expires_at',
            'architect_id':f'eq.{architect_id}','order':'created_at.desc','limit':1000},timeout=20) or []
        samples=supabase_request('/rest/v1/architect_sample_orders',params={
            'select':'id,status,shipping_address,tracking_no,carrier,created_at,shipped_at',
            'architect_id':f'eq.{architect_id}','order':'created_at.desc','limit':100},timeout=20) or []
        sample_items=[]
        if samples:
            sample_items=supabase_request('/rest/v1/architect_sample_items',params={
                'select':'sample_order_id,product_id,qty',
                'sample_order_id':'in.('+','.join(x['id'] for x in samples)+')',
                'limit':300},timeout=20) or []
        for row in samples:
            row['items']=[x for x in sample_items if x['sample_order_id']==row['id']]
        redemptions=[]
        if codes:
            redemptions=supabase_request('/rest/v1/architect_code_redemptions',params={
                'select':'id,code_id,order_id,distributor_id,created_at',
                'code_id':'in.('+','.join(x['id'] for x in codes)+')',
                'order':'created_at.desc','limit':1000},timeout=20) or []
        sale_lines=[]
        if redemptions:
            sale_lines=supabase_request('/rest/v1/architect_sale_lines',params={
                'select':'id,redemption_id,product_id,qty,wholesale_net_before,manufacturer_discount_net,wholesale_net_after,retail_gross_after,commission_estimate',
                'redemption_id':'in.('+','.join(x['id'] for x in redemptions)+')',
                'limit':3000},timeout=25) or []
        orders=[]
        if redemptions:
            orders=supabase_request('/rest/v1/orders',params={
                'select':'id,order_no,status,created_at',
                'id':'in.('+','.join(str(x['order_id']) for x in redemptions)+')',
                'limit':1000},timeout=20) or []
        ledger=supabase_request('/rest/v1/architect_commission_ledger',params={
            'select':'id,sale_line_id,invoice_id,sold_qty,sale_reference,sale_date,retail_net_after,amount_net,entry_type,status,reason,reversal_of,approved_at,settled_at,created_at',
            'architect_id':f'eq.{architect_id}','order':'created_at.desc','limit':3000},timeout=20) or []
        settlements=supabase_request('/rest/v1/architect_monthly_settlements',params={
            'select':'id,period_month,batch_no,status,total_net,created_at',
            'architect_id':f'eq.{architect_id}','order':'period_month.desc','limit':200},timeout=20) or []
        payouts=[]
        if settlements:
            payouts=supabase_request('/rest/v1/architect_payouts',params={
                'select':'settlement_id,amount_net,paid_at,reference',
                'settlement_id':'in.('+','.join(x['id'] for x in settlements)+')',
                'limit':200},timeout=20) or []
        audits=supabase_request('/rest/v1/architect_audit_events',params={
            'select':'actor_id,action,entity_type,entity_id,details,created_at',
            'entity_id':f'eq.{architect_id}','order':'created_at.desc','limit':200},timeout=20) or []
        code_by_id={x['id']:x for x in codes}
        order_by_id={to_int(x['id'],0):x for x in orders}
        payout_by_settlement={x['settlement_id']:x for x in payouts}
        sale_ledger={}
        for entry in ledger:
            if entry.get('entry_type') == 'accrual' and entry.get('sale_line_id'):
                sale_ledger.setdefault(to_int(entry['sale_line_id'],0),[]).append(entry)
        for row in redemptions:
            row['code']=code_by_id.get(row['code_id'],{}).get('code','')
            row['order']=order_by_id.get(to_int(row['order_id'],0),{})
            row['lines']=[x for x in sale_lines if x['redemption_id']==row['id']]
            for line in row['lines']:
                entries=sale_ledger.get(to_int(line['id'],0),[])
                line['sold_qty']=sum(to_int(e.get('sold_qty'),0) for e in entries)
                line['invoice_ids']=sorted({to_int(e.get('invoice_id'),0) for e in entries if e.get('invoice_id')})
        for row in settlements:
            row['payout']=payout_by_settlement.get(row['id'])
        private_notes=[]
        try:
            from architect_project_admin import private_code_notes
        except ImportError:
            pass  # Optional local projects package is not installed yet.
        else:
            private_notes=private_code_notes(supabase_request,architect_id,codes)
        return render_template('architect_profile.html',title='Profil architekta',
            profile=owner,samples=samples,codes=codes,redemptions=redemptions,
            ledger=ledger,settlements=settlements,audits=audits,
            welcome_request_id=str(uuid.uuid4()),private_code_notes=private_notes,
            welcome_delivery=delivery_state(welcome_jobs[0]) if welcome_jobs else None,
            welcome_job=welcome_jobs[0] if welcome_jobs else None)

    @app.get('/architects/<uuid:architect_id>/welcome-email-status')
    @require_permission('system.users_manage', operation='architect.program_admin')
    def architect_welcome_email_status(architect_id):
        try:
            row=read_delivery(supabase_request,architect_id,request.args.get('job'))
            if not row:
                return jsonify(ok=False,error='Nie znaleziono wysyłki'),404
            return jsonify(ok=True,job_id=row['id'],**delivery_state(row))
        except (ValueError,TypeError):
            return jsonify(ok=False,error='Nieprawidłowe żądanie'),400
        except Exception:
            return jsonify(ok=False,error='Nie udało się odczytać statusu. Odśwież kartę.'),503

    @app.route('/architects', methods=['GET', 'POST'])
    @require_permission('system.users_manage', operation='architect.program_admin')
    def architects_admin():
        actor = str(session.get('admin_username') or 'admin')
        if request.method == 'POST':
            action = norm(request.form.get('action'))
            try:
                if action == 'remove_architect':
                    architect_id = str(uuid.UUID(norm(request.form.get('architect_id'))))
                    if norm(request.form.get('confirmation')) != 'USUŃ':
                        raise ValueError('Wpisz USUŃ, aby potwierdzić zamknięcie konta')
                    reason = norm(request.form.get('reason'))
                    if len(reason) < 10:
                        raise ValueError('Podaj powód usunięcia konta (co najmniej 10 znaków)')
                    supabase_request('/rest/v1/rpc/architect_remove_from_program',method='POST',
                        payload={'p_architect_id':architect_id,'p_reason':reason,'p_actor':actor},timeout=20)
                elif action == 'cancel_sample_item':
                    sample_id = str(uuid.UUID(norm(request.form.get('sample_id'))))
                    product_id = to_int(request.form.get('product_id'),0)
                    if product_id <= 0: raise ValueError('Wybierz próbkę do anulowania')
                    supabase_request('/rest/v1/rpc/architect_cancel_sample_item',method='POST',
                        payload={'p_sample_id':sample_id,'p_product_id':product_id,'p_actor':actor},timeout=20)
                elif action == 'prepare_sample_shipping':
                    sample_id = str(uuid.UUID(norm(request.form.get('sample_id'))))
                    order_id = supabase_request('/rest/v1/rpc/architect_prepare_sample_shipping',method='POST',
                        payload={'p_sample_id':sample_id,'p_actor':actor},timeout=30)
                    if not order_id: raise ValueError('Nie potwierdzono przygotowania paczki. Odśwież listę przed ponowieniem.')
                    maybe_pull_shared_from_supabase(force=True)
                    return redirect(url_for('order_packing_list_download_admin',order_id=int(order_id)))
                elif action == 'review':
                    architect_id = str(uuid.UUID(norm(request.form.get('architect_id'))))
                    status = norm(request.form.get('status'))
                    if status not in {'approved','rejected','suspended'}:
                        raise ValueError('Nieprawidłowa decyzja')
                    supabase_request('/rest/v1/rpc/architect_review_profile',method='POST',
                        payload={'p_architect_id':architect_id,'p_status':status,
                                 'p_actor':actor},timeout=15)
                    if status == 'approved':
                        # The review RPC rejects unchanged status. Only a real
                        # transition reaches here; opening/saving cannot send.
                        try:
                            job=read_delivery(supabase_request,architect_id)
                            if job and job.get('status') in {'PENDING','RETRY'}:
                                request_attempt(supabase_request,job['id'])
                        except Exception:
                            pass  # Account remains active; durable queue retries.
                elif action == 'resend_welcome':
                    architect_id = str(uuid.UUID(norm(request.form.get('architect_id'))))
                    request_id = str(uuid.UUID(norm(request.form.get('request_id'))))
                    job_id=supabase_request('/rest/v1/rpc/architect_resend_welcome', method='POST',
                        payload={'p_architect_id':architect_id,'p_request_id':request_id,
                                 'p_actor':actor}, timeout=15)
                    started=request_attempt(supabase_request,job_id)
                    if request.accept_mimetypes.best == 'application/json':
                        return jsonify(ok=True,job_id=job_id,state='sending' if started else 'queued'),202
                    return redirect(url_for('architect_admin_profile',architect_id=architect_id,
                                            result='welcome_attempt'))
                elif action == 'sample_status':
                    sample_id = str(uuid.UUID(norm(request.form.get('sample_id'))))
                    status = norm(request.form.get('status'))
                    tracking = norm(request.form.get('tracking_no'))
                    if status not in {'preparing','shipped','delivered','cancelled'} or (status == 'shipped' and not tracking):
                        raise ValueError('Status lub numer przesyłki jest nieprawidłowy')
                    supabase_request('/rest/v1/rpc/architect_change_sample_status',method='POST',
                        payload={'p_sample_id':sample_id,'p_status':status,
                                 'p_tracking_no':tracking,'p_actor':actor},timeout=20)
                elif action == 'sample_repeat':
                    architect_id = str(uuid.UUID(norm(request.form.get('architect_id'))))
                    reason = norm(request.form.get('reason'))
                    if len(reason) < 10: raise ValueError('Podaj powód wyjątkowej kolejnej paczki')
                    supabase_request('/rest/v1/rpc/architect_grant_extra_samples',method='POST',
                        payload={'p_architect_id':architect_id,'p_reason':reason,
                                 'p_actor':actor},timeout=15)
                elif action == 'distributor_add':
                    customer_id = to_int(request.form.get('customer_id'),0)
                    rows = supabase_request('/rest/v1/customers',params={
                        'select':'id,name,address,phone,email,price_list',
                        'id':f'eq.{customer_id}','limit':1},timeout=15) or []
                    if not rows or norm(rows[0].get('price_list') or 'pln').lower() != 'pln':
                        raise ValueError('Wybierz istniejącego dystrybutora z cennikiem PLN')
                    customer = rows[0]
                    store_name = norm(customer.get('name'))
                    address = norm(customer.get('address'))
                    city_match = re.search(r'\b\d{2}-\d{3}\s+([^,\n]+)', address)
                    city = city_match.group(1).strip() if city_match else norm(request.form.get('city'))
                    if not store_name or not city: raise ValueError('Uzupełnij miasto w kartotece klienta lub w formularzu')
                    supabase_request('/rest/v1/rpc/architect_set_distributor',method='POST',payload={
                        'p_customer_id':customer_id,'p_active':True,
                        'p_store_name':store_name,'p_city':city,
                        'p_store_address':address,
                        'p_contact':norm(customer.get('phone') or customer.get('email')),
                        'p_actor':actor},timeout=15)
                elif action == 'distributor_disable':
                    customer_id = to_int(request.form.get('customer_id'),0)
                    supabase_request('/rest/v1/rpc/architect_set_distributor',method='POST',payload={
                        'p_customer_id':customer_id,'p_active':False,
                        'p_store_name':'','p_city':'','p_store_address':'','p_contact':'',
                        'p_actor':actor},timeout=15)
                elif action == 'verified_sale':
                    sale_line_id = to_int(request.form.get('sale_line_id'),0)
                    invoice_id = to_int(request.form.get('invoice_id'),0)
                    sold_qty = to_int(request.form.get('sold_qty'),0)
                    reference = norm(request.form.get('sale_reference'))
                    lines = supabase_request('/rest/v1/architect_sale_lines',params={
                        'select':'redemption_id','id':f'eq.{sale_line_id}','limit':1},timeout=15) or []
                    redemptions = supabase_request('/rest/v1/architect_code_redemptions',params={
                        'select':'distributor_id','id':f"eq.{lines[0]['redemption_id']}",'limit':1},timeout=15) or [] if lines else []
                    if not redemptions: raise ValueError('Nie znaleziono pozycji programu')
                    supabase_request('/rest/v1/rpc/architect_record_verified_sale',method='POST',
                        payload={'p_sale_line_id':sale_line_id,'p_invoice_id':invoice_id,
                                 'p_distributor_id':redemptions[0]['distributor_id'],
                                 'p_sold_qty':sold_qty,'p_sale_date':norm(request.form.get('sale_date')),
                                 'p_sale_reference':reference,
                                 'p_retail_gross_before':norm(request.form.get('retail_gross_before')),
                                 'p_retail_gross_after':norm(request.form.get('retail_gross_after')),
                                 'p_actor':actor},timeout=20)
                elif action == 'approve_commission':
                    ledger_id = str(uuid.UUID(norm(request.form.get('ledger_id'))))
                    supabase_request('/rest/v1/rpc/architect_approve_commission',method='POST',
                        payload={'p_ledger_id':ledger_id,'p_actor':actor},timeout=20)
                elif action == 'record_return':
                    accrual_id = str(uuid.UUID(norm(request.form.get('accrual_id'))))
                    supabase_request('/rest/v1/rpc/architect_record_return',method='POST',
                        payload={'p_accrual_id':accrual_id,
                                 'p_return_qty':to_int(request.form.get('return_qty'),0),
                                 'p_return_date':norm(request.form.get('return_date')),
                                 'p_reference':norm(request.form.get('return_reference')),
                                 'p_reason':norm(request.form.get('reason')),
                                 'p_actor':actor},timeout=20)
                elif action == 'create_settlement':
                    architect_id = str(uuid.UUID(norm(request.form.get('architect_id'))))
                    supabase_request('/rest/v1/rpc/architect_create_monthly_settlement',method='POST',
                        payload={'p_architect_id':architect_id,
                                 'p_period_month':norm(request.form.get('period_month'))+'-01',
                                 'p_actor':actor},timeout=20)
                elif action == 'approve_settlement':
                    settlement_id = str(uuid.UUID(norm(request.form.get('settlement_id'))))
                    supabase_request('/rest/v1/rpc/architect_approve_monthly_settlement',method='POST',
                        payload={'p_settlement_id':settlement_id,'p_actor':actor},timeout=20)
                elif action == 'record_payout':
                    settlement_id = str(uuid.UUID(norm(request.form.get('settlement_id'))))
                    supabase_request('/rest/v1/rpc/architect_record_payout',method='POST',
                        payload={'p_settlement_id':settlement_id,
                                 'p_reference':norm(request.form.get('payment_reference')),
                                 'p_paid_at':payout_timestamp(norm(request.form.get('paid_at')), app_now()),
                                 'p_actor':actor},timeout=20)
                elif action == 'product_details':
                    product_id = to_int(request.form.get('product_id'),0)
                    raw_technical = norm(request.form.get('technical_data')) or '{}'
                    technical = json.loads(raw_technical)
                    if not isinstance(technical,dict): raise ValueError('Dane techniczne muszą być obiektem JSON')
                    override = norm(request.form.get('retail_gross_override'))
                    supabase_request('/rest/v1/rpc/architect_upsert_product_details',method='POST',
                        payload={'p_product_id':product_id,
                                 'p_description':norm(request.form.get('description')),
                                 'p_material':norm(request.form.get('material')) or 'Lity mosiądz',
                                 'p_mounting_spacing':norm(request.form.get('mounting_spacing')),
                                 'p_dimensions':norm(request.form.get('dimensions')),
                                 'p_finish':norm(request.form.get('finish')),
                                 'p_technical_data':technical,
                                 'p_model_3d_url':norm(request.form.get('model_3d_url')),
                                 'p_vat_rate':norm(request.form.get('vat_rate')) or '0.23',
                                 'p_retail_gross_override':str(money(override)) if override else None,
                                 'p_actor':actor},timeout=20)
                else:
                    raise ValueError('Nieznana operacja')
                return_view = norm(request.form.get('return_view'))
                if return_view not in {'distributors', 'catalog', 'settings'}:
                    return_view = 'settings'
                return redirect(url_for('architects_admin', view=return_view, result='saved'))
            except Exception as exc:
                app.logger.exception('Architect admin action failed: %s', action)
                if action == 'resend_welcome' and request.accept_mimetypes.best == 'application/json':
                    return jsonify(ok=False,error='Nie udało się rozpocząć wysyłki. Odśwież status lub spróbuj później.'),400
                message = str(exc) if isinstance(exc, ValueError) else 'Nie udało się zapisać zmiany. Odśwież widok i sprawdź aktualny stan przed ponowieniem.'
                return render_template('architect_admin.html',title='Architekci',error=message,
                    architects=[],samples=[],distributors=[],sale_lines=[],ledger=[],settlements=[],products=[],product_details=[]),400
        architects = supabase_request('/rest/v1/architect_profiles',params={
            'select':'id,first_name,last_name,studio_name,email,instagram,shipping_address,status,sample_entitlements,created_at,removed_at',
            'order':'created_at.desc','limit':5000},timeout=20) or []
        samples = supabase_request('/rest/v1/architect_sample_orders',params={
            'select':'id,architect_id,shipping_address,status,tracking_no,created_at,fulfillment_order_id',
            'order':'created_at.desc','limit':200},timeout=20) or []
        if samples:
            ids=','.join(x['id'] for x in samples)
            sample_items=supabase_request('/rest/v1/architect_sample_items',params={
                'select':'sample_order_id,product_id,qty,cancelled_at',
                'sample_order_id':f'in.({ids})','limit':600},timeout=20) or []
            for row in samples:
                row['items']=[x for x in sample_items if x['sample_order_id']==row['id']]
        distributors = supabase_request('/rest/v1/distributor_program_memberships',params={
            'select':'customer_id,store_name,city,active','order':'store_name.asc','limit':500},timeout=20) or []
        customers = supabase_request('/rest/v1/customers',params={
            'select':'id,name,address,phone,email,price_list','order':'name.asc','limit':5000},timeout=20) or []
        customer_by_id = {to_int(c.get('id'),0): c for c in customers}
        for distributor in distributors:
            customer = customer_by_id.get(to_int(distributor.get('customer_id'),0), {})
            distributor['store_name'] = customer.get('name') or distributor.get('store_name')
            distributor['address'] = customer.get('address') or ''
            distributor['contact'] = customer.get('phone') or customer.get('email') or ''
        sale_lines = supabase_request('/rest/v1/architect_sale_lines',params={
            'select':'id,order_item_id,product_id,qty,retail_gross_after,commission_estimate',
            'order':'id.desc','limit':200},timeout=20) or []
        ledger = supabase_request('/rest/v1/architect_commission_ledger',params={
            'select':'id,architect_id,sale_line_id,retail_net_after,amount_net,status,entry_type,sold_qty,sale_date,created_at',
            'order':'created_at.desc','limit':5000},timeout=20) or []
        settlements = supabase_request('/rest/v1/architect_monthly_settlements',params={
            'select':'id,architect_id,period_month,batch_no,total_net,status',
            'order':'period_month.desc','limit':200},timeout=20) or []
        products = supabase_request('/rest/v1/client_stock_catalog_v',params={
            'select':'product_id,sku,model,name','order':'sku.asc','limit':5000},timeout=30) or []
        sku_by_id={to_int(x['product_id'],0):x.get('sku','') for x in products}
        for row in samples:
            for item in row.get('items',[]):
                item['sku']=sku_by_id.get(to_int(item['product_id'],0),'')
        product_details = supabase_request('/rest/v1/architect_product_details',params={
            'select':'product_id,material,mounting_spacing,dimensions,finish,vat_rate,retail_gross_override,updated_at',
            'order':'product_id.asc','limit':5000},timeout=20) or []
        stats_codes = supabase_request('/rest/v1/architect_codes',params={
            'select':'id,architect_id,distributor_id,code,status,created_at,expires_at','limit':5000},timeout=20) or []
        stats_redemptions = supabase_request('/rest/v1/architect_code_redemptions',params={
            'select':'id,distributor_id','limit':5000},timeout=20) or []
        stats_samples = supabase_request('/rest/v1/architect_sample_orders',params={
            'select':'architect_id,status','limit':5000},timeout=20) or []
        stats_lines = supabase_request('/rest/v1/architect_sale_lines',params={
            'select':'id,redemption_id','limit':5000},timeout=20) or []
        metrics = {x['id']:{'shipped':0,'codes':0,'redeemed':0,'sales':Decimal('0'),
                            'earnings':Decimal('0'),'referrals':set()} for x in architects}
        for row in stats_codes:
            if row['architect_id'] in metrics:
                metrics[row['architect_id']]['codes'] += 1
                if row['status'] == 'redeemed':
                    metrics[row['architect_id']]['redeemed'] += 1
        for row in stats_samples:
            if row['architect_id'] in metrics and row['status'] in ('shipped','delivered'):
                metrics[row['architect_id']]['shipped'] += 1
        line_redemption = {to_int(x['id'],0):x['redemption_id'] for x in stats_lines}
        for row in ledger:
            owner_id = row.get('architect_id')
            if owner_id not in metrics: continue
            metrics[owner_id]['earnings'] += Decimal(str(row.get('amount_net') or 0))
            if row.get('entry_type') == 'accrual':
                metrics[owner_id]['sales'] += Decimal(str(row.get('retail_net_after') or 0))
                redemption_id = line_redemption.get(to_int(row.get('sale_line_id'),0))
                if redemption_id: metrics[owner_id]['referrals'].add(redemption_id)
        for row in architects:
            row['stats'] = metrics[row['id']]
            row['stats']['referrals'] = len(row['stats']['referrals'])
            address = norm(row.get('shipping_address'))
            match = re.search(r'\b\d{2}-\d{3}\s+([^,\n]+)', address)
            row['city'] = match.group(1).strip() if match else ''
        architect_by_id = {row['id']: row for row in architects}
        for distributor in distributors:
            customer_id = to_int(distributor.get('customer_id'), 0)
            related_codes = [code for code in stats_codes if to_int(code.get('distributor_id'), 0) == customer_id]
            distributor['architects'] = sorted({
                ' '.join(filter(None, [architect_by_id[code['architect_id']].get('first_name'),
                                        architect_by_id[code['architect_id']].get('last_name')]))
                for code in related_codes if code.get('architect_id') in architect_by_id})
            distributor['codes_count'] = len(related_codes)
            distributor['redemptions_count'] = sum(
                to_int(redemption.get('distributor_id'), 0) == customer_id
                for redemption in stats_redemptions)
        architect_owners = {row['id']: row for row in architects}
        if request.args.get('show_removed') == '1':
            architects = [row for row in architects if row.get('removed_at')]
        else:
            architects = [row for row in architects if not row.get('removed_at')]
        total_architects = len(architects)
        total_codes = len(stats_codes)
        redeemed_codes = sum(code.get('status') == 'redeemed' for code in stats_codes)
        top_architects = sorted(
            (row for row in architects if row['stats']['referrals'] > 0),
            key=lambda row: (row['stats']['sales'],row['stats']['referrals']),reverse=True)[:3]
        pending_architects = [row for row in architects if row.get('status') == 'pending_verification']
        search = norm(request.args.get('q')).casefold()
        status_filter = norm(request.args.get('status'))
        city_filter = norm(request.args.get('city'))
        cities = sorted({row['city'] for row in architects if row['city']})
        if search:
            architects = [row for row in architects if search in ' '.join(str(row.get(k) or '')
                for k in ('studio_name','first_name','last_name','email','instagram')).casefold()]
        if status_filter:
            architects = [row for row in architects if row.get('status') == status_filter]
        if city_filter:
            architects = [row for row in architects if row['city'] == city_filter]
        sort = norm(request.args.get('sort'))
        if sort not in {'name','sales','orders','earnings','newest'}: sort = 'newest'
        if sort == 'name': architects.sort(key=lambda row: (row['studio_name'] or '').casefold())
        elif sort == 'sales': architects.sort(key=lambda row: row['stats']['sales'],reverse=True)
        elif sort == 'orders': architects.sort(key=lambda row: row['stats']['referrals'],reverse=True)
        elif sort == 'earnings': architects.sort(key=lambda row: row['stats']['earnings'],reverse=True)
        else: architects.sort(key=lambda row: row.get('created_at') or '',reverse=True)
        filtered_count = len(architects)
        filtered_architects = architects
        page_size = 20
        page_count = max(1,(filtered_count + page_size - 1)//page_size)
        page = min(max(to_int(request.args.get('page'),1),1),page_count)
        page_architects = architects[(page-1)*page_size:page*page_size]
        view = norm(request.args.get('view'))
        program_invoices = supabase_request('/rest/v1/invoices',params={
            'select':'id,invoice_no','order':'id.desc','limit':200},timeout=20) or [] if view == 'settings' else []
        code_status = norm(request.args.get('code_status'))
        code_rows = [row for row in stats_codes if not code_status or row.get('status') == code_status]
        template = {'distributors':'architect_distributors.html',
                    'catalog':'architect_catalog_admin.html',
                    'settings':'architect_admin.html',
                    'codes':'architect_admin_codes.html'}.get(view,'architect_admin_overview.html')
        def pln(value):
            return f"{Decimal(str(value or 0)):,.2f}".replace(',',' ').replace('.',',') + ' zł'
        return render_template(template,title='Program Architektów',error='',
            architects=page_architects if template == 'architect_admin_overview.html' else filtered_architects,
            samples=samples,distributors=distributors,customers=customers,
            sale_lines=sale_lines,ledger=ledger,settlements=settlements,
            products=products,product_details=product_details,
            search=search,status_filter=status_filter,city_filter=city_filter,cities=cities,
            sort=sort,page=page,page_count=page_count,filtered_count=filtered_count,
            total_architects=total_architects,total_codes=total_codes,
            redeemed_codes=redeemed_codes,top_architects=top_architects,pln=pln,
            pending_architects=pending_architects,architect_owners=architect_owners,
            code_rows=code_rows,code_status=code_status,
            program_invoices=program_invoices,
            new_samples_count=sum(1 for s in stats_samples if s.get('status') == 'requested'))

    return {}
