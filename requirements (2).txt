# Wdrożenie bezpiecznego tworzenia zamówień

## Zmienne Rendera

Ustaw w `Environment` bez wpisywania wartości do repozytorium:

- `SUPABASE_URL` – adres projektu Supabase.
- `SUPABASE_SERVICE_ROLE_KEY` – nowy klucz service role, wyłącznie na Renderze.
- `RESEND_API_KEY` – klucz Resend.
- `EMAIL_FROM` – zweryfikowany nadawca Resend.
- `ADMIN_EMAIL` – adres kopii administracyjnej.
- `EMAIL_ENABLED=1`.
- `CLIENT_ALLOWED_ORIGINS` – dozwolone originy panelu rozdzielone przecinkami, np. produkcyjna domena Netlify i ewentualna domena testowa. Bez końcowego ukośnika.
- `ADMIN_ACTION_TOKEN` – losowy, długi sekret wymagany przez zbiorczy endpoint ponawiania maili.

Brak `SUPABASE_SERVICE_ROLE_KEY` wyłącza operacje wymagające Supabase i zapisuje błąd konfiguracji w logach, bez ujawniania sekretu.

## Kolejność wdrożenia

1. Natychmiast unieważnij dotychczasowy ujawniony klucz `service_role` w panelu Supabase i wygeneruj nowy.
2. Ustaw nowy klucz wyłącznie jako sekret `SUPABASE_SERVICE_ROLE_KEY` na Renderze.
3. Uruchom `supabase_order_idempotency.sql` w Supabase SQL Editor.
4. Wdróż backend na Renderze i sprawdź `/email-test`.
5. Wykonaj test zamówienia na osobnym koncie testowym i produkcie testowym; ustaw tymczasowo `EMAIL_FROM` oraz `ADMIN_EMAIL` na kontrolowane skrzynki. Nie używaj kont klientów.
6. Wdróż `index.html` panelu na Netlify.
7. Sprawdź w narzędziach przeglądarki, że kliknięcie wysyła jeden `POST /api/client/orders` z `Authorization` i `Idempotency-Key`.

Nie umieszczaj `service_role` w Netlify ani w kodzie panelu. Panel potrzebuje tylko dotychczasowego publicznego klucza anon Supabase.

## Ponawianie maili

Ręcznie użyj przycisku w szczegółach zamówienia. Zbiorcze ponowienie nieudanych zdarzeń udostępnia `POST /email/order-confirmations/retry-failed` z nagłówkiem `X-Admin-Token` równym `ADMIN_ACTION_TOKEN`. Można wywoływać go okresowo z zaufanego monitora/cron.

## Rollback danych

Implementacja używa rollbacku kompensacyjnego: po błędzie pozycji usuwa z Supabase pozycje danego zamówienia, a następnie zamówienie. Chroni to przed typowymi błędami, ale awaria sieci podczas samego rollbacku może wymagać ręcznego usunięcia rekordu oznaczonego numerem `TEMP`. Pełną atomowość zapewniłaby w przyszłości funkcja PostgreSQL RPC.

## Wycofanie wdrożenia

1. Przywróć poprzedni deploy panelu w Netlify.
2. Przywróć poprzedni deploy backendu w Renderze. Stary `/api/client_order_email` pozostał kompatybilny.
3. Kolumnę `idempotency_key` można zostawić – nie wpływa na starszą aplikację. Nie usuwaj jej w trakcie obsługi aktywnych requestów.
