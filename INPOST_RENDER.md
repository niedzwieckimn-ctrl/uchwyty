# Integracja InPost ShipX

## 1. Supabase

Przed wdrożeniem aplikacji uruchom w Supabase SQL Editor zawartość pliku
`supabase_inpost_migration.sql`.

## 2. Render

W usłudze backendu dodaj w `Environment`:

- `INPOST_ORGANIZATION_ID` — ID organizacji z Managera Paczek,
- `INPOST_API_TOKEN` — token ShipX,
- opcjonalnie `INPOST_SANDBOX=1` wyłącznie do testów w sandboxie.

Nie zapisuj tokenu w repozytorium ani w pliku aplikacji.

## 3. Użycie

1. Otwórz zamówienie i użyj `Pakuj`.
2. Wybierz `InPost` albo `Inny przewoźnik`.
3. Dla InPost przejdź przez parametry paczki i utwórz przesyłkę.
4. Wybierz serwis, liczbę paczek, wymiary w cm i wagę w kg.
5. Opcjonalnie ustaw ochronę, pobranie, SMS, e-mail, zwrot dokumentów lub sobotę.
6. Potwierdź utworzenie przesyłki.
7. Zostanie pobrany jeden PDF: lista pakowa A4, a po niej etykieta InPost A6.
8. Po fizycznym nadaniu kliknij `Wysłane`.

## Zamówienie kuriera

Po utworzeniu etykiet otwórz `Zamów kuriera InPost`, zaznacz gotowe
przesyłki, sprawdź adres magazynu i utwórz jedno zlecenie odbioru. Etykiety
i zlecenie odbioru są oddzielnymi operacjami. Przesyłka już przypisana do
zlecenia nie pojawi się ponownie na liście oczekujących.

Zamówienia spakowane razem dla tego samego adresata otrzymują jedną przesyłkę.
Ponowne wejście pobiera istniejącą etykietę i nie tworzy kolejnej przesyłki.
