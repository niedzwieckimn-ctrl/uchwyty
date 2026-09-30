"use strict";
(() => {
  const el=id=>document.getElementById(id), csrf=document.querySelector('meta[name="csrf-token"]').content;
  const labels={orders:"Pobieranie zmian zamówień",catalog:"Porównanie dostępności",write:"Wysyłanie zmian",idle:"Zakończono"};
  const errors={REMOTE_SKU_NOT_FOUND:"Brak wariantu w Orderchamp",REMOTE_STOCK_SCOPE_UNSAFE:"Sprawdź lokalizację i zasady sprzedaży wariantu",ORDERS_NOT_YET_RECONCILED:"Nowa zmiana zamówienia — ponowna próba w kolejnym cyklu",MUTATION_OUTCOME_UNKNOWN:"Nieznany wynik zapisu. Wysyłka zatrzymana; wymaga sprawdzenia przed wznowieniem.",SYNC_DATABASE_UNAVAILABLE:"Brak połączenia z bazą. Synchronizacja czeka przed kolejną próbą.",ORDER_ALREADY_IN_FULFILLMENT:"Zmiana zamówienia dotyczy rozpoczętej realizacji — wymaga sprawdzenia."};
  Object.assign(errors,{LOCAL_SKU_NOT_FOUND:'Nie znaleziono aktywnego SKU w magazynie.',PAUSE_BEFORE_SKU_TEST:'Najpierw wstrzymaj automat, potem wykonaj test jednego SKU.',JOB_RUNNING:'Zadanie już trwa. Poczekaj na jego zakończenie.',LOCAL_ORDER_CONFLICT_REVIEW:'Zamówienie anulowano lokalnie, ale nadal jest aktywne w Orderchamp. Uzgodnij anulowanie.',LOCAL_SKU_REMOVED_REVIEW:'SKU usunięto lub zarchiwizowano lokalnie. Sprawdź ofertę Orderchamp.',OVERCOMMITTED_STOCK_REVIEW:'Rezerwacje przekraczają stan. Zwiększenie stanu wstrzymano.',ORDERS_CHANGED_DURING_CATALOG:'Podczas porównania pojawiła się zmiana zamówienia. Automat ponowi odczyt.',AUTH_OR_SCOPE_ERROR:'Sprawdź uprawnienia tokenu Orderchamp.',TOKEN_MISSING_OR_INVALID:'Brakuje prawidłowej konfiguracji tokenu Orderchamp.'});
  let timer, loading=false, lastState=null, pending=false;
  errors.LOCAL_IMPORT_CONFLICT='Lokalna faktura korzysta już ze zmienianych pozycji. Import zatrzymano do uzgodnienia; lokalnych alokacji nie usunięto.';
  Object.assign(errors,{ORDER_SKU_NOT_FOUND:'Zakup zawiera SKU, którego nie ma w aktywnym magazynie. Uzgodnij produkt przed dalszą synchronizacją.',TEST_ORDER_REQUIRES_REVIEW:'Wykryto zamówienie testowe. Sprawdź je przed importem do magazynu.',CURRENCY_NOT_SUPPORTED:'Zamówienie ma walutę nieobsługiwaną przez obecny magazyn.',INVALID_ORDER:'Dane zakupu są niekompletne. Import i wysyłka czekają na wyjaśnienie.',INVALID_ORDER_PRICE:'Brakuje poprawnych kwot zakupu.',BILLING_DATA_MISSING:'W zamówieniu brakuje danych adresowych.',SYNC_FAILED:'Nie udało się zakończyć cyklu. Automat zrobi przerwę przed następną próbą.'});
  function updateButtons(){
    for(const button of document.querySelectorAll('[data-action]'))button.disabled=pending||!lastState?.configured||(button.dataset.action==='reconcile'?!lastState.review_required:(lastState.review_required&&button.dataset.action!=='pause'))||(button.dataset.action==='compare'&&lastState.running);
  }
  async function refresh(){
    if(loading)return; loading=true; clearTimeout(timer);
    try {
      const response=await fetch('/api/admin/orderchamp/status',{credentials:'same-origin',signal:AbortSignal.timeout(12000)});
      if(!response.ok)throw new Error('Nie można pobrać stanu zadania. Sprawdź sesję i połączenie.');
      const state=await response.json(), report=state.report||{};
      lastState=state;
      el('enabled').textContent=state.enabled?'Włączony':'Wyłączony';
      el('phase').textContent=labels[state.phase]||'Oczekuje';
      el('completed').textContent=report.completed_at?new Date(report.completed_at).toLocaleString('pl-PL'):'—';
      el('changed').textContent=report.changed_sku??0;el('imported').textContent=report.imported_orders??0;
      el('message').textContent=state.error?(errors[state.error]||`Synchronizacja zatrzymana: ${state.error}`):state.running?'Zadanie działa na serwerze.':state.configured?'Gotowe.':'Oczekuje na konfigurację.';
      el('message').className=state.error?'error':'';
      if(report.pending_orders?.length)el('message').textContent+=` Zamówienia do sprawdzenia: ${report.pending_orders.join(', ')}.`;
      el('issues').replaceChildren();
      for(const row of report.skipped||[]){const tr=document.createElement('tr');for(const value of [row.sku,errors[row.reason]||row.reason]){const td=document.createElement('td');td.textContent=value;tr.append(td);}el('issues').append(tr);}
      const seconds=report.seconds||{};
      el('timing').textContent=seconds.total==null?'':`Ostatni cykl: ${seconds.total} s. Odczyt dostępności: ${seconds.availability??'—'} s. Żądania Orderchamp: ${report.orderchamp_requests??'—'}. Zakres: ${report.only_sku||'wszystkie SKU'}.`;
      updateButtons();
      // Poll the small job record, never start a stock scan from the browser.
      if(state.configured)timer=setTimeout(refresh,state.running?10000:60000);
    }catch(error){el('message').textContent=error.message;timer=setTimeout(refresh,60000);}
    finally{loading=false;}
  }
  for(const button of document.querySelectorAll('[data-action]'))button.addEventListener('click',async()=>{
    pending=true;updateButtons();
    const body={action:button.dataset.action};if(body.action==='queue')body.sku=el('only-sku').value.trim()||null;
    try{const r=await fetch('/api/admin/orderchamp/control',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body),signal:AbortSignal.timeout(12000)});if(!r.ok){const info=await r.json();throw new Error(info.error==='WAIT_BEFORE_RECONCILIATION'?'Od niepotwierdzonego zapisu musi minąć 5 minut. Potem zostaną odczytane aktualne zamówienia i stany.':(errors[info.error]||'Nie uruchomiono operacji. Sprawdź stan zadania.'));}await refresh();}
    catch(error){el('message').textContent=error.message;}finally{pending=false;updateButtons();}
  });
  el('refresh').addEventListener('click',refresh);refresh();
})();
