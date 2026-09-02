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
2. Kliknij `Generuj etykietę InPost`.
3. Podaj wymiary w mm i wagę w kg.
4. Potwierdź utworzenie przesyłki.
5. PDF A6 zostanie pobrany, a numer śledzenia zapisany w zamówieniu.
6. Po fizycznym nadaniu kliknij `Wysłane`.

Zamówienia spakowane razem dla tego samego adresata otrzymują jedną przesyłkę.
Ponowne wejście pobiera istniejącą etykietę i nie tworzy kolejnej przesyłki.
