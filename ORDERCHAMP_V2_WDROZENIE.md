# Orderchamp v2 — instrukcja wdrożenia i odbioru

## Status i baza

Przygotowano lokalnie 30.09.2026. Nie wdrożono na Render ani w produkcyjnym Supabase.

Baza: **cb818964dfae2ac5f494c9a0be831ce0d67f5077**, ostatnia wersja z Orderchamp sprzed rollbacku.
Paczka zmian jest przeznaczona do tej wersji. Nie nakładać jej bezpośrednio na rollback `f651a4c`.
Kod pozostałych poprawek pochodzi z cb81896. Konfiguracja Gunicorn nie jest częścią zmiany.

## Działanie

- Serwer co 60 sekund po zakończeniu poprzedniego cyklu odczytuje nowe/zmienione zamówienia i jeden spójny wynik dostępności.
- Dostępne = maksymalnie 0 lub stan fizyczny minus niezrealizowane rezerwacje, według reguł ekranu magazynu.
- Wysyłane są tylko różnice ilości, paczkami do 20 istniejących wariantów. Zera są obsługiwane.
- Co 15 minut następuje porównanie z Orderchamp; brakujący lub niebezpieczny wariant jest pomijany i pokazywany w panelu.
- Zakup tworzy zwykłe zamówienie/rezerwację z opisem `Orderchamp — numer`. Identyfikator zakupu chroni przed ponownym utworzeniem.
- Anulowanie przed rozpoczęciem realizacji zwalnia rezerwację. Zmiana ilości/anulowanie po fakturowaniu lub rozpoczęciu realizacji zatrzymuje cykl do wyjaśnienia.
- Nieznany wynik zapisu nie jest ponawiany. Panel pozwala po 5 minutach zlecić świeże uzgodnienie; poprzednia mutacja nie jest odtwarzana.
- Zamknięcie karty nie zatrzymuje pracy. Postęp i blokada zadania są trwałe w Supabase.

Czas 60 sekund jest interwałem pracy, nie gwarancją dotarcia zmiany do klienta. Dochodzą czas API, ewentualny zimny start oraz przerwa 5 minut po błędzie. W razie niedostępności bazy integracja przerywa cykl; nie wpisuje zer w miejsce błędów. Synchronizacja asynchroniczna nie stanowi atomowej rezerwacji między dwoma niezależnymi kanałami sprzedaży.

## Co zmienia SQL

`sql/orderchamp_sync_v2.sql` tworzy trzy nowe tabele:

1. `orderchamp_sync_job` — pojedyncze zadanie, ostatnie potwierdzone ilości, blokada i postęp.
2. `orderchamp_order_links` — zewnętrzny identyfikator, lokalne zamówienie i dane źródłowe zakupu.
3. `orderchamp_sync_events` — historia importu, zamiaru zapisu i potwierdzeń/błędów.

Dodaje trzy funkcje RPC: odczyt dostępności, sterowanie zadaniem i import zamówień. Dostęp ma backend `service_role`; `anon` i `authenticated` są wyłączone. Instalacja nie zmienia ilości w istniejących tabelach i nie włącza automatu.

**Po uruchomieniu synchronizacji** import zapisuje klientów, zamówienia i pozycje; aktualizuje wyłącznie powiązane importy. Nie zmniejsza stanu fizycznego i nie tworzy faktur, płatności ani wiadomości. Kopia SQLite otrzymuje tylko te konkretne rekordy.

SQL dopasowano do typów w wcześniej przesłanym lokalnym eksporcie schematu: `products.archived` jest boolean, daty istniejących zamówień są tekstem, ceny pozycji mają numeric(12,2). Eksport nie zastępuje potwierdzenia aktualnego schematu przed instalacją.

## Kolejność wdrożenia — wymaga zgody na produkcyjne zmiany

1. Zachować identyfikator działającego wdrożenia i konfigurację środowiska. Przygotować commit na bazie cb81896 z tą paczką.
2. Wykonać `sql/orderchamp_sync_preflight_readonly.sql` i sprawdzić zgodność kolumn, ograniczeń oraz sekwencji. Jest to tylko odczyt metadanych; nie eksportuje klientów ani zamówień.
3. Po akceptacji wykonać `sql/orderchamp_sync_v2.sql`. Trzy nowe tabele pozostają z wyłączonym automatem. Błąd instalacji wycofuje transakcję.
4. Wdrożyć kod z `ORDERCHAMP_SYNC_V2_ENABLED=0`. Zachować działające `GUNICORN_CMD_ARGS=--access-logfile -` i inne ustawienia Gunicorn. Nie zmieniać komendy startowej ani liczby workerów.
5. Sprawdzić logowanie, ekran magazynu i `/admin/orderchamp` w stanie wyłączonym.
6. Skonfigurować istniejący backendowy `ORDERCHAMP_API_TOKEN` z prawami odczytu zamówień, odczytu produktów i aktualizacji stanów. Token pozostaje wyłącznie na serwerze. Następnie ustawić `ORDERCHAMP_SYNC_V2_ENABLED=1`. Samo ustawienie tej flagi nadal nie włącza zadania zapisanego w bazie.
7. W panelu przy wyłączonym automacie wpisać **jeden uzgodniony SKU** i nacisnąć „Synchronizuj teraz”. Zakres ogranicza zapis stanów; import zamówień nadal musi być kompletny, aby poprawnie rozliczyć rezerwacje.
8. Sprawdzić ilość dostępną tego SKU w obu aplikacjach oraz raport zadania. Następnie zatwierdzić uruchomienie reszty przyciskiem „Włącz automat dla wszystkich SKU”.
9. Przed uznaniem integracji za odebraną sprawdzić kontrolowany zakup, jego rezerwację, powtórne powiadomienie i anulowanie bez podwójnych zapisów. Sprawdzić podgląd danych faktury i walutę. Testy lokalne nie zastępują tych czynności na rzeczywistym koncie.

Starsze endpointy zapisu po jednym SKU nie są rejestrowane. Nie uruchamiać starych helperów `push_one_stock` równolegle z v2. Stare flagi inicjalizacji nie sterują nowym mechanizmem.

## Render Free — niezależność od otwartego panelu

Pracownik działa w obsługującym procesie, startuje leniwie po żądaniu; blokada w bazie dopuszcza tylko jedno zadanie. Bez ruchu uśpiony Render nie zapewni cyklu minutowego.

Przygotowano opcjonalny `sql/orderchamp_sync_wake_optional.sql` dla istniejących rozszerzeń pg_cron, pg_net i Vault. Instalacja tworzy **wyłączone** wybudzanie co minutę, bez instalowania rozszerzeń i bez zmiany stanu magazynu.

- Wygenerować osobny losowy sekret minimum 32 znaków. Ustawić go w Render jako `ORDERCHAMP_SYNC_TRIGGER_TOKEN` i w Vault jako `orderchamp_sync_trigger_token`.
- Nie używać do tego tokenu Orderchamp ani klucza service_role i nie wklejać wartości do raportów.
- Cron wywołuje tylko `/api/internal/orderchamp/tick`; odpowiedź budzi pracownika i nie skanuje tabel w żądaniu HTTP.
- Po sprawdzeniu endpointu osobno włączyć przygotowane zadanie cron. Bez tej konfiguracji nie deklarować stałej pracy na uśpionym Render Free.

Webhook `/webhooks/orderchamp` jest opcjonalnym szybszym sygnałem. Wymaga zweryfikowanej konfiguracji Orderchamp App: `ORDERCHAMP_WEBHOOK_SECRET` oraz `ORDERCHAMP_ACCOUNT_ID`. Weryfikuje HMAC surowego body i konto. Nie zakładać, że zwykły prywatny token API jest sekretem podpisu. Polling zmian działa również bez webhooka i odzyskuje zgubione powiadomienia.

## Faktury i granice tego etapu

Zachowano rzeczywistego kupującego, NIP, adresy, ilości, walutę i kwoty transakcyjne w danych importu. Formularz wymaga sprawdzenia dokumentów Orderchamp i jawnego wyboru istniejącego rodzaju faktury. EUR nie wybiera automatycznie WDT dla importu. Wybrany rodzaj jest zachowany przy edycji i regeneracji.

To nadal ręczne wystawianie faktury w aplikacji. Nie rozstrzygnięto rozrachunków, prowizji i modelu fakturowania konkretnego konta Orderchamp. Opłaty dostawy, prowizje i wypłaty nie są automatycznie dodawane do dokumentu. Nie łączyć importu z innymi zamówieniami po wspólnym e-mailu.

Ceny jednostkowe wymagające więcej niż dwóch miejsc po przecinku są przechowywane dokładnie w źródłowym payloadzie, ale wymagają uzgodnienia przed fakturą — istniejące kolumny cen zaokrąglają do dwóch miejsc. Dla takiego przypadku zapis faktury jest jawnie blokowany, aby nie wystawić dokumentu z cicho zmienioną kwotą. Import/rezerwacja nadal działają. Ten przypadek wymaga osobnego rozszerzenia dotychczasowego silnika faktur, jeśli wystąpi na realnym zamówieniu.

## Wyłączenie

W panelu „Wstrzymaj po bieżącej paczce” zatrzymuje kolejne zapisy. W celu wyłączenia całej integracji ustawić `ORDERCHAMP_SYNC_V2_ENABLED=0` i wyłączyć dedykowane wybudzanie cron. Zachować nowe tabele oraz zaimportowane zamówienia — usunięcie mapowania mogłoby dopuścić duplikaty. Nie cofać danych magazynowych ani wystawionych dokumentów.

## Testy lokalne

Runtime aplikacji nie wymaga nowych pakietów. Do testów potrzebne są pytest, Flask, requests, pglast i graphql-core. Test SQL używa lokalnego PostgreSQL WASM `@electric-sql/pglite@0.5.8`; nie łączy się z Supabase.

```text
python -m pytest -q test_orderchamp_sync_v2.py test_orderchamp_stock.py test_inventory_analytics.py
node test_orderchamp_sql.mjs
node --check static/orderchamp_sync.js
```

PGlite można udostępnić przez lokalną instalację pakietu albo zmienną `ORDERCHAMP_PGLITE_MODULE` zawierającą lokalny URL file do jego modułu. Testy używają danych syntetycznych. Nie importują startowego `app.py` i nie uruchamiają produkcyjnych workerów.
