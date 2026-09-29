# Korekta Orderchamp — Render Free / HTTP, 2026-09-29

Poprzednia instrukcja nie uwzględniała braku Shell na Free. Dodano adapter HTTP
i małą stronę diagnostyki. **Nie dodano żadnego WRITE do Orderchamp.**
Klient, serwis dry run i CLI pozostają identyczne bajtowo z poprzednią dostawą.

## 1–2. Endpointy i plik

W `orderchamp_http.py`:

| Metoda i adres | Funkcja |
|---|---|
| `POST /api/admin/orderchamp/test-connection` | Istniejące `OrderchampClient.test_connection()`, wynik JSON |
| `POST /api/admin/orderchamp/dry-run` | Istniejące `dry_run_stock_sync()` dla jawnego SKU, wynik JSON |
| `GET /admin/orderchamp` | Strona administracyjna z przyciskami i raportem |

W `app.py` dopisano tylko import i rejestrację tras z callbackiem do istniejącego
`DB_PATH`. Nie można wybrać bazy parametrem HTTP.

## 3. Zabezpieczenia

- Istniejąca globalna bramka `security_gate()` aplikacji pozostaje aktywna.
- Adapter wymaga podpisanej sesji Flask z `admin_authenticated`.
- Istniejące `internal_rbac.current_actor_context()` musi zwrócić aktywnego
  aktora `HUMAN`, rolę `OWNER` i `system.jobs=ALLOW`.
- Klient panelu B2B, role pracowników, AI i aktorzy systemowi nie mają dostępu.
  Rola/actor ID z body, query i nagłówków nie przyznają uprawnień.
- POST wymaga `X-CSRF-Token` z istniejącej sesji logowania oraz zgodnego Origin,
  jeżeli został przesłany. Nie powstało nowe logowanie ani osobny klucz admina.
- Ścisła lista parametrów, body do 2048 bajtów, odpowiedzi `Cache-Control: no-store`.
- Token jest redagowany przed serializacją JSON. Nie są zwracane ENV, nagłówki,
  surowe wyjątki, tracebacki ani ścieżka lokalnej bazy.
- W jednym procesie może trwać jedna diagnostyka. Kolejna dostaje 409
  `DIAGNOSTIC_BUSY`. To nie jest globalny limit między procesami Gunicorn.

## 4–5. Bez Shell, test połączenia i CH010-AB-N28

Po wdrożeniu i dodaniu `ORDERCHAMP_API_TOKEN` z `products_read` zaloguj się swoim
kontem administratora i otwórz **https://uchwyty.onrender.com/admin/orderchamp**.
Kliknij **Test połączenia**, następnie **Sprawdź SKU** przy `CH010-AB-N28`.
Wyniki są widoczne bezpośrednio na stronie. Przycisk **Pobierz JSON** generuje
plik w przeglądarce. Backend HTTP nie zapisuje raportu w `/tmp` ani w bazie.

## 6. Przykładowe requesty

Normalnie wystarczają przyciski. Poniższy przykład można wykonać w konsoli
przeglądarki na **stronie `/admin/orderchamp`**, po zalogowaniu:

```javascript
async function orderchampRead(path, body) {
  const r = await fetch('/api/admin/orderchamp/' + path, {
    method: 'POST', credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content
    },
    body: JSON.stringify(body)
  });
  return {status: r.status, data: await r.json()};
}
console.log(await orderchampRead('test-connection', {}));
console.log(await orderchampRead('dry-run', {sku: 'CH010-AB-N28'}));
```

CSRF nie jest tokenem Orderchamp. API token pozostaje w Render Environment.
Otwarcie adresu POST w pasku przeglądarki daje 405; użyj strony diagnostyki.

## 7. Pełny katalog i użycie z panelu

Tak: nowa strona działa w istniejącej sesji administratora, bez zmian menu.
**Sprawdź cały katalog** jawnie uruchamia serię odczytów, nie synchronizację.
Początek przez API:

```json
{"offset": 0, "limit": 1}
```

Puste `{}` oznacza pierwszą stronę, nigdy wszystkie 500 SKU w jednym requestcie.
`limit` musi wynosić **1**. Odpowiedź zawiera `pagination`: `total_sku`,
`next_offset`, `has_more`, `catalog_version`. Następna strona wymaga otrzymanego
`next_offset` i `catalog_version`. Zmiana listy produktów/SKU daje 409
`CATALOG_CHANGED_RESTART`. Zmiana samego stanu nie przerywa przechodzenia stron.
Nie jest to wspólny snapshot całego katalogu. `total_sku` liczy różne SKU;
`summary.local_sku` istniejącego serwisu liczy rekordy, więc duplikaty mogą
powodować różnicę. Duplikaty nadal są błędami istniejącego serwisu.

### Czas żądania

Nie ma synchronicznego odczytu 500 wariantów. HTTP sprawdza jedno SKU;
transport ma timeouty do 2 s połączenia / 3 s odczytu oraz budżet 12 s sprawdzany
przed/po wywołaniach i przed backoff. Długie Retry-After kończy się kontrolowanym
błędem, zamiast blokować request. Nie zmienia to timeoutów ani działania CLI.

Budżet nie zabija wątku: DNS/system oraz lokalne obliczenia mogą zwiększyć
całkowity czas. Przeglądarka przerywa oczekiwanie po 25 s i zachowuje wcześniejszy
raport. Nie dodano trwałych zadań, tabel, workera ani schedulera. Na produkcji
pozostaje sprawdzenie czasu odpowiedzi i dostępności lokalnych danych po cold start.

## 8. Testy i ograniczenia weryfikacji

**111 testów Python zaliczonych, 0 błędów:** 42 HTTP, 45 dotychczasowego klienta
i serwisu, 11 centralnej dostępności, 13 istniejącego RBAC. Testy HTTP używają
rzeczywistej aplikacji Flask, sesji i RBAC oraz tymczasowej bazy SQLite.
Orderchamp jest mockowany, sieć zewnętrzna blokowana.

**5 testów JavaScript zaliczonych:** sesja/CSRF, pojedyncze SKU, paginacja,
częściowy raport po błędzie katalogu, ręczne zatrzymanie. Test raportu sprawdza
również zawartość generowanego Blob do pobrania JSON.

W lokalnej przeglądarce wykonano: logowanie, test połączenia, dry run
CH010-AB-N28 (dostępne 24), cały syntetyczny katalog (3 SKU, 2 matched,
1 missing, 0 errors, 0 zapisów). Poprawiono wykryte podwójne liczenie missing
jako errors. Oczekiwanie narzędzia przeglądarkowego na zdarzenie pobrania pliku
przekroczyło limit; pobrania przez to narzędzie nie potwierdzono.

Testy potwierdzają brak mutacji, niezmienioną bazę magazynową, brak plików
raportów backendu, kontrolowany brak SKU, odmowę dostępu niewłaściwym rolom,
CSRF/Origin, brak tokenu w wynikach/logach i przerwanie długiego Retry-After.

```bash
python -m pytest -q test_orderchamp_http.py test_orderchamp_stock.py test_inventory_analytics.py test_internal_rbac.py
node --test test_orderchamp_diagnostics.mjs
```

Nie wdrażano aplikacji ani nie używano produkcyjnego tokenu. Lokalny wynik
nie potwierdza aktualnej konfiguracji i opóźnień Render/Orderchamp.

## 9. Wszystkie zmiany tej korekty

Zmienione względem paczki Orderchamp CLI:

- `app.py` — import i rejestracja;
- `docs/ORDERCHAMP_RENDER_DRY_RUN.md` — instrukcja bez Shell;
- `docs/ORDERCHAMP_ANALIZA.md` — oznaczenie wcześniejszego etapu;
- `docs/ORDERCHAMP_RAPORT.md` — odsyłacz do korekty.

Nowe:

- `orderchamp_http.py`;
- `templates/orderchamp_diagnostics.html`;
- `static/orderchamp_diagnostics.js`;
- `test_orderchamp_http.py`;
- `test_orderchamp_diagnostics.mjs`;
- `docs/ORDERCHAMP_HTTP_RENDER_FREE.md`.

Nie zmieniono `orderchamp_client.py`, `orderchamp_stock_sync.py`,
`run_orderchamp_stock.py`, logiki magazynu, zamówień, remanentu, InPost, KSeF,
17track, agenta ani pozostałych endpointów. **Nie potrzeba SQL ani products_write.**

Ograniczenia planu potwierdza [dokumentacja Render Free](https://render.com/docs/free#other-limitations).
