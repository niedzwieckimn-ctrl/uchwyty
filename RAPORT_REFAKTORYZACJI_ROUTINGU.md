# Raport refaktoryzacji routingu

## Zakres i wynik

Zmiana jest refaktoryzacją strukturalną. Nie zmieniano reguł biznesowych, adresów URL,
metod HTTP, nazw endpointów, schematu bazy ani istniejących statusów. Z `app.py`
wydzielono 85 funkcji routingu do siedmiu modułów domenowych rejestrowanych po
inicjalizacji infrastruktury aplikacji.

| Metryka | Przed | Po |
|---|---:|---:|
| Rozmiar `app.py` | 584 738 B | 281 291 B |
| Linie `app.py` | 12 842 | 6 322 |
| Redukcja rozmiaru | - | 51,9% |
| Reguły URL | 89 | 89 |
| Endpointy Flask | 89 | 89 |

## Nowe moduły routingu

- `routes/orders.py` — zamówienia i operacje na zamówieniach (23 trasy)
- `routes/customers.py` — klienci, wyszukania i klient API (9 tras)
- `routes/invoices.py` — faktury i KSeF (21 tras)
- `routes/inventory.py` — magazyn, dostawy, produkty i analityka (9 tras)
- `routes/shipping.py` — wysyłki, pakowanie i InPost (6 tras)
- `routes/china.py` — dostawy z Chin (9 tras)
- `routes/admin.py` — logowanie, pulpit, ustawienia i administracja (8 tras)

`app.py` pozostaje miejscem inicjalizacji Flask, konfiguracji, middleware,
infrastruktury, wspólnych helperów i rejestracji modułów. Skrypt
`tools_split_routes.py` dokumentuje i umożliwia powtórzenie mechanicznego podziału.

## Integracje

- Routing InPost przeniesiono do `routes/shipping.py`; komunikacja z API pozostaje w
  istniejącym `inpost_module.py`. Testy nie tworzą rzeczywistych przesyłek.
- Routing faktur i KSeF przeniesiono do `routes/invoices.py`. Zachowano wcześniejsze
  moduły rozdzielające faktury krajowe/zagraniczne i KSeF krajowy/zagraniczny.
- Integracje e-mail, Supabase, płatności i analityka zachowały dotychczasowe moduły
  oraz kontrakty wywołań.
- Zachowano historyczne podmienienie endpointu `/searches` na
  `client_searches_v2`; kolejność rejestracji jest jawnie zabezpieczona.

## Weryfikacja

Pełny zestaw: **54 testy zaliczone**. Testy obejmują m.in. faktury, KSeF, waluty,
zamówienia klienta, InPost, analitykę magazynową, wydanie magazynowe oraz kontrolę,
że odczyt ekranów pulpit/klienci/zamówienia/magazyn/faktury/KSeF/Chiny nie modyfikuje
żadnej tabeli. Test strukturalny potwierdza 89 reguł i kluczowe nazwy endpointów.

Archiwum źródłowe nie zawiera produkcyjnej bazy danych, dlatego nie było możliwe
wykonanie porównania rekord po rekordach na danych produkcyjnych. Na izolowanej bazie
regresyjnej migawka wszystkich tabel przed i po przejściu ścieżek tylko do odczytu
jest identyczna; zachowane są zamówienie, pozycja, klient, stan 17 szt., faktura,
alokacja faktury, partia pakowania i historia śledzenia.

## Pozostały zakres i ryzyka

W `app.py` nadal znajdują się współdzielone helpery domenowe oraz część dużych
szablonów HTML. Ich dalsze wydzielanie powinno być osobnym etapem, ponieważ wymaga
rozpinania zależności, a nie mechanicznego przeniesienia tras. Nie wykonywano testów
na produkcyjnych kluczach KSeF, InPost, SMTP ani Supabase; testy integracji korzystają
z atrap i nie powodują skutków zewnętrznych.
