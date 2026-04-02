<!doctype html>
<html lang="pl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Panel Klienta</title>
  <script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>
  <script src="https://cdn.jsdelivr.net/npm/qrcode@1.5.3/build/qrcode.min.js"></script>
  <script src="https://unpkg.com/html5-qrcode" type="text/javascript"></script>
  <style>
    body{font-family:Arial,sans-serif;background:#f5f7fb;margin:0;color:#111}
    .top{background:#111;color:#fff;padding:16px 14px 18px;border-bottom:1px solid rgba(255,255,255,.08)}
    .top-head{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
    .brand{font-size:20px;font-weight:700}
    .top .btn{padding:8px 14px;border-radius:16px}
    .who{color:#a3a3a3;font-size:13px}
    .client-nav{display:flex;gap:12px;flex-wrap:wrap;margin-top:14px}
    .client-nav a{color:#fff;text-decoration:none;padding:12px 18px;border:1px solid rgba(255,255,255,.22);border-radius:18px;background:rgba(255,255,255,.02);display:inline-block}
    .client-nav a:hover,.client-nav a.active{background:rgba(255,255,255,.12)}
    .wrap{max-width:1100px;margin:16px auto;padding:0 12px}
    .card{background:#fff;border:1px solid #e8e8ef;border-radius:18px;padding:14px;margin-bottom:14px;box-shadow:0 6px 18px rgba(0,0,0,.03)}
    .btn{padding:10px 14px;border:1px solid #ddd;border-radius:14px;background:#fff;cursor:pointer}
    .btn.primary{background:#111;color:#fff;border-color:#111}
    input,textarea,select{width:100%;padding:12px;border:1px solid #ddd;border-radius:14px;box-sizing:border-box}
    table{width:100%;border-collapse:collapse}
    th,td{padding:10px 8px;border-bottom:1px solid #eee;text-align:left;vertical-align:middle}
    th{font-size:14px}
    .muted{color:#666;font-size:12px}
    .hidden{display:none}
    .row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
    .row3{display:grid;grid-template-columns:1fr 140px 140px;gap:10px}
    .badge{display:inline-block;padding:4px 10px;border:1px solid #ddd;border-radius:999px;font-size:12px}
    .status-chip{display:inline-block;padding:4px 10px;border-radius:999px;font-size:12px;color:#fff}
    .status-new{background:#ef4444}
    .status-confirmed{background:#16a34a}
    .status-in-delivery{background:#2563eb}
    .status-other{background:#6b7280}
    #qrScannerModal.hidden{display:none}
    h3{font-size:22px;margin:4px 0 10px}
    @media(max-width:900px){.row,.row3{grid-template-columns:1fr}}
  </style>
</head>
<body>
  <div class="top">
    <div class="top-head">
      <div class="brand">Panel Klienta</div>
      <button id="btnLogout" class="btn hidden" type="button">Wyloguj</button>
      <span id="who" class="who"></span>
    </div>
    <div id="clientNav" class="client-nav hidden">
      <a href="#" data-target="stockSection">Stany magazynowe</a>
      <a href="#" data-target="orderSection">Złóż zamówienie</a>
      <a href="#" data-target="ordersSection">Moje zamówienia</a>
      <a href="#" data-target="invoicesSection">Faktury</a>
      <a href="#" data-target="settingsSection">Ustawienia</a>
      <a href="#" data-target="scanSection">Skan QR</a>
    </div>
    <div id="orderSummaryBar" class="hidden" style="margin-top:12px;display:flex;gap:10px;flex-wrap:wrap;">
      <span class="badge" style="background:rgba(255,255,255,.06);border-color:rgba(255,255,255,.18);color:#fff;padding:8px 12px;">
        Zamówienia w toku: <b id="ordersInProgressCount">0</b>
      </span>
      <span class="badge" style="background:rgba(255,255,255,.06);border-color:rgba(255,255,255,.18);color:#fff;padding:8px 12px;">
        Zamówienia zrealizowane: <b id="ordersDoneCount">0</b>
      </span>
    </div>
  </div>

  <div class="wrap">
    <div id="authCard" class="card">
      <h3>Logowanie klienta</h3>
      <form onsubmit="return false;">
        <div class="row">
          <div>
            <label for="email">Email</label>
            <input id="email" type="email" autocomplete="email" placeholder="email" />
          </div>
          <div>
            <label for="password">Hasło</label>
            <input id="password" type="password" autocomplete="current-password" placeholder="hasło" />
          </div>
        </div>
        <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
          <button id="btnLogin" class="btn primary" type="button">Zaloguj</button>
          <button id="btnRegister" class="btn" type="button">Załóż konto</button>
        </div>
      </form>
      <div id="authMsg" class="muted" style="margin-top:8px"></div>
    </div>

    <div id="appArea" class="hidden">
      <div class="card section-block hidden" id="stockSection">
        <h3>Stany magazynowe</h3>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
          <button id="btnLoadStock" class="btn" type="button">Odśwież stany</button>
          <input id="stockSearch" placeholder="Szukaj: model / nazwa..." style="max-width:360px" />
          <label class="btn" style="display:flex;align-items:center;gap:6px;cursor:pointer">
            <input id="onlyAvailable" type="checkbox" />
            Na stanie
          </label>
          <span id="stockCount" class="badge">Pozycji: 0</span>
        </div>

                <div style="overflow:auto;margin-top:10px">
          <table id="stockTable">
            <thead>
              <tr>
                <th>Model</th><th>Nazwa</th><th>Netto B2B</th><th>Brutto klient</th><th>Na stanie</th>
              </tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>
      </div>

      <div class="card section-block hidden" id="orderSection">
        <h3>Złóż zamówienie</h3>

        <div class="row3">
          <div>
            <label>Produkt (pisz, aby wyszukać)</label>
            <input id="orderProductInput" list="orderProductList" placeholder="np. CH010 / Winsor..." />
            <datalist id="orderProductList"></datalist>
          </div>
          <div>
            <label>Ilość</label>
            <input id="orderQtyInput" type="number" min="1" value="1" />
          </div>
          <div style="display:flex;align-items:end">
            <button id="btnAddOrderItem" class="btn" type="button" style="width:100%">Dodaj pozycję</button>
          </div>
        </div>

        <div style="overflow:auto;margin-top:10px">
          <table id="orderDraftTable">
            <thead>
              <tr><th>Model</th><th>Nazwa</th><th>Netto/szt</th><th>Brutto/szt</th><th>Ilość</th><th>Wartość brutto</th><th></th></tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>

        <input id="orderNote" placeholder="Notatka (opcjonalnie)" style="margin-top:8px" />
        <div id="orderDraftSummary" class="muted" style="margin-top:8px">Pozycji: 0</div>

        <div style="margin-top:8px">
          <button id="btnCreateOrder" class="btn primary" type="button">Wyślij zamówienie</button>
        </div>
        <div id="orderMsg" class="muted" style="margin-top:8px"></div>
      </div>

      <div class="card section-block hidden" id="ordersSection">
        <h3>Moje zamówienia</h3>
        <div class="muted">Statusy synchronizują się z główną aplikacją: <b>niepotwierdzone</b>, <b>potwierdzone</b>, <b>w dostawie</b>, <b>zrealizowane</b>.</div>
        <button id="btnLoadOrders" class="btn" type="button" style="margin-top:8px">Odśwież zamówienia</button>
        <div style="overflow:auto;margin-top:10px">
          <table id="ordersTable">
            <thead>
              <tr><th>Nr</th><th>Status</th><th>Netto</th><th>Brutto</th><th>Data</th><th>Akcje</th></tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>
      </div>

      <div class="card section-block hidden" id="invoicesSection">
        <h3>Faktury</h3>
        <div class="muted">Tutaj widzisz faktury, które zostały udostępnione klientowi z panelu magazynu.</div>
        <button id="btnLoadInvoices" class="btn" type="button" style="margin-top:8px">Odśwież faktury</button>
        <div style="overflow:auto;margin-top:10px">
          <table id="invoicesTable">
            <thead>
              <tr><th>Nr faktury</th><th>Nr zamówienia</th><th>Data</th><th>Netto</th><th>Brutto</th><th>PDF</th></tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>
      </div>

      <div class="card section-block hidden" id="settingsSection">
        <h3>Ustawienia</h3>
        <div class="muted">Tutaj zmienisz hasło do logowania w panelu klienta.</div>
        <div class="row" style="margin-top:10px;">
          <div>
            <label>Nowe hasło</label>
            <input id="newPassword" type="password" autocomplete="new-password" placeholder="Nowe hasło" />
          </div>
          <div>
            <label>Powtórz nowe hasło</label>
            <input id="repeatPassword" type="password" autocomplete="new-password" placeholder="Powtórz nowe hasło" />
          </div>
        </div>
        <div style="margin-top:10px;">
          <button id="btnChangePassword" class="btn primary" type="button">Zmień hasło</button>
        </div>
        <div id="passwordMsg" class="muted" style="margin-top:8px"></div>
      </div>

      <div class="card section-block hidden" id="scanSection">
        <h3 id="openQrScanner" style="cursor:pointer;text-decoration:underline;">Skanuj QR zamówienia</h3>
        <div>
          <input id="orderCodeInput" placeholder="Wklej kod zamówienia ZAM-... albo kliknij „Skanuj QR zamówienia”" />
          <button id="btnShowOrderByCode" class="btn" type="button" style="margin-top:8px">Pokaż zamówienie</button>
        </div>
        <div id="orderScanOut" style="margin-top:10px"></div>
      </div>

      <div id="qrScannerModal" class="hidden" style="position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:9999;padding:16px;box-sizing:border-box;">
        <div style="max-width:420px;margin:4vh auto;background:#fff;border-radius:16px;padding:12px;">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px;">
            <b>Skanuj QR</b>
            <button id="closeQrScanner" class="btn" type="button">Zamknij</button>
          </div>
          <div class="muted" style="margin-bottom:8px;">Uruchamia się tylko tylny aparat.</div>
          <div id="orderQrReader" style="width:100%;min-height:280px;"></div>
        </div>
      </div>

    </div>
  </div>

<script>
const SUPABASE_URL = "https://qfzawzkynmqkbjlbtkjd.supabase.co";
const SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InFmemF3emt5bm1xa2JqbGJ0a2pkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzQ1MjQ4MTAsImV4cCI6MjA5MDEwMDgxMH0.Z_FcKX_iMS7rdx8gNOAWOEBJgO2MA7cqm8ppZiqu7DM";

const sbAuth = supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
  auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true }
});

const sbPublic = supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
  auth: { persistSession: false, autoRefreshToken: false, detectSessionInUrl: false }
});

window.sb = sbAuth;
window.sbPublic = sbPublic;
const MAGAZYN_API_BASE = "https://uchwyty.onrender.com";

const STATUS_LABELS = {
  new: 'Niepotwierdzone',
  pending: 'Niepotwierdzone',
  unconfirmed: 'Niepotwierdzone',
  confirmed: 'Potwierdzone',
  packed: 'Potwierdzone',
  in_delivery: 'W dostawie',
  shipped: 'W dostawie',
  issued: 'Zrealizowane',
  cancelled: 'Anulowane'
};

function orderStatusMeta(statusRaw) {
  const key = String(statusRaw || '').toLowerCase();
  if (key === 'new' || key === 'pending' || key === 'unconfirmed') {
    return { label: STATUS_LABELS[key], cls: 'status-new' };
  }
  if (key === 'confirmed' || key === 'packed') {
    return { label: STATUS_LABELS[key], cls: 'status-confirmed' };
  }
  if (key === 'in_delivery' || key === 'shipped') {
    return { label: STATUS_LABELS[key], cls: 'status-in-delivery' };
  }
  return { label: STATUS_LABELS[key] || statusRaw || '-', cls: 'status-other' };
}

function updateOrderSummaryBar(orders){
  const rows = Array.isArray(orders) ? orders : [];
  const inProgressStatuses = new Set(['new','pending','unconfirmed','confirmed','packed','in_delivery','shipped']);
  const doneStatuses = new Set(['issued']);
  let inProgress = 0;
  let done = 0;

  rows.forEach(o => {
    const st = String(o?.status || '').toLowerCase();
    if (inProgressStatuses.has(st)) inProgress += 1;
    if (doneStatuses.has(st)) done += 1;
  });

  $('ordersInProgressCount').textContent = String(inProgress);
  $('ordersDoneCount').textContent = String(done);
}

const $ = (id)=>document.getElementById(id);
const authCard=$('authCard'), appArea=$('appArea'), who=$('who'), btnLogout=$('btnLogout');

let STOCK_RAW = [];
let STOCK_VIEW = [];
let ORDER_DRAFT = [];
let CURRENT_CUSTOMER = null;
let PRICE_BY_MODEL = new Map();
let PRICE_BY_SKU = new Map();

function nowIso(){
  return new Date().toISOString().replace('T',' ').slice(0,19);
}

function makeOrderNo(id){
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth()+1).padStart(2,'0');
  const dd = String(d.getDate()).padStart(2,'0');
  return `ZAM-${yyyy}${mm}${dd}-${String(id).padStart(6,'0')}`;
}

function makeClientOrderNo(){
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth()+1).padStart(2,'0');
  const dd = String(d.getDate()).padStart(2,'0');
  const hh = String(d.getHours()).padStart(2,'0');
  const mi = String(d.getMinutes()).padStart(2,'0');
  const ss = String(d.getSeconds()).padStart(2,'0');
  const rand = Math.random().toString(36).slice(2,6).toUpperCase();
  return `ZAM-${yyyy}${mm}${dd}-${hh}${mi}${ss}-${rand}`;
}

function displayOrderNo(order){
  const raw = String(order?.order_no || '').trim();
  if(raw && raw.toUpperCase() !== 'TEMP'){
    return raw.replace(/^ORD-/, 'ZAM-');
  }

  const id = String(order?.id || '').padStart(6, '0');
  const created = String(order?.created_at || '');
  const datePart = created ? created.slice(0,10).replace(/-/g,'') : new Date().toISOString().slice(0,10).replace(/-/g,'');
  return `ZAM-${datePart}-${id}`;
}

async function makeQrDataUrl(value){
  try {
    return await QRCode.toDataURL(String(value || '').trim(), { width: 220, margin: 1 });
  } catch(e){
    return '';
  }
}

function formatMoney(v){
  return Number(v || 0).toFixed(2);
}

function calcClientGrossFromNet(netB2B){
  return Number(netB2B || 0) * 1.45 * 1.23;
}

async function fetchMagazynJson(path){
  const res = await fetch(`${MAGAZYN_API_BASE}${path}`);
  if(!res.ok){
    throw new Error(`Magazyn API: ${res.status}`);
  }
  return await res.json();
}

function renderOrderDetailsHtml(order, items, totals){
  const rows = (items || []).map(it => `
    <tr>
      <td>${esc(it.sku)}</td>
      <td>${esc(it.name || '')}</td>
      <td>${formatMoney(it.net_price)}</td>
      <td>${formatMoney(it.gross_price)}</td>
      <td>${Number(it.qty || 0)}</td>
      <td>${formatMoney(it.line_net)}</td>
      <td>${formatMoney(it.line_gross)}</td>
    </tr>
  `).join('') || '<tr><td colspan="7">Brak pozycji</td></tr>';

  return `
    <div class="card" style="margin:8px 0 0 0">
      <div>
        <div><b>${esc(displayOrderNo(order))}</b></div>
        <div class="muted" style="margin-top:6px">Klient: ${esc(order.customer_name || '-')}</div>
        <div class="muted">Email: ${esc(order.customer_email || '-')}</div>
        <div class="muted">Data: ${esc(String(order.created_at || '').replace('T',' ').slice(0,19))}</div>
        <div class="muted">Notatka: ${esc(order.note || '-')}</div>
        <div style="margin-top:8px"><span class="badge">Netto: ${formatMoney(totals.net)}</span> <span class="badge">Brutto: ${formatMoney(totals.gross)}</span></div>
      </div>
      <div style="overflow:auto;margin-top:10px">
        <table>
          <thead>
            <tr><th>Model</th><th>Nazwa</th><th>Netto/szt</th><th>Brutto/szt</th><th>Ilość</th><th>Wartość netto</th><th>Wartość brutto</th></tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>
  `;
}

async function fetchOrdersBundle(){
  const session = await getSession();
  if(!session?.user?.email) throw new Error('Brak sesji.');
  const email = session.user.email.trim().toLowerCase();

  const ordersRes = await sbAuth
    .from('orders')
    .select('id,order_no,qr_data_url,status,created_at,customer_name,customer_email,note')
    .eq('customer_email', email)
    .order('created_at', { ascending:false })
    .limit(200);
  if(ordersRes.error) throw ordersRes.error;

  const orders = ordersRes.data || [];
  const itemsByOrder = new Map();
  const totalsByOrder = new Map();

  await Promise.all(
    orders.map(async (order) => {
      try {
        const code = displayOrderNo(order);
        const payload = await fetchMagazynJson(`/api/order_lookup?token=${encodeURIComponent(code)}`);
        const items = (payload.items || []).map(it => ({
          order_id: Number(order.id),
          product_id: Number(it.product_id || 0),
          sku: it.sku || '',
          model: it.model || '',
          name: it.name || '',
          qty: Number(it.qty || 0),
          net_price: Number(it.net_price || 0),
          gross_price: Number(it.gross_price || 0),
          line_net: Number(it.line_value_net || 0),
          line_gross: Number(it.line_value_gross || 0)
        }));
        itemsByOrder.set(Number(order.id), items);
        totalsByOrder.set(Number(order.id), {
          net: Number(payload.order?.total_net ?? payload.total_net ?? 0),
          gross: Number(payload.order?.total_gross ?? payload.total_gross ?? 0)
        });
      } catch(error) {
        itemsByOrder.set(Number(order.id), []);
        totalsByOrder.set(Number(order.id), { net:0, gross:0 });
      }
    })
  );

  return { orders, itemsByOrder, totalsByOrder };
}

function parseOrderCode(value){
  const raw = String(value || '').trim();
  if(!raw) return '';

  const match = raw.match(/ZAM-[0-9]{8}-[A-Z0-9-]+/i);
  if(match) return match[0].toUpperCase();

  try {
    const url = new URL(raw);
    const fromPath = url.pathname.match(/ZAM-[0-9]{8}-[A-Z0-9-]+/i);
    if(fromPath) return fromPath[0].toUpperCase();
  } catch(e) {}

  return raw.toUpperCase();
}

async function loadInvoices(){
  const tb = $('invoicesTable').querySelector('tbody');
  const oldHtml = tb.innerHTML;
  tb.innerHTML = '<tr><td colspan="6">Ładowanie...</td></tr>';

  try {
    const session = await getSession();
    if(!session?.user?.email) throw new Error('Brak sesji.');
    const emailRaw = session.user.email.trim().toLowerCase();
    const email = encodeURIComponent(emailRaw);
    const payload = await fetchMagazynJson(`/api/client_invoices?email=${email}`);
    const rows = payload.invoices || [];

    if(!rows.length){
      tb.innerHTML = oldHtml || '<tr><td colspan="6">Brak faktur</td></tr>';
      return;
    }

    tb.innerHTML = rows.map(r => `
      <tr>
        <td><b>${esc(r.invoice_no || '')}</b></td>
        <td>${esc(r.order_no || '')}</td>
        <td>${esc(r.issue_date || '')}</td>
        <td>${formatMoney(r.total_net)}</td>
        <td>${formatMoney(r.total_gross)}</td>
        <td><a class="btn" href="${MAGAZYN_API_BASE}/api/invoices/${r.id}/download?email=${email}" target="_blank" rel="noopener">Pobierz PDF</a></td>
      </tr>
    `).join('');
  } catch(error){
    tb.innerHTML = oldHtml || `<tr><td colspan="6">Błąd: ${esc(error.message)}</td></tr>`;
  }
}

async function showOrderByCode(rawCode){
  const out = $('orderScanOut');
  const code = parseOrderCode(rawCode || $('orderCodeInput').value || '');
  if(!code){
    out.innerHTML = '<div class="muted">Wpisz albo zeskanuj kod ZAM-...</div>';
    return;
  }

  try {
    const bundle = await fetchOrdersBundle();
    const order = (bundle.orders || []).find(o => displayOrderNo(o) === code || String(o.order_no || '').trim() === code);
    if(!order){
      out.innerHTML = '<div class="muted">Nie znaleziono Twojego zamówienia o takim kodzie.</div>';
      return;
    }
    const items = bundle.itemsByOrder.get(Number(order.id)) || [];
    const totals = bundle.totalsByOrder.get(Number(order.id)) || { net:0, gross:0 };
    out.innerHTML = renderOrderDetailsHtml(order, items, totals);
    out.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch(error){
    out.innerHTML = `<div class="muted">Błąd: ${esc(error.message)}</div>`;
  }
}

function esc(v){
  return String(v ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function showAuth(msg='') {
  authCard.classList.remove('hidden');
  appArea.classList.add('hidden');
  btnLogout.classList.add('hidden');
  $('clientNav').classList.add('hidden');
  $('orderSummaryBar').classList.add('hidden');
  hideAllSections();
  who.textContent='';
  $('authMsg').textContent=msg;
}

function showApp(email) {
  authCard.classList.add('hidden');
  appArea.classList.remove('hidden');
  btnLogout.classList.remove('hidden');
  $('clientNav').classList.remove('hidden');
  $('orderSummaryBar').classList.remove('hidden');
  hideAllSections();
  who.textContent=email;
}

async function getSession(){
  const { data:{ session }, error } = await sbAuth.auth.getSession();
  if(error) throw error;
  return session;
}

function hideAllSections(){
  document.querySelectorAll('.section-block').forEach(el => el.classList.add('hidden'));
  document.querySelectorAll('#clientNav [data-target]').forEach(el => el.classList.remove('active'));
}

function openSection(sectionId){
  hideAllSections();
  const el = document.getElementById(sectionId);
  const btn = document.querySelector(`#clientNav [data-target="${sectionId}"]`);
  if(el) el.classList.remove('hidden');
  if(btn) btn.classList.add('active');
  if(el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function loadSession() {
  try {
    const session = await getSession();
    if(!session){ showAuth(); return; }

    showApp(session.user?.email || 'zalogowany');
    $('authMsg').textContent = '';

    try { await loadStock(); } catch(e) {}
    try { await loadOrders(); } catch(e) {}
  } catch(error){
    showAuth('Błąd sesji: ' + error.message);
  }
}

$('btnLogin').onclick = async () => {
  const email=$('email').value.trim();
  const password=$('password').value;
  const { data, error } = await sbAuth.auth.signInWithPassword({ email, password });
  if(error){ showAuth('Błąd logowania: ' + error.message); return; }
  if(!data?.session){
    showAuth('Logowanie przyjęte, ale brak aktywnej sesji. Jeśli masz włączone potwierdzenie mailowe, potwierdź e-mail.');
    return;
  }
  await loadSession();
};

$('btnRegister').onclick = async () => {
  const email=$('email').value.trim();
  const password=$('password').value;
  const { data, error } = await sbAuth.auth.signUp({ email, password });
  if(error){ showAuth('Błąd rejestracji: ' + error.message); return; }
  if(data?.session){
    await loadSession();
    return;
  }
  showAuth('Konto utworzone. Jeśli masz włączone potwierdzenie mailowe, potwierdź e-mail i dopiero się zaloguj.');
};

btnLogout.onclick = async () => {
  await sbAuth.auth.signOut();
  CURRENT_CUSTOMER = null;
  showAuth('Wylogowano.');
};

sbAuth.auth.onAuthStateChange((_event, session) => {
  if(session){
    loadSession();
  } else {
    showAuth();
  }
});

function normalizeStockRow(r){
  const rawOn = Number(r.qty_on_stock ?? 0);
  const rawIn = Number(r.qty_in_delivery ?? 0);
  const debt = Math.max(0, -rawOn);
  const onStock = Math.max(0, rawOn);
  const inDelivery = Math.max(0, rawIn - debt);
  const total = onStock + inDelivery;

  return {
    product_id: Number(r.product_id),
    sku: r.sku || '',
    model: r.model || '',
    name: r.name || '',
    net_price: Number(r.net_price || 0),
    gross_price: Number(r.gross_price || 0),
    qty_on_stock: onStock,
    qty_in_delivery: inDelivery,
    qty_total_visible: total
  };
}

function renderStockTable(){
  const tb = $('stockTable').querySelector('tbody');
  const q = ($('stockSearch')?.value || '').trim().toLowerCase();
  const onlyAvail = !!$('onlyAvailable')?.checked;

  if (!q && !onlyAvail) {
    $('stockCount').textContent = 'Pozycji: 0';
    tb.innerHTML = '<tr><td colspan="5">Wpisz model / SKU / nazwę albo zaznacz „Na stanie”.</td></tr>';
    return;
  }

  let rows = [...STOCK_VIEW];

  if (q) {
    rows = rows.filter(r =>
      r.sku.toLowerCase().includes(q) ||
      r.model.toLowerCase().includes(q) ||
      r.name.toLowerCase().includes(q)
    );
  }

  if (onlyAvail) {
    rows = rows.filter(r => Number(r.qty_on_stock) > 0);
  }

  $('stockCount').textContent = `Pozycji: ${rows.length}`;

  if (!rows.length) {
    tb.innerHTML = '<tr><td colspan="5">Brak danych</td></tr>';
    return;
  }

  tb.innerHTML = rows.map(r => {
    const hasPrice = Number(r.net_price || 0) > 0;
    return `
    <tr>
      <td>${esc(r.sku)}</td>
      <td>${esc(r.name)}</td>
      <td>${hasPrice ? formatMoney(r.net_price) : '—'}</td>
      <td>${hasPrice ? formatMoney(calcClientGrossFromNet(r.net_price)) : '—'}</td>
      <td>${r.qty_on_stock}</td>
    </tr>
  `;
  }).join('');
}

function rebuildOrderProductList(){
  const dl = $('orderProductList');
  dl.innerHTML = '';
  STOCK_VIEW.forEach(r => {
    const opt = document.createElement('option');
    opt.value = `${r.sku} | ${r.model} | ${r.name} | #${r.product_id}`;
    dl.appendChild(opt);
  });
}

async function loadStock(){
  const tb = $('stockTable').querySelector('tbody');
  tb.innerHTML = '<tr><td colspan="6">Ładowanie...</td></tr>';

  try {
    const payload = await fetchMagazynJson('/api/client_stock_catalog');
    const rows = payload.rows || [];

    STOCK_RAW = rows.map(r => ({
      product_id: Number(r.product_id),
      sku: r.sku || '',
      model: r.model || '',
      name: r.name || '',
      net_price: Number(r.net_price || 0),
      gross_price: Number(r.gross_price || 0),
      qty_on_stock: Number(r.qty_on_stock || 0),
      qty_in_delivery: 0,
      qty_total_visible: Number(r.qty_on_stock || 0)
    }));

    STOCK_VIEW = STOCK_RAW.map(normalizeStockRow);
    renderStockTable();
    rebuildOrderProductList();
  } catch(error){
    updateOrderSummaryBar([]);
    tb.innerHTML = `<tr><td colspan="6">Błąd: ${esc(error.message)}</td></tr>`;
  }
}

$('btnLoadStock').onclick = loadStock;
$('stockSearch').oninput = renderStockTable;
$('onlyAvailable').onchange = renderStockTable;

function renderOrderDraft(){
  const tb = $('orderDraftTable').querySelector('tbody');

  if(!ORDER_DRAFT.length){
    tb.innerHTML = '<tr><td colspan="7">Brak pozycji</td></tr>';
  } else {
    tb.innerHTML = ORDER_DRAFT.map((it, idx) => {
      const grossLine = Number(it.gross_price || 0) * Number(it.qty || 0);
      return `
      <tr>
        <td>${esc(it.sku)}</td>
        <td>${esc(it.name)}</td>
        <td>${Number(it.net_price || 0).toFixed(2)}</td>
        <td>${Number(it.gross_price || 0).toFixed(2)}</td>
        <td><input type="number" min="1" value="${it.qty}" data-qty-idx="${idx}" style="width:90px" /></td>
        <td>${grossLine.toFixed(2)}</td>
        <td><button class="btn" type="button" data-del-idx="${idx}">Usuń</button></td>
      </tr>
    `;
    }).join('');
  }

  const totalNet = ORDER_DRAFT.reduce((sum, it) => sum + Number(it.net_price || 0) * Number(it.qty || 0), 0);
  const totalGross = ORDER_DRAFT.reduce((sum, it) => sum + Number(it.gross_price || 0) * Number(it.qty || 0), 0);
  $('orderDraftSummary').textContent = `Pozycji: ${ORDER_DRAFT.length} | Netto: ${totalNet.toFixed(2)} | Brutto: ${totalGross.toFixed(2)}`;
}

function parseProductInputToId(txt){
  const m = String(txt || '').match(/#(\d+)\s*$/);
  return m ? Number(m[1]) : null;
}

$('btnAddOrderItem').onclick = () => {
  const txt = $('orderProductInput').value.trim();
  const qty = Math.max(1, Number($('orderQtyInput').value || 1));
  const productId = parseProductInputToId(txt);

  if(!productId){
    $('orderMsg').textContent = 'Wybierz produkt z listy podpowiedzi.';
    return;
  }

  const row = STOCK_VIEW.find(x => Number(x.product_id) === Number(productId));
  if(!row){
    $('orderMsg').textContent = 'Nie znaleziono produktu.';
    return;
  }

  const existing = ORDER_DRAFT.find(x => Number(x.product_id) === Number(productId));
  if(existing){
    existing.qty += qty;
  } else {
    ORDER_DRAFT.push({
      product_id: Number(row.product_id),
      sku: row.sku,
      model: row.model,
      name: row.name,
      net_price: Number(row.net_price || 0),
      gross_price: Number(row.gross_price || 0),
      qty
    });
  }

  $('orderProductInput').value = '';
  $('orderQtyInput').value = 1;
  $('orderMsg').textContent = '';
  renderOrderDraft();
};

$('orderDraftTable').addEventListener('input', (e) => {
  const idx = e.target?.dataset?.qtyIdx;
  if(idx == null) return;
  ORDER_DRAFT[Number(idx)].qty = Math.max(1, Number(e.target.value || 1));
  $('orderMsg').textContent = '';
});

$('orderDraftTable').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-del-idx]');
  if(!btn) return;
  ORDER_DRAFT.splice(Number(btn.dataset.delIdx), 1);
  renderOrderDraft();
  $('orderMsg').textContent = '';
});

$('btnCreateOrder').onclick = async () => {
  if(!ORDER_DRAFT.length){
    $('orderMsg').textContent = 'Dodaj przynajmniej jedną pozycję.';
    return;
  }

  try {
    const session = await getSession();
    if(!session?.user?.email) throw new Error('Brak zalogowanego użytkownika.');
    const email = session.user.email.trim().toLowerCase();
    const fallbackName = email.split('@')[0] || email;
    const p_note = $('orderNote').value || null;

    const orderNo = makeClientOrderNo();
    const qrDataUrl = await makeQrDataUrl(orderNo);

    const insertedOrder = await sbAuth
      .from('orders')
      .insert([{
        order_no: orderNo,
        customer_id: null,
        customer_name: fallbackName,
        customer_address: '',
        customer_phone: '',
        customer_email: email,
        status: 'new',
        note: p_note,
        created_at: nowIso(),
        qr_data_url: qrDataUrl
      }])
      .select('id,order_no,qr_data_url')
      .single();

    if(insertedOrder.error) throw insertedOrder.error;

    const orderId = Number(insertedOrder.data.id);

    const itemsPayload = ORDER_DRAFT.map(x => ({
      order_id: orderId,
      product_id: x.product_id,
      sku: x.sku,
      qty: x.qty,
      created_at: nowIso()
    }));

    const itemsRes = await sbAuth.from('order_items').insert(itemsPayload);
    if(itemsRes.error){
      await sbAuth.from('orders').delete().eq('id', orderId);
      throw itemsRes.error;
    }

    $('orderMsg').textContent = 'OK, utworzono zamówienie: ' + (insertedOrder.data?.order_no || orderNo);
    ORDER_DRAFT = [];
    $('orderNote').value = '';
    renderOrderDraft();
    await loadOrders();
  } catch(error){
    $('orderMsg').textContent = 'Błąd: ' + error.message;
  }
};

async function loadOrders(){
  const tb = $('ordersTable').querySelector('tbody');
  tb.innerHTML = '<tr><td colspan="6">Ładowanie...</td></tr>';

  try {
    const bundle = await fetchOrdersBundle();
    const orders = bundle.orders || [];
    updateOrderSummaryBar(orders);

    if(!orders.length){
      tb.innerHTML = '<tr><td colspan="5">Brak zamówień</td></tr>';
      return;
    }

    tb.innerHTML = orders.map(r => {
      const st = orderStatusMeta(r.status);
      const totals = bundle.totalsByOrder.get(Number(r.id)) || { net:0, gross:0 };
      const items = bundle.itemsByOrder.get(Number(r.id)) || [];
      const detailsHtml = renderOrderDetailsHtml(r, items, totals);
      return `
        <tr>
          <td>${esc(displayOrderNo(r))}</td>
          <td><span class="status-chip ${st.cls}">${esc(st.label)}</span></td>
          <td>${formatMoney(totals.net)}</td>
          <td>${formatMoney(totals.gross)}</td>
          <td>${esc((r.created_at || '').replace('T',' ').slice(0,19))}</td>
          <td><button class="btn" type="button" data-open-order="${r.id}">Szczegóły</button></td>
        </tr>
        <tr id="order-detail-${r.id}" class="hidden">
          <td colspan="5">${detailsHtml}</td>
        </tr>
      `;
    }).join('');

    tb.querySelectorAll('[data-open-order]').forEach(btn => {
      btn.onclick = () => {
        const id = btn.getAttribute('data-open-order');
        const row = document.getElementById(`order-detail-${id}`);
        if(row) row.classList.toggle('hidden');
      };
    });
  } catch(error){
    tb.innerHTML = `<tr><td colspan="6">Błąd: ${esc(error.message)}</td></tr>`;
  }
}
$('btnLoadOrders').onclick = loadOrders;
if ($('btnLoadInvoices')) $('btnLoadInvoices').onclick = loadInvoices;
$('btnShowOrderByCode').onclick = () => showOrderByCode();


document.querySelectorAll('#clientNav [data-target]').forEach(link => {
  link.onclick = async (e) => {
    e.preventDefault();
    openSection(link.dataset.target);

    if(link.dataset.target === 'stockSection'){
      await loadStock();
    }
    if(link.dataset.target === 'orderSection'){
      await loadStock();
      renderOrderDraft();
    }
    if(link.dataset.target === 'ordersSection'){
      await loadOrders();
    }
    if(link.dataset.target === 'invoicesSection'){
      await loadInvoices();
    }
  };
});

$('btnChangePassword').onclick = async () => {
  const p1 = ($('newPassword').value || '').trim();
  const p2 = ($('repeatPassword').value || '').trim();

  if(!p1 || !p2){
    $('passwordMsg').textContent = 'Wpisz nowe hasło dwa razy.';
    return;
  }
  if(p1 !== p2){
    $('passwordMsg').textContent = 'Hasła nie są takie same.';
    return;
  }
  if(p1.length < 6){
    $('passwordMsg').textContent = 'Hasło musi mieć co najmniej 6 znaków.';
    return;
  }

  try {
    const { error } = await sbAuth.auth.updateUser({ password: p1 });
    if(error) throw error;
    $('newPassword').value = '';
    $('repeatPassword').value = '';
    $('passwordMsg').textContent = 'Hasło zostało zmienione.';
  } catch(error){
    $('passwordMsg').textContent = 'Błąd: ' + error.message;
  }
};

renderOrderDraft();

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
    await showOrderByCode(parsedCode || decodedText);
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
      $('orderScanOut').innerHTML = '<div class="muted">Nie udało się uruchomić tylnego aparatu.</div>';
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

loadSession();
</script>

</body>
</html>
