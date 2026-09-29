# Orderchamp — dry run na Render Free, bez Shell

Instrukcja poprawiona 2026-09-29. Poprzednia wersja wymagała Render Shell,
którego plan Free nie udostępnia. Aktualna wersja działa przez zalogowany panel.

## Wdrożenie

1. Dodaj pliki z paczki do repozytorium aplikacji, zachowując katalogi.
2. W Render Environment dodaj `ORDERCHAMP_API_TOKEN` z prawem `products_read`.
   Jeżeli token już jest ustawiony, pozostaw go. Nie wklejaj go do przeglądarki,
   komendy fetch ani rozmowy. Opcjonalne `ORDERCHAMP_API_URL` pozostaje
   `https://api.orderchamp.com/v1/graphql`.
3. Wdróż aktualizację zwykłą drogą dla swojej aplikacji. Nie zmieniaj Start
   Command, `APP_DATA_DIR`, bazy, ustawień InPost ani konfiguracji Supabase.
4. Nie potrzeba SQL, migracji, dodatkowego workera ani Cron Job.

Paczka skumulowana zawiera również niezmienione pliki Orderchamp z poprzedniego
etapu, jeżeli nie zostały jeszcze dodane do repozytorium.

## Uruchomienie z przeglądarki

1. Zaloguj się do aplikacji swoim dotychczasowym kontem administratora.
2. Otwórz ekran Stan magazynu, aby sprawdzić aktualność lokalnych danych.
   Adapter korzysta z tej samej bazy i nie uruchamia własnego odświeżania Supabase.
3. Otwórz **https://uchwyty.onrender.com/admin/orderchamp**.
4. Kliknij **Test połączenia**.
5. Pozostaw w polu SKU `CH010-AB-N28` i kliknij **Sprawdź SKU**.
6. Wynik JSON otrzymasz na stronie. **Pobierz JSON** generuje plik po stronie
   przeglądarki; backend HTTP nie zapisuje raportu w `/tmp` ani w bazie.

**Sprawdź cały katalog** rozpoczyna serię odczytów po jednym SKU. Przycisk
**Zatrzymaj po tej pozycji** zachowuje częściowy raport (`complete: false`).
Zamknięcie strony zatrzymuje dalsze żądania. Nie jest to synchronizacja stanów
ani harmonogram. Nie dodano nowych pozycji do dotychczasowego menu.

## Wyniki

- Połączenie: `connected: true`, `products_read: true`, `write_checked: false`.
- Dry run: `rows` zawiera `local_available`, `would_send`, `status`, `remote`
  oraz `warnings`; `writes_enabled` zawsze wynosi `false`.
- `would_send` to kandydat ilości z dotychczasowego serwisu, nie wykonany zapis.
- `MISSING` / HTTP 404: brak SKU w Orderchamp; pełny odczyt przechodzi dalej.
- `LOCAL_SKU_NOT_FOUND` / HTTP 404: brak SKU lokalnie.
- HTTP 401/403: zaloguj się ponownie kontem administratora i odśwież stronę.
- `TOKEN_MISSING_OR_INVALID` / `AUTH_OR_SCOPE_ERROR`: sprawdź ENV i uprawnienia.
- `HTTP_TIME_BUDGET_EXCEEDED` / HTTP 504: ponów odczyt. Niczego nie zapisano.
- `CATALOG_CHANGED_RESTART` / HTTP 409: lista SKU zmieniła się; zacznij raport od nowa.
- `DIAGNOSTIC_BUSY` / HTTP 409: w tym procesie trwa już diagnostyka.

Pełny raport zawiera kolejne momenty odczytu, nie wspólny snapshot dwóch systemów.
Render Free ma nietrwały system plików: adapter korzysta z dotychczasowego sposobu
odtwarzania danych aplikacji, bez przebudowy magazynu.

## CLI pozostaje dostępne opcjonalnie

Na środowisku z terminalem nadal działają niezmienione komendy:

```bash
python run_orderchamp_stock.py test-connection
python run_orderchamp_stock.py dry-run --sku CH010-AB-N28
```

Endpointy, przykłady requestów, testy i lista plików: `ORDERCHAMP_HTTP_RENDER_FREE.md`.
Ograniczenie Shell potwierdza [dokumentacja Render Free](https://render.com/docs/free#other-limitations).
