"use strict";
(() => {
  const byId = id => document.getElementById(id);
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  let stop = false, busy = false, saved = null, lastSingle = null;
  function show(value) {
    saved = value;
    byId("result").textContent = JSON.stringify(value, null, 2);
    byId("download").disabled = false;
  }
  async function post(path, body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), path.endsWith("push-one") ? 90000 : 25000);
    try {
      const response = await fetch(path, {
        method: "POST", credentials: "same-origin", signal: controller.signal,
        headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
        body: JSON.stringify(body)
      });
      if (!(response.headers.get("content-type") || "").includes("application/json")) {
        throw new Error("Serwer nie zwrócił JSON. Sprawdź sesję i dostępność aplikacji.");
      }
      return {status: response.status, data: await response.json()};
    } finally { clearTimeout(timer); }
  }
  async function run(action) {
    if (busy) return;
    busy = true; stop = false;
    ["connection", "orders", "single", "all", "push", "push-all"].forEach(id => { byId(id).disabled = true; });
    byId("stop").disabled = !["all", "push-all"].includes(action);
    byId("status").textContent = "Sprawdzam…";
    try {
      if (action === "push") {
        const row = lastSingle?.rows?.[0];
        if (!safeToPush(row)) throw new Error("Najpierw sprawdź SKU; wariant musi mieć jednoznaczny, niezarezerwowany stan w głównej lokalizacji.");
        const result = await pushRow(row);
        show(result.data);
        byId("status").textContent = result.status === 200 ? "Stan zapisany i zweryfikowany." : "Wysyłka nie została potwierdzona; sprawdź raport i ponów odczyt.";
        lastSingle = null;
      } else if (action === "connection" || action === "orders" || action === "single") {
        const request = action === "single" ? {sku: byId("sku").value} : {};
        const path = action === "connection" ? "test-connection" : action === "orders" ? "probe-orders" : "dry-run";
        const result = await post("/api/admin/orderchamp/" + path, request);
        show(result.data);
        lastSingle = action === "single" && result.status === 200 ? result.data : null;
        byId("status").textContent = result.status === 200 ? "Odczyt zakończony." : "Odczyt zgłosił problem — szczegóły poniżej.";
      } else {
        const pushing = action === "push-all";
        const report = {mode: pushing ? "paged_stock_push" : "paged_dry_run", complete: false,
          pages: [], summary: {local_sku: 0, matched: 0, missing: 0, errors: 0, synchronized: 0, skipped: 0}};
        let offset = 0, version;
        do {
          const body = {offset, limit: 1};
          if (version) body.catalog_version = version;
          const {status, data} = await post("/api/admin/orderchamp/dry-run", body);
          if (!data.pagination) {
            report.error = data;
            show(report);
            throw new Error("Przerwano odczyt katalogu. Szczegóły w raporcie; można rozpocząć ponownie.");
          }
          report.pages.push(data);
          for (const key of ["local_sku", "matched", "missing", "errors"])
            report.summary[key] += data.summary?.[key] ?? (key === "errors" && status >= 400 ? 1 : 0);
          if (pushing) {
            const row = data.rows?.[0];
            if (safeToPush(row)) {
              const result = await pushRow(row);
              data.push_result = result.data;
              if (result.status === 200) report.summary.synchronized += Number(result.data.wrote);
              else {
                report.summary.errors += 1;
                if (["MUTATION_OUTCOME_UNKNOWN", "STOCK_SET_NOT_VERIFIED", "AUTH_OR_SCOPE_ERROR"]
                    .includes(result.data.error_code)) stop = true;
              }
            } else report.summary.skipped += 1;
          }
          version = data.pagination.catalog_version;
          offset = data.pagination.next_offset;
          report.complete = !data.pagination.has_more;
          show(report);
          byId("status").textContent = `${pushing ? "Przetworzono" : "Sprawdzono"} ${report.pages.length} z ${data.pagination.total_sku} SKU.`;
        } while (!report.complete && !stop);
        byId("status").textContent += report.complete ? " Raport ukończony." : " Zatrzymano; raport jest częściowy.";
      }
    } catch (error) {
      byId("status").textContent = error.name === "AbortError" ?
        "Przekroczono czas oczekiwania. Spróbuj ponownie; zachowano dotychczasowy raport." : error.message;
    } finally {
      busy = false;
      ["connection", "orders", "single", "all", "push-all"].forEach(id => { byId(id).disabled = false; });
      byId("push").disabled = !safeToPush(lastSingle?.rows?.[0]);
      byId("stop").disabled = true;
    }
  }
  function safeToPush(row) {
    const remote = row?.remote, levels = remote?.levels;
    return row?.status === "MATCHED" && remote?.levels_complete === true &&
      remote?.inventory_policy === "DENY" && levels?.length === 1 &&
      levels[0].is_primary === true && levels[0].quantity === levels[0].available_quantity &&
      remote.inventory_quantity === levels[0].quantity &&
      Number.isInteger(row.would_send) && row.would_send <= levels[0].quantity &&
      typeof levels[0].updated_at === "string";
  }
  function pushRow(row) {
    return post("/api/admin/orderchamp/push-one", {sku: row.sku,
      expected_local: row.would_send,
      expected_remote_updated_at: row.remote.levels[0].updated_at});
  }
  for (const action of ["connection", "orders", "single", "all", "push", "push-all"])
    byId(action).addEventListener("click", () => run(action));
  byId("stop").addEventListener("click", () => { stop = true; byId("stop").disabled = true; });
  byId("download").addEventListener("click", () => {
    if (!saved) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(saved, null, 2)], {type: "application/json"}));
    const link = document.createElement("a");
    link.href = url; link.download = "orderchamp-dry-run.json"; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
})();
