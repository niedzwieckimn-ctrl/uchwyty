# Panel Chiny (P/O) i 17TRACK — raport wdrożeniowy

## Zakres

Panel `/china` otrzymał KPI, alerty operacyjne, filtry, kompaktową tabelę, rozwijane szczegóły i modal „Nowe P/O”. Odczyt panelu, filtrowanie i rozwijanie nie zmieniają danych biznesowych. Dotychczasowe adresy `/china`, `/china/<id>` i akcje edycyjne pozostały dostępne.

## Pliki

Zmienione: `app.py`, `routes/china.py`, `routes/admin.py`, `routes/orders.py`, `inventory_analytics.py`, `cash_flow_module.py`, testy regresyjne.

Nowe: `templates/china_list.html`, `seventeentrack_module.py`, `supabase_17track_migration.sql`, `test_17track.py`, ten raport.

## Konfiguracja

Przed wdrożeniem kodu uruchomić w Supabase SQL Editor plik `supabase_17track_migration.sql`. Migracja wyłącznie dodaje nullable kolumny i indeksy; nie aktualizuje istniejących rekordów.

ENV na Renderze:

- `SEVENTEENTRACK_ENABLED=1`
- `SEVENTEENTRACK_API_KEY=<klucz z panelu API 17TRACK>`
- opcjonalnie `SEVENTEENTRACK_TIMEOUT_SEC=15` (zakres w kodzie 3–30 s)

Brak klucza lub `SEVENTEENTRACK_ENABLED=0` nie blokuje panelu ani ręcznej obsługi trackingu.

## Oficjalne endpointy API v2.4

- `POST https://api.17track.net/track/v2.4/register` — idempotentna rejestracja; błąd „already registered” jest sukcesem.
- `POST https://api.17track.net/track/v2.4/gettrackinfo` — ręczne pobranie danych (maks. 40 w API; obecny przycisk wysyła jeden numer).
- klient zawiera również `POST .../push` jako kontrolowany fallback, ale widok GET go nie uruchamia.
- `POST /webhooks/17track` — odbiór pushy `TRACKING_UPDATED`.

Webhook weryfikuje nagłówek `sign` jako SHA-256 z `raw_body + '/' + API key`, waliduje JSON, limituje rozmiar do 1 MB i tempo do 300/min. Nieznane numery są tylko logowane. Adres webhooka do ustawienia w panelu 17TRACK: `https://uchwyty.onrender.com/webhooks/17track`.

## Statusy

- `InfoReceived`, `NotFound` → bez automatycznej zmiany P/O
- `InTransit`, `OutForDelivery`, `AvailableForPickup` → `shipped`
- `Delivered` → `arrived`
- `DeliveryFailure`, `Exception`, `Expired` → `problem`

Automatyka jest monotoniczna: nie cofa `shipped` do `ordered` ani `arrived` do `shipped`. Ręczna korekta pozostaje dostępna, ale przyjętego `arrived` nie można cofnąć do „w drodze”, ponieważ dublowałoby dostępność.

Zgodnie z późniejszą decyzją właściciela aplikacji przejście na `arrived` automatycznie przyjmuje zawartość na `stock`. Operacja jest idempotentna przez `warehouse_received`: ręczny status, webhook powtórzony kilka razy i kolejne synchronizacje zwiększą stan tylko raz. Historyczne rekordy `arrived` z wartością NULL są uznawane za wcześniej przyjęte i nie są ponownie księgowane. Integracja nie zmienia `warehouse_issued`, alokacji, faktur, KSeF ani pozycji zamówień.

## Odporność

API nie jest wywoływane podczas GET `/china`. Wywołania odbywają się po świadomym kliknięciu „Rejestruj”/„Odśwież” albo przez podpisany webhook. Błędy i timeout zapisują krótki komunikat logistyczny bez klucza API; panel dalej działa. Limit ręcznych akcji wynosi 40/min.

## Testy i ograniczenia

74 testy przechodzą. Pokryto brak klucza, wyłączone API, podpis webhooka, odrzucenie złego podpisu, mapowanie statusów, monotoniczność, brak zmiany stanu dla `InTransit` oraz dokładnie jedno przyjęcie dla powtarzanego `Delivered`. Test przeglądarkowy potwierdził render panelu, filtry, KPI i otwarcie modala.

Bez produkcyjnego klucza nie wykonano rzeczywistej rejestracji numeru, nie potwierdzono kształtu danych konkretnego przewoźnika ani dostarczenia webhooka przez konto użytkownika. Parser defensywnie obsługuje brakujące pola, ale po pierwszym prawdziwym numerze należy sprawdzić zapis przewoźnika, zdarzeń i ETA.

Największe ryzyko wdrożeniowe: kod z nowymi kolumnami nie powinien być uruchamiany przed migracją Supabase. Automatyczne `Delivered → arrived` ma skutek magazynowy zgodnie z decyzją właściciela, dlatego integrację należy włączyć dopiero po próbie na jednym kontrolowanym P/O.
