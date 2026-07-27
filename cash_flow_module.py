# -*- coding: utf-8 -*-
import json
from datetime import datetime, timedelta

from flask import request, redirect, url_for
from flask import render_template_string


CASH_FLOW_SETTING_KEYS = {
    "account_balance": "0",
    "monthly_zus": "0",
    "cash_buffer": "0",
    "planned_china_budget": "0",
    "growth_percent": "0",
}


def parse_date_safe(value):
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y"):
        try:
            return datetime.strptime(value[:19] if "%H" in fmt else value[:10], fmt).date()
        except Exception:
            pass
    return None


def ensure_cash_flow_tables(conn, now_iso):
    c = conn()
    cur = c.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS cash_flow_settings(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL
    )
    """)
    ts = now_iso()
    for key, value in CASH_FLOW_SETTING_KEYS.items():
        cur.execute("""
          INSERT OR IGNORE INTO cash_flow_settings(key, value, updated_at)
          VALUES(?,?,?)
        """, (key, value, ts))
    c.commit()
    c.close()


def register_cash_flow(app, deps):
    conn = deps["conn"]
    now_iso = deps["now_iso"]
    app_now = deps["app_now"]
    to_float = deps["to_float"]
    maybe_pull_shared_from_supabase = deps["maybe_pull_shared_from_supabase"]
    base_url = deps["BASE_URL"]
    db_path = deps["DB_PATH"]

    ensure_cash_flow_tables(conn, now_iso)

    def cash_flow_settings_load():
        data = dict(CASH_FLOW_SETTING_KEYS)
        c = conn()
        cur = c.cursor()
        try:
            cur.execute("SELECT key, value FROM cash_flow_settings")
            for row in cur.fetchall():
                if row["key"] in data:
                    data[row["key"]] = row["value"]
        except Exception:
            pass
        c.close()
        return data

    def cash_flow_settings_save(form):
        c = conn()
        cur = c.cursor()
        ts = now_iso()
        for key in CASH_FLOW_SETTING_KEYS:
            val = str(to_float(form.get(key), 0.0))
            cur.execute("""
              INSERT INTO cash_flow_settings(key, value, updated_at)
              VALUES(?,?,?)
              ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """, (key, val, ts))
        c.commit()
        c.close()

    @app.route("/cash-flow", methods=["GET", "POST"])
    def cash_flow():
        maybe_pull_shared_from_supabase()
        if request.method == "POST":
            cash_flow_settings_save(request.form)
            return redirect(url_for("cash_flow", saved=1))

        today = app_now().date()
        settings = cash_flow_settings_load()
        account_balance = to_float(settings.get("account_balance"), 0)
        monthly_zus = to_float(settings.get("monthly_zus"), 0)
        cash_buffer = to_float(settings.get("cash_buffer"), 0)
        planned_china_budget = to_float(settings.get("planned_china_budget"), 0)
        growth_percent = to_float(settings.get("growth_percent"), 0)
        growth_factor = max(0, 1 + (growth_percent / 100.0))

        c = conn()
        cur = c.cursor()

        cur.execute("""
          SELECT i.*,
                 COALESCE(m.paid,0) AS paid,
                 m.paid_at,
                 COALESCE(m.payment_reminder,0) AS payment_reminder,
                 m.invoice_items_json
          FROM invoices i
          LEFT JOIN invoice_meta m ON m.invoice_id=i.id
          ORDER BY COALESCE(i.payment_to, i.issue_date) ASC, i.id DESC
        """)
        invoices_rows = cur.fetchall()

        unpaid_total = overdue_total = due_7_total = due_30_total = 0.0
        month_vat = month_net = month_profit = 0.0
        last_30_net = last_30_profit = 0.0
        sold_30_qty = 0
        overdue_clients_map = {}
        paid_clients_map = {}
        inflow_rows = []

        for inv in invoices_rows:
            gross = to_float(inv["total_gross"], 0)
            net = to_float(inv["total_net"], 0)
            vat = max(0.0, gross - net)
            paid = int(inv["paid"] or 0) == 1
            issue_d = parse_date_safe(inv["issue_date"])
            due_d = parse_date_safe(inv["payment_to"]) or issue_d or today
            buyer = inv["buyer_name"] or "-"
            invoice_no = inv["invoice_no"] or "-"

            if issue_d and issue_d.year == today.year and issue_d.month == today.month:
                month_net += net
                month_vat += vat
                month_profit += net * 0.60

            if issue_d and issue_d >= today - timedelta(days=30):
                last_30_net += net
                last_30_profit += net * 0.60
                try:
                    for item in json.loads(inv["invoice_items_json"] or "[]"):
                        sold_30_qty += int(item.get("qty") or item.get("invoice_qty") or item.get("current_invoice_qty") or 0)
                except Exception:
                    pass

            if paid:
                paid_d = parse_date_safe(inv["paid_at"]) or issue_d
                if paid_d and paid_d >= today - timedelta(days=30):
                    rec = paid_clients_map.setdefault(buyer, {"buyer": buyer, "gross": 0.0, "count": 0, "last": ""})
                    rec["gross"] += gross
                    rec["count"] += 1
                    rec["last"] = max(rec["last"], str(paid_d))
                continue

            unpaid_total += gross
            if due_d <= today + timedelta(days=7):
                due_7_total += gross
            if due_d <= today + timedelta(days=30):
                due_30_total += gross
            if due_d < today:
                overdue_total += gross
                days_late = (today - due_d).days
                rec = overdue_clients_map.setdefault(buyer, {"buyer": buyer, "gross": 0.0, "count": 0, "days_late": 0})
                rec["gross"] += gross
                rec["count"] += 1
                rec["days_late"] = max(rec["days_late"], days_late)

            inflow_rows.append({
                "invoice_no": invoice_no,
                "buyer": buyer,
                "due": due_d.isoformat() if due_d else "-",
                "gross": gross,
                "days": (due_d - today).days if due_d else 0,
                "overdue": due_d < today if due_d else False,
                "reminder": int(inv["payment_reminder"] or 0) == 1,
            })

        cur.execute("""
          SELECT COALESCE(SUM(s.qty),0) AS units,
                 COALESCE(SUM(s.qty * COALESCE(pr.net_price,0)),0) AS sale_net,
                 COALESCE(SUM(s.qty * COALESCE(pr.net_price,0) / 2.5),0) AS cost_est
          FROM stock s
          LEFT JOIN products p ON p.id=s.product_id
          LEFT JOIN pricing pr ON lower(pr.model)=lower(COALESCE(p.sku,p.model))
        """)
        stock_row = cur.fetchone()
        stock_units = int(stock_row["units"] or 0)
        stock_sale_net = to_float(stock_row["sale_net"], 0)
        stock_cost_est = to_float(stock_row["cost_est"], 0)

        cur.execute("""
          SELECT COALESCE(SUM(ci.qty),0) AS qty,
                 COALESCE(SUM(ci.qty * COALESCE(pr.net_price,0) / 2.5),0) AS cost_est
          FROM china_items ci
          JOIN china_packages cp ON cp.id=ci.package_id
          LEFT JOIN products p ON p.id=ci.product_id
          LEFT JOIN pricing pr ON lower(pr.model)=lower(COALESCE(p.sku, ci.sku))
          WHERE lower(COALESCE(cp.status,'')) IN ('planned','ordered','shipped')
        """)
        china_row = cur.fetchone()
        china_qty = int(china_row["qty"] or 0)
        china_cost_est = to_float(china_row["cost_est"], 0)

        cutoff_30 = (today - timedelta(days=30)).isoformat()
        cutoff_90 = (today - timedelta(days=90)).isoformat()
        cur.execute("""
          WITH demand AS (
            SELECT
              oi.product_id,
              SUM(CASE WHEN date(o.created_at) >= date(?) THEN oi.qty ELSE 0 END) AS ordered_30,
              SUM(CASE WHEN date(o.created_at) >= date(?) THEN oi.qty ELSE 0 END) AS ordered_90
            FROM order_items oi
            JOIN orders o ON o.id=oi.order_id
            WHERE lower(COALESCE(o.status,'')) NOT IN ('cancelled','canceled','deleted','usuniete','anulowane')
            GROUP BY oi.product_id
          ),
          china_incoming AS (
            SELECT
              ci.product_id,
              SUM(ci.qty) AS incoming_qty
            FROM china_items ci
            JOIN china_packages cp ON cp.id=ci.package_id
            WHERE lower(COALESCE(cp.status,'')) IN ('planned','ordered','shipped')
            GROUP BY ci.product_id
          )
          SELECT
            p.sku,
            p.model,
            p.name,
            COALESCE(d.ordered_30,0) AS ordered_30,
            COALESCE(d.ordered_90,0) AS ordered_90,
            COALESCE(s.qty,0) AS stock_qty,
            COALESCE(ci.incoming_qty,0) AS incoming_qty,
            MAX(0, COALESCE(d.ordered_90,0) - COALESCE(s.qty,0) - COALESCE(ci.incoming_qty,0)) AS suggested_qty,
            CASE
              WHEN COALESCE(d.ordered_90,0) <= 0 THEN 0
              ELSE ROUND((COALESCE(s.qty,0) + COALESCE(ci.incoming_qty,0)) * 100.0 / COALESCE(d.ordered_90,0), 0)
            END AS coverage_pct
          FROM demand d
          JOIN products p ON p.id=d.product_id
          LEFT JOIN stock s ON s.product_id=p.id
          LEFT JOIN china_incoming ci ON ci.product_id=p.id
          WHERE COALESCE(d.ordered_90,0) > 0
            AND (
              COALESCE(s.qty,0) + COALESCE(ci.incoming_qty,0) < COALESCE(d.ordered_90,0)
              OR COALESCE(s.qty,0) <= COALESCE(d.ordered_30,0)
            )
          ORDER BY
            COALESCE(d.ordered_90,0) DESC,
            suggested_qty DESC,
            coverage_pct ASC
          LIMIT 10
        """, (cutoff_30, cutoff_90))
        reorder_rows = cur.fetchall()
        c.close()

        avg_daily_gross = (last_30_net * 1.23) / 30.0 if last_30_net else 0.0
        forecast_7_sales = avg_daily_gross * 7 * growth_factor
        forecast_30_sales = avg_daily_gross * 30 * growth_factor
        forecast_7_total = due_7_total + forecast_7_sales
        forecast_30_total = due_30_total + forecast_30_sales
        safe_to_spend = account_balance + due_7_total - month_vat - monthly_zus - cash_buffer - planned_china_budget

        overdue_clients = sorted(overdue_clients_map.values(), key=lambda r: (r["days_late"], r["gross"]), reverse=True)[:10]
        paid_clients = sorted(paid_clients_map.values(), key=lambda r: r["gross"], reverse=True)[:10]
        inflow_rows = sorted(inflow_rows, key=lambda r: (r["overdue"], r["due"]), reverse=True)[:25]

        kpis = {
            "account_balance": account_balance,
            "unpaid_total": unpaid_total,
            "overdue_total": overdue_total,
            "due_7_total": due_7_total,
            "due_30_total": due_30_total,
            "month_vat": month_vat,
            "monthly_zus": monthly_zus,
            "china_cost_est": china_cost_est,
            "china_qty": china_qty,
            "stock_units": stock_units,
            "stock_sale_net": stock_sale_net,
            "stock_cost_est": stock_cost_est,
            "stock_profit_est": stock_sale_net * 0.60,
            "last_30_net": last_30_net,
            "last_30_profit": last_30_profit,
            "sold_30_qty": sold_30_qty,
            "forecast_7_total": forecast_7_total,
            "forecast_30_total": forecast_30_total,
            "forecast_7_sales": forecast_7_sales,
            "forecast_30_sales": forecast_30_sales,
            "safe_to_spend": safe_to_spend,
        }

        tpl = r"""
        {% extends "base.html" %}
        {% block content %}
          <div class="card">
            <div class="flex">
              <h1 style="margin:0;">Cash flow</h1>
              <span class="badge">panel wewnętrzny</span>
              {% if request.args.get('saved') %}<span class="badge">Zapisano</span>{% endif %}
            </div>
            <div class="notice" style="margin-top:10px;">
              Prognoza jest pomocnicza: opiera się na fakturach, terminach płatności, opłaconych/zaległych klientach oraz szacunku marży.
              Zakładamy sprzedaż = zakup × 2,5, więc zysk orientacyjny = netto × 0,60.
            </div>
          </div>

          <div class="card">
            <h2>Ustawienia płynności</h2>
            <form method="post" class="grid3">
              <div><label class="muted small">Stan konta</label><input name="account_balance" value="{{ settings.account_balance }}"></div>
              <div><label class="muted small">ZUS / stałe koszty miesięczne</label><input name="monthly_zus" value="{{ settings.monthly_zus }}"></div>
              <div><label class="muted small">Bufor bezpieczeństwa</label><input name="cash_buffer" value="{{ settings.cash_buffer }}"></div>
              <div><label class="muted small">Zarezerwowane na Chiny</label><input name="planned_china_budget" value="{{ settings.planned_china_budget }}"></div>
              <div><label class="muted small">Korekta wzrostu prognozy %</label><input name="growth_percent" value="{{ settings.growth_percent }}"></div>
              <div class="flex" style="align-items:flex-end;"><button class="btn primary" type="submit">Zapisz cash flow</button></div>
            </form>
          </div>

          <div class="grid3">
            <div class="card"><h2>Stan konta</h2><div style="font-size:26px;font-weight:800;">{{ "%.2f"|format(k.account_balance) }} PLN</div></div>
            <div class="card"><h2>Do wpływu</h2><div style="font-size:26px;font-weight:800;">{{ "%.2f"|format(k.unpaid_total) }} PLN</div><div class="muted">z niezapłaconych faktur</div></div>
            <div class="card"><h2>Zaległości</h2><div style="font-size:26px;font-weight:800;color:{% if k.overdue_total>0 %}#b00020{% else %}#067a2d{% endif %};">{{ "%.2f"|format(k.overdue_total) }} PLN</div></div>

            <div class="card"><h2>VAT do zapłaty</h2><div style="font-size:24px;font-weight:800;">{{ "%.2f"|format(k.month_vat) }} PLN</div><div class="muted">orientacyjnie z faktur z bieżącego miesiąca</div></div>
            <div class="card"><h2>ZUS / koszty stałe</h2><div style="font-size:24px;font-weight:800;">{{ "%.2f"|format(k.monthly_zus) }} PLN</div></div>
            <div class="card"><h2>Chiny w drodze</h2><div style="font-size:24px;font-weight:800;">{{ "%.2f"|format(k.china_cost_est) }} PLN</div><div class="muted">{{ k.china_qty }} szt. wg cen zakupu szac. netto/2,5</div></div>

            <div class="card"><h2>Prognoza 7 dni</h2><div style="font-size:24px;font-weight:800;">{{ "%.2f"|format(k.forecast_7_total) }} PLN</div><div class="muted">terminy płatności + sprzedaż wg ostatnich 30 dni</div></div>
            <div class="card"><h2>Prognoza 30 dni</h2><div style="font-size:24px;font-weight:800;">{{ "%.2f"|format(k.forecast_30_total) }} PLN</div><div class="muted">uwzględnia korektę wzrostu</div></div>
            <div class="card"><h2>Możesz dziś wydać na Chiny</h2><div style="font-size:24px;font-weight:800;color:{% if k.safe_to_spend<0 %}#b00020{% else %}#067a2d{% endif %};">{{ "%.2f"|format(k.safe_to_spend) }} PLN</div><div class="muted">po VAT, ZUS, buforze i rezerwie</div></div>
          </div>

          <div class="card">
            <h2>Magazyn i marża</h2>
            <div class="flex">
              <span class="badge">Stan: {{ k.stock_units }} szt.</span>
              <span class="badge">Wartość sprzedaży netto: {{ "%.2f"|format(k.stock_sale_net) }} PLN</span>
              <span class="badge">Szac. koszt zakupu: {{ "%.2f"|format(k.stock_cost_est) }} PLN</span>
              <span class="badge">Szac. zysk w magazynie: {{ "%.2f"|format(k.stock_profit_est) }} PLN</span>
              <span class="badge">Sprzedane 30 dni: {{ k.sold_30_qty }} szt.</span>
            </div>
          </div>

          <div class="grid2">
            <div class="card">
              <h2>Najbliższe wpływy</h2>
              <table>
                <thead><tr><th>Faktura</th><th>Klient</th><th>Termin</th><th>Brutto</th><th>Status</th></tr></thead>
                <tbody>
                  {% for r in inflow_rows %}
                    <tr>
                      <td><b>{{ r.invoice_no }}</b></td><td>{{ r.buyer }}</td><td>{{ r.due }}</td>
                      <td>{{ "%.2f"|format(r.gross) }}</td>
                      <td>{% if r.overdue %}<span class="badge" style="color:#b00020;">zaległa {{ -r.days }} dni</span>{% elif r.days <= 7 %}<span class="badge">do 7 dni</span>{% else %}<span class="badge">oczekuje</span>{% endif %}</td>
                    </tr>
                  {% endfor %}
                  {% if not inflow_rows %}<tr><td colspan="5" class="muted">Brak niezapłaconych faktur.</td></tr>{% endif %}
                </tbody>
              </table>
            </div>

            <div class="card">
              <h2>Popularne SKU z niskim zapasem</h2>
              <p class="muted">Ranking pokazuje uchwyty najczęściej zamawiane w ostatnich 90 dniach, przy których stan magazynu jest niski. Towar w drodze z Chin zmniejsza sugerowaną ilość do domówienia.</p>
              <table>
                <thead><tr><th>SKU</th><th>Nazwa</th><th>30 dni</th><th>90 dni</th><th>Stan</th><th>W drodze</th><th>Sugeruj</th></tr></thead>
                <tbody>
                  {% for r in reorder_rows %}
                    <tr>
                      <td><b>{{ r.sku }}</b></td>
                      <td>{{ r.name or r.model or '-' }}</td>
                      <td>{{ r.ordered_30 }}</td>
                      <td>{{ r.ordered_90 }}</td>
                      <td>{{ r.stock_qty }}</td>
                      <td>{{ r.incoming_qty }}</td>
                      <td><b>{{ r.suggested_qty }}</b></td>
                    </tr>
                  {% endfor %}
                  {% if not reorder_rows %}<tr><td colspan="7" class="muted">Brak popularnych SKU z niskim zapasem według ostatnich zamówień.</td></tr>{% endif %}
                </tbody>
              </table>
            </div>
          </div>

          <div class="grid2">
            <div class="card">
              <h2>Klienci zalegający</h2>
              <table>
                <thead><tr><th>Klient</th><th>Faktur</th><th>Zalega</th><th>Najdłużej</th></tr></thead>
                <tbody>
                  {% for r in overdue_clients %}
                    <tr><td><b>{{ r.buyer }}</b></td><td>{{ r.count }}</td><td>{{ "%.2f"|format(r.gross) }} PLN</td><td>{{ r.days_late }} dni</td></tr>
                  {% endfor %}
                  {% if not overdue_clients %}<tr><td colspan="4" class="muted">Brak zaległych płatności.</td></tr>{% endif %}
                </tbody>
              </table>
            </div>

            <div class="card">
              <h2>Klienci, którzy zapłacili</h2>
              <table>
                <thead><tr><th>Klient</th><th>Faktur</th><th>Wpłynęło</th><th>Ostatnio</th></tr></thead>
                <tbody>
                  {% for r in paid_clients %}
                    <tr><td><b>{{ r.buyer }}</b></td><td>{{ r.count }}</td><td>{{ "%.2f"|format(r.gross) }} PLN</td><td>{{ r.last }}</td></tr>
                  {% endfor %}
                  {% if not paid_clients %}<tr><td colspan="4" class="muted">Brak oznaczonych wpłat z ostatnich 30 dni.</td></tr>{% endif %}
                </tbody>
              </table>
            </div>
          </div>
        {% endblock %}
        """
        return render_template_string(tpl, title="Cash flow", base_url=base_url, db_path=db_path,
                                      settings=settings, k=kpis, inflow_rows=inflow_rows,
                                      overdue_clients=overdue_clients, paid_clients=paid_clients,
                                      reorder_rows=reorder_rows)
