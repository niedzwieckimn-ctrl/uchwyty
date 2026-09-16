# Packing list auto-fill — diagnoza i minimalna poprawka

## Wynik

Naprawiono obliczenie wartości `PAKUJ` w `GET /orders/<order_id>/packing-list` oraz ograniczenie tej samej ilości w `POST`. Poprawka ponownie wykorzystuje istniejące, trwałe przypisanie zrealizowanych ilości do `order_item_id` w tabeli `invoice_allocations`.

Nie wdrażano zmian. Nie zmieniono danych, schematu bazy ani semantyki magazynu.

## A. Root cause

Backend trasy packing-list pobierał bieżący stan magazynu i pilnował wspólnej puli `product_id`, ale obliczał maksimum jako:

```python
min(ordered_qty, available_stock)
```

Nie pobierał istniejącej sumy realizacji z `invoice_allocations`, więc przy ponownym otwarciu listy pakowej wcześniejsza realizacja tej samej pozycji zamówienia nie była odejmowana.

Template działał zgodnie z danymi backendu: zarówno `max`, jak i `value` inputu `PAKUJ` pochodziły z `item.max_pack_qty`. Na tej stronie nie ma JavaScriptu, który nadpisywałby wartość.

## B. Gdzie wcześniej była ta logika

Istniejący mechanizm jest w:

- `app.py::invoiced_qty_by_order_item_ids` — sumuje `invoice_allocations.qty` według dokładnego `order_item_id`;
- `routes/invoices.py` — oblicza `remaining_qty = max(0, ordered_qty - done_qty)` przed podpowiedzią ilości faktury;
- `app.py::_order_packing_list_email_attachment` — dla częściowej realizacji odtwarza pozostałą ilość na podstawie `invoice_allocations`;
- `app.py::reconcile_legacy_shipped_order_statuses` — jawnie określa `invoice_allocations` jako źródło prawdy o zrealizowanych ilościach.

`packing_allocations` nie jest właściwym źródłem wartości „już wysłano”: przechowuje wybór paczki oczekujący na fakturę. Liczenie go razem z `invoice_allocations` podwajałoby partie już skonsumowane przez fakturę, a otwarte partie mogłyby reprezentować niedokończony wybór. Historia przewoźnika jest na poziomie zamówienia/przesyłki i nie zapewnia dokładnego przypisania ilości do `order_item_id`.

## C. Gdzie logika została przerwana

Porównanie lokalnych archiwów pokazuje:

- archiwa `(1)`–`(7)`: istnieje mechanizm `remaining_qty` oparty na `invoice_allocations`, ale nie ma nowej trasy packing-list;
- archiwum `(8)` z 27.08.2026: pojawia się `order_packing_list_download_admin`, lecz używa od początku `min(order_item.qty, stock)` bez odjęcia realizacji;
- archiwum `(10)` z 03.09.2026: pojawia się obecny ekran „Wybierz zawartość paczki” z kolumnami `ZAMÓWIONO`, `DOSTĘPNE DO PACZKI`, `PAKUJ`, nadal oparty na niepełnym wzorze;
- archiwa `(10)`–`(37)`: błąd pozostaje w tej samej postaci.

Regresja powstała więc przy wydzieleniu nowego flow packing-list: trasa ominęła wcześniej istniejące obliczenie pozostałej ilości z flow faktury. Template i frontend nie odcięły poprawnej wartości; backend nigdy nie przekazał jej do nowego ekranu.

## D. Dokładny wzór przed i po

### Przed

```text
available = max(0, current_stock_in_shared_product_pool)
max_pack_qty = min(max(0, ordered_qty), available)
PAKUJ_GET = max_pack_qty
PAKUJ_POST = min(max_pack_qty, max(0, submitted_qty))
```

### Po

```text
already_shipped_qty = max(
    0,
    SUM(invoice_allocations.qty WHERE order_item_id = current_order_item.id)
)
remaining_to_ship = max(0, max(0, ordered_qty) - already_shipped_qty)
packable_now = min(remaining_to_ship, available)
PAKUJ_GET = packable_now
PAKUJ_POST = min(packable_now, max(0, submitted_qty))
```

Wspólna pula bieżącego stanu dla `product_id` została zachowana. Dzięki temu ten sam stan magazynowy nie może zostać przydzielony dwa razy, gdy ten sam produkt występuje w kilku pozycjach lub zamówieniach.

## E. Zmienione pliki

- `routes/shipping.py` — pobranie sum z istniejącego `invoiced_qty_by_order_item_ids`, wyliczenie `already_shipped_qty`, `remaining_to_ship` i `packable_now`, ustawienie `max_pack_qty` oraz wartości inputu na `packable_now`.
- `test_packing_list_autofill.py` — 10 testów regresyjnych wymaganych dla backendu, POST i HTML.

## F. Testy

Dedykowane scenariusze:

1. brak wcześniejszej wysyłki;
2. częściowa wcześniejsza wysyłka;
3. całkowicie wysłana pozycja;
4. dostępność mniejsza niż pozostała ilość;
5. dostępność większa niż pozostała ilość;
6. kilka wcześniejszych realizacji tej samej pozycji;
7. dwa zamówienia z tym samym SKU — historia nie przecieka między `order_item_id`;
8. nadmiarowa historia nie daje wartości poniżej zera;
9. POST nie zapisuje więcej niż pozostało do realizacji;
10. `value` i `max` inputu `PAKUJ` są dokładnie równe backendowemu `packable_now`.

Wyniki:

- `test_packing_list_autofill.py`: **10 passed**;
- istniejące testy packing/stock/shipping: **7 passed**;
- istotne testy orkiestratora listy pakowej: **3 passed**;
- `py_compile routes/shipping.py test_packing_list_autofill.py`: zakończone poprawnie.

Szerszy przebieg ujawnił trzy niezależne błędy: dwa w numeracji faktur i jeden w konfiguracji sesji testowej. Wszystkie trzy odtworzono bez zmian na nietkniętym archiwum `(37)`, więc nie pochodzą z tej poprawki.

## G. Czy stare wysyłki są poprawnie uwzględniane

Tak — wszystkie historyczne realizacje posiadające wpisy `invoice_allocations` są sumowane per dokładny `order_item_id`, także gdy pozycja była realizowana w kilku wcześniejszych paczkach/fakturach. Historia innego zamówienia z tym samym SKU nie wpływa na wynik.

Rekordów sprzed wprowadzenia `invoice_allocations`, które nie mają żadnego przypisania ilości do pozycji zamówienia, nie da się bezpiecznie odtworzyć na podstawie samego SKU ani historii przewoźnika. Poprawka celowo nie zgaduje takich ilości. Jest to zgodne z istniejącym zachowaniem `reconcile_legacy_shipped_order_statuses`.

## H. Czy fix dotyka danych historycznych

Nie. Zmiana wyłącznie odczytuje istniejące `invoice_allocations`. Nie aktualizuje starych faktur, paczek, zamówień ani stanów magazynowych.

## I. Czy wymaga migracji

Nie. Wykorzystane tabele, indeksy i funkcja agregująca już istnieją. Nie ma zmian schematu ani backfillu.

