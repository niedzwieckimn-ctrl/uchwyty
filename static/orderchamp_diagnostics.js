"use strict";
(() => {
  const byId = id => document.getElementById(id);
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  let stop = false, busy = false, saved = null;
  function show(value) {
    saved = value;
    byId("result").textContent = JSON.stringify(value, null, 2);
    byId("download").disabled = false;
  }
  async function post(path, body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 25000);
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
    ["connection", "single", "all"].forEach(id => { byId(id).disabled = true; });
    byId("stop").disabled = action !== "all";
    byId("status").textContent = "Sprawdzam…";
    try {
      if (action !== "all") {
        const request = action === "connection" ? {} : {sku: byId("sku").value};
        const path = action === "connection" ? "test-connection" : "dry-run";
        const result = await post("/api/admin/orderchamp/" + path, request);
        show(result.data);
        byId("status").textContent = result.status === 200 ? "Odczyt zakończony." : "Odczyt zgłosił problem — szczegóły poniżej.";
      } else {
        const report = {mode: "paged_dry_run", writes_enabled: false, complete: false,
          pages: [], summary: {local_sku: 0, matched: 0, missing: 0, errors: 0, synchronized: 0}};
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
          version = data.pagination.catalog_version;
          offset = data.pagination.next_offset;
          report.complete = !data.pagination.has_more;
          show(report);
          byId("status").textContent = `Sprawdzono ${report.pages.length} z ${data.pagination.total_sku} SKU.`;
        } while (!report.complete && !stop);
        byId("status").textContent += report.complete ? " Raport ukończony." : " Zatrzymano; raport jest częściowy.";
      }
    } catch (error) {
      byId("status").textContent = error.name === "AbortError" ?
        "Przekroczono czas oczekiwania. Spróbuj ponownie; zachowano dotychczasowy raport." : error.message;
    } finally {
      busy = false;
      ["connection", "single", "all"].forEach(id => { byId(id).disabled = false; });
      byId("stop").disabled = true;
    }
  }
  for (const action of ["connection", "single", "all"])
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
