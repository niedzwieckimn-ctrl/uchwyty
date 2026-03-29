<!doctype html>
<html lang="pl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Panel Klienta</title>
  <script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>
  <style>
    body{font-family:Arial,sans-serif;background:#f5f7fb;margin:0;color:#111}
    .top{background:#111;color:#fff;padding:12px 14px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
    .wrap{max-width:1100px;margin:16px auto;padding:0 12px}
    .card{background:#fff;border:1px solid #e8e8ef;border-radius:12px;padding:12px;margin-bottom:12px}
    .btn{padding:8px 12px;border:1px solid #ddd;border-radius:10px;background:#fff;cursor:pointer}
    .btn.primary{background:#111;color:#fff;border-color:#111}
    input,textarea,select{width:100%;padding:10px;border:1px solid #ddd;border-radius:10px;box-sizing:border-box}
    table{width:100%;border-collapse:collapse}
    th,td{padding:8px;border-bottom:1px solid #eee;text-align:left;vertical-align:middle}
    .muted{color:#666;font-size:12px}
    .hidden{display:none}
    .row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
    .row3{display:grid;grid-template-columns:1fr 140px 140px;gap:10px}
    .badge{display:inline-block;padding:2px 8px;border:1px solid #ddd;border-radius:999px;font-size:12px}
    .status-chip{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;color:#fff}
    .status-new{background:#ef4444}
    .status-confirmed{background:#16a34a}
    .status-in-delivery{background:#2563eb}
    .status-other{background:#6b7280}
    @media(max-width:900px){.row,.row3{grid-template-columns:1fr}}
  </style>
</head>
<body>
  <div class="top">
    <b>Panel Klienta</b>
    <button id="btnLogout" class="btn hidden" type="button">Wyloguj</button>
    <span id="who" class="muted"></span>
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
      <div class="card">
        <h3>Stany magazynowe</h3>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
          <button id="btnLoadStock" class="btn" type="button">Odśwież stany</button>
          <input id="stockSearch" placeholder="Szukaj: SKU / model / nazwa..." style="max-width:360px" />
          <label class="btn" style="display:flex;align-items:center;gap:6px;cursor:pointer">
            <input id="onlyAvailable" type="checkbox" checked />
            Na stanie
          </label>
          <span id="stockCount" class="badge">Pozycji: 0</span>
        </div>

        <div class="muted" style="margin-top:6px">Dane bezpośrednio z <code>products + stock + china_items + china_packages</code></div>
        <div style="overflow:auto;margin-top:10px">
          <table id="stockTable">
            <thead>
              <tr>
                <th>SKU</th><th>Model</th><th>Nazwa</th><th>Na stanie</th><th>W drodze</th><th>Razem</th>
              </tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>
      </div>

      <div class="card">
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
              <tr><th>SKU</th><th>Model</th><th>Nazwa</th><th>Ilość</th><th></th></tr>
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

      <div class="card">
        <h3>Moje zamówienia</h3>
        <div class="muted">Statusy synchronizują się z główną aplikacją: <b>niepotwierdzone</b>, <b>potwierdzone</b>, <b>w dostawie</b>, <b>wydane</b>.</div>
        <button id="btnLoadOrders" class="btn" type="button" style="margin-top:8px">Odśwież zamówienia</button>
        <div style="overflow:auto;margin-top:10px">
          <table id="ordersTable">
            <thead>
              <tr><th>Nr</th><th>Status</th><th>Netto</th><th>Brutto</th><th>Data</th></tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>
      </div>

      <div class="card">
        <h3>Tracking / numer paczki</h3>
        <input id="tokenInput" placeholder="Wklej numer tracking albo numer paczki" />
        <button id="btnTrack" class="btn" type="button" style="margin-top:8px">Sprawdź paczkę</button>
        <pre id="trackOut" style="white-space:pre-wrap;background:#fafafa;padding:10px;border-radius:10px"></pre>
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

const STATUS_LABELS = {
  new: 'Niepotwierdzone',
  pending: 'Niepotwierdzone',
  unconfirmed: 'Niepotwierdzone',
  confirmed: 'Potwierdzone',
  packed: 'Potwierdzone',
  in_delivery: 'W dostawie',
  shipped: 'W dostawie',
  issued: 'Wydane',
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

const $ = (id)=>document.getElementById(id);
const authCard=$('authCard'), appArea=$('appArea'), who=$('who'), btnLogout=$('btnLogout');

let STOCK_RAW = [];
let STOCK_VIEW = [];
let ORDER_DRAFT = [];
let CURRENT_CUSTOMER = null;

function nowIso(){
  return new Date().toISOString().replace('T',' ').slice(0,19);
}

function makeOrderNo(id){
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth()+1).padStart(2,'0');
  const dd = String(d.getDate()).padStart(2,'0');
  return `ORD-${yyyy}${mm}${dd}-${String(id).padStart(6,'0')}`;
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
  who.textContent='';
  $('authMsg').textContent=msg;
}

function showApp(email) {
  authCard.classList.add('hidden');
  appArea.classList.remove('hidden');
  btnLogout.classList.remove('hidden');
  who.textContent=email;
}

async function getSession(){
  const { data:{ session }, error } = await sbAuth.auth.getSession();
  if(error) throw error;
  return session;
}

async function ensureCustomer(){
  const session = await getSession();
  if(!session?.user?.email) throw new Error('Brak zalogowanego użytkownika.');
  const email = session.user.email.trim().toLowerCase();

  const existingRes = await sbAuth
    .from('customers')
    .select('id,name,address,phone,email,nip')
    .eq('email', email)
    .order('id', { ascending:false })
    .limit(1);

  if(existingRes.error) throw existingRes.error;
  if(existingRes.data && existingRes.data.length){
    CURRENT_CUSTOMER = existingRes.data[0];
    return CURRENT_CUSTOMER;
  }

  const fallbackName = email.split('@')[0] || email;
  const inserted = await sbAuth
    .from('customers')
    .insert([{
      name: fallbackName,
      address: '',
      phone: '',
      email,
      nip: '',
      created_at: nowIso()
    }])
    .select('id,name,address,phone,email,nip')
    .single();

  if(inserted.error) throw inserted.error;
  CURRENT_CUSTOMER = inserted.data;
  return CURRENT_CUSTOMER;
}

async function loadSession() {
  try {
    const session = await getSession();
    if(!session){ showAuth(); return; }

    showApp(session.user?.email || 'zalogowany');
    await ensureCustomer();
    await loadStock();
    await loadOrders();
  } catch(error){
    showAuth('Błąd sesji: ' + error.message);
  }
}

$('btnLogin').onclick = async () => {
  const email=$('email').value.trim();
  const password=$('password').value;
  const { error } = await sbAuth.auth.signInWithPassword({ email, password });
  if(error){ showAuth('Błąd logowania: ' + error.message); return; }
  await loadSession();
};

$('btnRegister').onclick = async () => {
  const email=$('email').value.trim();
  const password=$('password').value;
  const { error } = await sbAuth.auth.signUp({ email, password });
  if(error){ showAuth('Błąd rejestracji: ' + error.message); return; }
  showAuth('Konto utworzone. Jeśli masz włączone potwierdzenie mailowe, potwierdź e-mail i zaloguj się.');
};

btnLogout.onclick = async () => {
  await sbAuth.auth.signOut();
  CURRENT_CUSTOMER = null;
  showAuth('Wylogowano.');
};

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
    qty_on_stock: onStock,
    qty_in_delivery: inDelivery,
    qty_total_visible: total
  };
}

function renderStockTable(){
  const tb = $('stockTable').querySelector('tbody');
  const q = ($('stockSearch')?.value || '').trim().toLowerCase();
  const onlyAvail = !!$('onlyAvailable')?.checked;

  let rows = [...STOCK_VIEW];

  if (q) {
    rows = rows.filter(r =>
      r.sku.toLowerCase().includes(q) ||
      r.model.toLowerCase().includes(q) ||
      r.name.toLowerCase().includes(q)
    );
  }

  if (onlyAvail) {
    rows = rows.filter(r => Number(r.qty_on_stock) > 0 || Number(r.qty_in_delivery) > 0);
  }

  $('stockCount').textContent = `Pozycji: ${rows.length}`;

  if (!rows.length) {
    tb.innerHTML = '<tr><td colspan="6">Brak danych</td></tr>';
    return;
  }

  tb.innerHTML = rows.map(r => `
    <tr>
      <td>${esc(r.sku)}</td>
      <td>${esc(r.model)}</td>
      <td>${esc(r.name)}</td>
      <td>${r.qty_on_stock}</td>
      <td>${r.qty_in_delivery}</td>
      <td>${r.qty_total_visible}</td>
    </tr>
  `).join('');
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

  const [productsRes, stockRes, packagesRes] = await Promise.all([
    sbPublic.from('products').select('id,sku,model,name').order('sku', { ascending:true }).limit(5000),
    sbPublic.from('stock').select('product_id,qty').limit(5000),
    sbPublic.from('china_packages').select('id,status').in('status', ['planned','ordered','shipped']).limit(5000)
  ]);

  if(productsRes.error){ tb.innerHTML = `<tr><td colspan="6">Błąd: ${productsRes.error.message}</td></tr>`; return; }
  if(stockRes.error){ tb.innerHTML = `<tr><td colspan="6">Błąd: ${stockRes.error.message}</td></tr>`; return; }
  if(packagesRes.error){ tb.innerHTML = `<tr><td colspan="6">Błąd: ${packagesRes.error.message}</td></tr>`; return; }

  const packageIds = (packagesRes.data || []).map(x => x.id);
  let chinaItems = [];
  if(packageIds.length){
    const ciRes = await sbPublic.from('china_items').select('package_id,product_id,qty').in('package_id', packageIds).limit(10000);
    if(ciRes.error){ tb.innerHTML = `<tr><td colspan="6">Błąd: ${ciRes.error.message}</td></tr>`; return; }
    chinaItems = ciRes.data || [];
  }

  const stockMap = new Map();
  (stockRes.data || []).forEach(r => stockMap.set(Number(r.product_id), Number(r.qty || 0)));

  const inDeliveryMap = new Map();
  chinaItems.forEach(r => {
    const pid = Number(r.product_id);
    inDeliveryMap.set(pid, (inDeliveryMap.get(pid) || 0) + Number(r.qty || 0));
  });

  STOCK_RAW = (productsRes.data || []).map(p => ({
    product_id: Number(p.id),
    sku: p.sku || '',
    model: p.model || '',
    name: p.name || '',
    qty_on_stock: Number(stockMap.get(Number(p.id)) || 0),
    qty_in_delivery: Number(inDeliveryMap.get(Number(p.id)) || 0),
    qty_total_visible: Number(stockMap.get(Number(p.id)) || 0) + Number(inDeliveryMap.get(Number(p.id)) || 0)
  }));
  STOCK_VIEW = STOCK_RAW.map(normalizeStockRow);

  renderStockTable();
  rebuildOrderProductList();
}

$('btnLoadStock').onclick = loadStock;
$('stockSearch').oninput = renderStockTable;
$('onlyAvailable').onchange = renderStockTable;

function renderOrderDraft(){
  const tb = $('orderDraftTable').querySelector('tbody');

  if(!ORDER_DRAFT.length){
    tb.innerHTML = '<tr><td colspan="5">Brak pozycji</td></tr>';
  } else {
    tb.innerHTML = ORDER_DRAFT.map((it, idx) => `
      <tr>
        <td>${esc(it.sku)}</td>
        <td>${esc(it.model)}</td>
        <td>${esc(it.name)}</td>
        <td><input type="number" min="1" value="${it.qty}" data-qty-idx="${idx}" style="width:90px" /></td>
        <td><button class="btn" type="button" data-del-idx="${idx}">Usuń</button></td>
      </tr>
    `).join('');
  }

  $('orderDraftSummary').textContent = `Pozycji: ${ORDER_DRAFT.length}`;
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
    const customer = await ensureCustomer();
    const email = session.user.email.trim().toLowerCase();
    const p_note = $('orderNote').value || null;

    const insertedOrder = await sbAuth
      .from('orders')
      .insert([{
        order_no: 'TEMP',
        customer_id: customer.id,
        customer_name: customer.name || email,
        customer_address: customer.address || '',
        customer_phone: customer.phone || '',
        customer_email: email,
        status: 'new',
        note: p_note,
        created_at: nowIso()
      }])
      .select('id')
      .single();

    if(insertedOrder.error) throw insertedOrder.error;

    const orderId = Number(insertedOrder.data.id);
    const orderNo = makeOrderNo(orderId);

    const updRes = await sbAuth
      .from('orders')
      .update({ order_no: orderNo })
      .eq('id', orderId);
    if(updRes.error) throw updRes.error;

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

    $('orderMsg').textContent = 'OK, utworzono zamówienie: ' + orderNo;
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
  tb.innerHTML = '<tr><td colspan="5">Ładowanie...</td></tr>';

  try {
    const session = await getSession();
    if(!session?.user?.email){
      tb.innerHTML = '<tr><td colspan="5">Brak sesji.</td></tr>';
      return;
    }
    const email = session.user.email.trim().toLowerCase();

    const ordersRes = await sbAuth
      .from('orders')
      .select('id,order_no,status,created_at')
      .eq('customer_email', email)
      .order('created_at', { ascending:false })
      .limit(200);

    if(ordersRes.error) throw ordersRes.error;
    const orders = ordersRes.data || [];

    if(!orders.length){
      tb.innerHTML = '<tr><td colspan="5">Brak zamówień</td></tr>';
      return;
    }

    const orderIds = orders.map(x => x.id);
    const itemsRes = await sbAuth
      .from('order_items')
      .select('order_id,product_id,qty,sku')
      .in('order_id', orderIds)
      .limit(10000);
    if(itemsRes.error) throw itemsRes.error;
    const items = itemsRes.data || [];

    const productIds = [...new Set(items.map(x => Number(x.product_id)).filter(Boolean))];
    let products = [];
    if(productIds.length){
      const prodRes = await sbPublic
        .from('products')
        .select('id,sku,model,name')
        .in('id', productIds)
        .limit(5000);
      if(prodRes.error) throw prodRes.error;
      products = prodRes.data || [];
    }

    const pricingRes = await sbPublic.from('pricing').select('model,net_price,gross_price').limit(5000);
    if(pricingRes.error) throw pricingRes.error;
    const pricing = pricingRes.data || [];

    const productMap = new Map(products.map(p => [Number(p.id), p]));
    const priceByModel = new Map();
    pricing.forEach(p => priceByModel.set(String(p.model || '').trim().toLowerCase(), p));

    const totalsByOrder = new Map();
    items.forEach(it => {
      const product = productMap.get(Number(it.product_id)) || {};
      const modelKey = String(product.model || '').trim().toLowerCase();
      const skuKey = String(product.sku || it.sku || '').trim().toLowerCase();
      const price = priceByModel.get(modelKey) || priceByModel.get(skuKey) || {};
      const net = Number(price.net_price || 0);
      const gross = Number(price.gross_price || 0);
      const prev = totalsByOrder.get(Number(it.order_id)) || { net:0, gross:0 };
      prev.net += net * Number(it.qty || 0);
      prev.gross += gross * Number(it.qty || 0);
      totalsByOrder.set(Number(it.order_id), prev);
    });

    tb.innerHTML = orders.map(r => {
      const st = orderStatusMeta(r.status);
      const totals = totalsByOrder.get(Number(r.id)) || { net:0, gross:0 };
      return `
        <tr>
          <td>${esc(r.order_no || r.id)}</td>
          <td><span class="status-chip ${st.cls}">${esc(st.label)}</span></td>
          <td>${Number(totals.net || 0).toFixed(2)}</td>
          <td>${Number(totals.gross || 0).toFixed(2)}</td>
          <td>${esc((r.created_at || '').replace('T',' ').slice(0,19))}</td>
        </tr>
      `;
    }).join('');
  } catch(error){
    tb.innerHTML = `<tr><td colspan="5">Błąd: ${esc(error.message)}</td></tr>`;
  }
}
$('btnLoadOrders').onclick = loadOrders;

$('btnTrack').onclick = async () => {
  const token = $('tokenInput').value.trim();
  if(!token){
    $('trackOut').textContent = 'Wpisz numer tracking albo numer paczki.';
    return;
  }

  try {
    let packages = [];
    let res = await sbPublic
      .from('china_packages')
      .select('id,package_no,status,tracking,note,created_at')
      .eq('tracking', token)
      .limit(20);
    if(res.error) throw res.error;
    packages = res.data || [];

    if(!packages.length){
      res = await sbPublic
        .from('china_packages')
        .select('id,package_no,status,tracking,note,created_at')
        .eq('package_no', token)
        .limit(20);
      if(res.error) throw res.error;
      packages = res.data || [];
    }

    if(!packages.length){
      $('trackOut').textContent = 'Brak paczki o takim numerze.';
      return;
    }

    const packageIds = packages.map(x => x.id);
    const itemsRes = await sbPublic
      .from('china_items')
      .select('id,package_id,product_id,sku,qty')
      .in('package_id', packageIds)
      .limit(10000);
    if(itemsRes.error) throw itemsRes.error;
    const items = itemsRes.data || [];

    const productIds = [...new Set(items.map(x => Number(x.product_id)).filter(Boolean))];
    let products = [];
    if(productIds.length){
      const prodRes = await sbPublic
        .from('products')
        .select('id,sku,model,name')
        .in('id', productIds)
        .limit(5000);
      if(prodRes.error) throw prodRes.error;
      products = prodRes.data || [];
    }
    const productMap = new Map(products.map(p => [Number(p.id), p]));

    const out = packages.map(p => ({
      package_no: p.package_no,
      status: p.status,
      tracking: p.tracking,
      note: p.note,
      created_at: p.created_at,
      items: items
        .filter(x => Number(x.package_id) === Number(p.id))
        .map(x => ({
          sku: x.sku,
          model: productMap.get(Number(x.product_id))?.model || '',
          name: productMap.get(Number(x.product_id))?.name || '',
          qty: x.qty
        }))
    }));

    $('trackOut').textContent = JSON.stringify(out, null, 2);
  } catch(error){
    $('trackOut').textContent = 'Błąd: ' + error.message;
  }
};

renderOrderDraft();
loadSession();
</script>

</body>
</html>
