# Orderchamp — uruchomienie dry run na Render

**Ta paczka uruchamia test połączenia i raport porównawczy. Nie uruchamia jeszcze
automatycznej synchronizacji stanów. Nie zawiera mutacji Orderchamp.**

## 1. Dodaj pliki do aktualnej aplikacji

Baza paczki to ZIP47 po dokończeniu InPost z 2026-09-29, opisany w
`ORDERCHAMP_ANALIZA.md`. Paczka „tylko nowe pliki” nie podmienia dotychczasowych
modułów. Dodaj jej zawartość z zachowaniem katalogów do repozytorium wdrażanego
na Render. Trzy pliki wykonywalne muszą leżeć obok `app.py`:

- `orderchamp_client.py`
- `orderchamp_stock_sync.py`
- `run_orderchamp_stock.py`

Pozostałe pliki są dokumentacją i testami. Istniejące `requirements.txt` już
zawiera `requests`. Nie zmieniaj Start Command, nie instaluj osobnego serwera.
Nie potrzeba SQL ani migracji Supabase/SQLite.

## 2. ENV w istniejącej usłudze webowej Render

| Zmienna | Ustawienie |
|---|---|
| `ORDERCHAMP_API_TOKEN` | Prywatny token z Orderchamp, wprowadzony wyłącznie w backendowym Environment. Na ten etap wystarczy uprawnienie odczytu produktów `products_read`. |
| `ORDERCHAMP_API_URL` | Opcjonalna; domyślnie `https://api.orderchamp.com/v1/graphql`. Inny adres jest odrzucany. |
| `APP_DATA_DIR` | Pozostaw istniejącą wartość aplikacji. Adapter czyta z niej `app.db`. Nie wpisuj zgadywanej ścieżki i nie kopiuj bazy. |

Token nie trafia do komendy, pliku JSON, repozytorium ani frontendu. Nie jest
potrzebny `products_write`, `orders_write`, token Supabase ani klucz OpenAI.

Po wdrożeniu nowych plików i ustawień otwórz **Render → obecna usługa aplikacji →
Shell**. Bieżący katalog powinien zawierać `app.py` i `run_orderchamp_stock.py`.
Jeśli nie, przejdź do katalogu źródeł tej aplikacji.

To musi być Shell działającej usługi, która ma bazę. Nie uruchamiaj tego jako
nowy Cron Job, build command, pre-deploy ani one-off job: nie mają dostępu do
dysku tej instancji. [Dokumentacja Render](https://render.com/docs/disks#disk-limitations-and-considerations).

## 3. Test połączenia

```bash
python run_orderchamp_stock.py test-connection
```

Oczekiwany wynik:

```json
{"connected": true, "products_read": true, "write_checked": false}
```

Sprawdza rzeczywisty odczyt produktów, także jeśli katalog Orderchamp jest pusty.
Nie sprawdza uprawnień do zapisu i nie pobiera danych klientów.

## 4. Najpierw jedno SKU

```bash
python run_orderchamp_stock.py dry-run --sku CH010-AB-N28 --report /tmp/orderchamp-one-sku.json
cat /tmp/orderchamp-one-sku.json
```

SKU jest dopasowywane dokładnie, bez zgadywania, zmiany wielkości liter i
automatycznego tworzenia produktu. Jeżeli tego SKU nie ma lokalnie, wybierz
istniejące SKU z ekranu magazynu.

## 5. Pełny dry run

```bash
python run_orderchamp_stock.py dry-run --report /tmp/orderchamp-dry-run.json
cat /tmp/orderchamp-dry-run.json
```

Istniejący raport nie jest nadpisywany. Przy kolejnym uruchomieniu podaj nową
nazwę, np. `/tmp/orderchamp-dry-run-2.json`. Bez `--report` cały JSON pojawi się
bezpośrednio w Shell. `/tmp` jest tymczasowy — skopiuj raport przed restartem
lub wdrożeniem. Nie wystawiamy go jako publicznego pliku strony.

Jeżeli aplikacja korzysta z niestandardowej lokalizacji bez `APP_DATA_DIR`,
możesz jawnie podać `--db /rzeczywista/sciezka/app.db`. Błędna ścieżka powoduje
przerwanie, a nie utworzenie pustej bazy. Nie kieruj adaptera na kopię SQLite,
jeśli celem jest sprawdzenie aktualnej instancji.

## 6. Co zobaczysz

Dla każdego lokalnego produktu:

- `local_available`: wynik centralnej funkcji ekranu magazynu;
- `would_send`: ten wynik ograniczony do minimum 0 — **kandydat ilości, jeszcze
  nie zatwierdzona wartość mutacji Inventory SET**;
- `status`: `MATCHED`, `MISSING` lub `ERROR`;
- `remote.id`, `inventory_quantity`, `inventory_policy`;
- `remote.levels`: stan i dostępność każdej zwróconej lokalizacji,
  jej identyfikator, oznaczenie głównego magazynu i czas aktualizacji;
- ostrzeżenia o rezerwacjach/różnicy stanów, backorderach, braku jednoznacznej
  lokalizacji albo niepełnej liście lokalizacji.

Podsumowanie: `local_sku`, `matched`, `missing`, `errors`, `synchronized`,
`skipped`, `warning_rows`, `http_requests`. W tym etapie `synchronized` zawsze
wynosi 0, `skipped` oznacza wszystkie pozycje bez zapisu. `local_sku` liczy
wybrane rekordy aktywnych produktów; duplikaty SKU są osobnymi błędami.

Raport nie sumuje zapasu z kilku magazynów Orderchamp i nie dolicza towaru
w drodze do eksportowanej ilości. `null` z API pozostaje brakiem informacji,
a nie zerem. Brak wariantu nie tworzy produktu i nie zatrzymuje reszty odczytu.

Odczyt lokalny jest spójny na moment `local_snapshot_at`; odczyty Orderchamp są
sekwencyjne i mogą odzwierciedlać późniejsze momenty. To raport diagnostyczny,
nie atomowy obraz dwóch systemów. Adapter nie wymusza odświeżenia z Supabase.
Przed porównaniem otwórz ekran Stan magazynu w tej samej instancji; ruch
magazynowy w czasie raportu może spowodować różnice względem późniejszego ekranu.

## 7. Wynik procesu i błędy

| Kod procesu | Znaczenie |
|---|---|
| 0 | Test/raport ukończony bez błędów i ostrzeżeń danych; nie jest to zgoda na WRITE |
| 1 | Raport ukończony; są brakujące SKU lub ostrzeżenia |
| 2 | Błąd konfiguracji/API/bazy, błędy pozycji lub pusty lokalny katalog |

- `AUTH_OR_SCOPE_ERROR`: sprawdź token i uprawnienia odczytu w Environment.
- `TOKEN_MISSING_OR_INVALID`: token nie jest dostępny w tej usłudze.
- `LOCAL_DATABASE_NOT_FOUND`: nieprawidłowy katalog albo inna instancja.
- `LOCAL_AVAILABILITY_READ_FAILED`: niezgodny schemat/błąd bazy; nie wykonuj
  przypadkowych migracji. Potrzebna weryfikacja wersji aplikacji.
- `GRAPHQL_ERROR`: API odrzuciło zapytanie; brakujące SKU nie jest tym samym.
- `TIMEOUT`, `NETWORK_ERROR`, `UPSTREAM_UNAVAILABLE`, `RATE_LIMITED`: odczyt
  nieudany po najwyżej 3 próbach. Ponów cały dry run z nową nazwą raportu.
- `RATE_LIMIT_WAIT_TOO_LONG`: serwer wymaga odczekania ponad 60 s; kolejne
  odczyty w tym przebiegu są wstrzymane. Uruchom ponownie później.

Odczyty odbywają się kolejno, maksymalnie 2 próby HTTP/s; jest timeout 5 s
połączenia i 20 s odczytu. Retry dotyczy wyłącznie bezpiecznych zapytań READ.
Treści odpowiedzi błędów i token nie są drukowane.

## 8. Warunek kolejnego etapu

Przekaż wygenerowany JSON. Rzeczywiste liczniki i różnice `quantity` /
`availableQuantity` pozwolą ustalić mapowanie stanu na tym koncie.

Przed automatycznym PUSH nadal trzeba ustalić docelowe pole, odzwierciedlenie
sprzedaży Orderchamp w istniejących rezerwacjach lokalnych i zachowanie zapisu
przy równoczesnej sprzedaży. Dokumentacja nie potwierdza atomowego warunku
`SET`. Ta paczka nie udaje rozwiązania tego problemu: nie udostępnia WRITE,
przełącznika aktywującego WRITE ani automatycznego harmonogramu.
