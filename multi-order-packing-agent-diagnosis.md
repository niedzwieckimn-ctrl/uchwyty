# Agent multi-order packing — diagnoza i minimalna poprawka

## Wynik

Agent może teraz użyć tego samego wielozamówieniowego flow co ekran `/orders/<id>/packing-list`:

1. odczytuje propozycję przez `orders.packing_list.preview`;
2. otrzymuje numery zamówień, `order_item_id`, SKU, dostępne ilości i sumę sztuk;
3. pokazuje propozycję użytkownikowi;
4. tworzy jedną decyzję HUMAN approval dla `orders.packing_list.generate`;
5. po zatwierdzeniu istniejący service tworzy jeden `packing_batch` i jeden PDF obejmujący pozycje z wielu zamówień.

Nie utworzono `shipment.merge`, nie połączono rekordów zamówień i nie zmieniono faktur. Zmian nie wdrażano.

## A. Jak działa obecny UI multi-order packing

`GET /orders/<id>/packing-list` uruchamia `order_packing_list_download_admin_service`.

Service:

- pobiera zamówienie bazowe;
- normalizuje jego `customer_email`;
- dobiera pozostałe kwalifikujące się zamówienia tego samego adresata;
- pobiera ich `order_items` i bieżący `stock`;
- sumuje wcześniejsze realizacje według dokładnego `order_item_id` z `invoice_allocations`;
- oblicza:

```text
remaining_to_ship = max(0, ordered_qty - already_shipped_qty)
available_to_package = min(remaining_to_ship, available_stock_pool)
```

- używa wspólnej puli `product_id`, więc jeden stan magazynowy nie jest przydzielany kilka razy;
- pokazuje każdą pozycję z kolumnami `ZAMÓWIONO`, `DOSTĘPNE DO PACZKI`, `PAKUJ`;
- po POST zapisuje jeden `packing_batch` i wiele `packing_allocations`, z których każda zawiera własne `order_id` i `order_item_id`;
- generuje jeden PDF z pozycjami pochodzącymi z wielu zamówień;
- oznacza wybrane zamówienia tym samym momentem pakowania, co pozwala istniejącemu fulfillment lifecycle traktować je jako jedną paczkę.

Do kwalifikujących się zamówień dodano także `partially_shipped`, ponieważ ich pozostałe pozycje są bezpiecznie ograniczane przez `invoice_allocations`.

## B. Funkcje i backend routes

- route: `GET/POST /orders/<int:order_id>/packing-list`;
- wspólny service UI/BO: `routes/shipping.py::order_packing_list_download_admin_service`;
- historia realizacji per pozycja: `app.py::invoiced_qty_by_order_item_ids`;
- zapis jednego zakresu pakowania: `app.py::save_packing_selection`;
- jeden dokument: `app.py::generate_invoice_packing_list_pdf`;
- wspólny status pakowania: `app.py::mark_orders_packed`;
- odtworzenie członków paczki: `app.py::_packed_package_orders`;
- istniejący write BO: `orders.packing_list.generate`;
- nowy read BO nad tym samym service: `orders.packing_list.preview`.

Structured GET service zwraca teraz:

- `candidate_order_ids`;
- `order_ids` faktycznie wchodzące do propozycji;
- `items` z numerem zamówienia, SKU, ilością zamówioną, wcześniej wysłaną, dostępną i proponowaną;
- `total_quantity`.

Read BO `orders.packing_list.preview` dodaje do tego:

- `approval_items` z konkretnymi `order_id`, `order_item_id`, SKU i ilością;
- fingerprint całego zakresu.

## C. Dlaczego agent tego nie widział

Write tool `orders.packing_list.generate` już istniał i wywoływał poprawny service UI, ale przygotowywał formularz wyłącznie z `s['items']` głównego `order_id`:

```python
pack_qty_<root_order_item_id> = root_order_item.qty
```

Service znajdował inne zamówienia klienta, lecz dla ich `pack_qty_*` formularz nie zawierał wartości. POST interpretował je jako `0`, więc wspólna lista obejmowała tylko zamówienie bazowe.

Dodatkowo BO:

- nie miał operacji pokazującej wielozamówieniową propozycję;
- wymagał pełnej gotowości jednego zamówienia zamiast pakowania bieżącej dostępnej części;
- miał input ograniczony do jednego `order_id`, bez fingerprintu i konkretnych pozycji approval;
- opisy narzędzi nie wskazywały, że intencja „jedna paczka / jedna lista pakowa” jest obsługiwana przez istniejący packing flow.

W efekcie model błędnie zinterpretował zadanie jako scalanie istniejących przesyłek.

## D. Czy brakowało toola, czy tylko routingu/schema

Write tool istniał. Brakowało:

- read toola do propozycji identycznej z UI;
- routingu intencji multi-order do tego podglądu;
- schemy wiążącej approval z wieloma `order_id` i pozycjami;
- przekazania wszystkich proponowanych `pack_qty_*` do istniejącego service.

Model danych już obsługiwał wiele zamówień: `packing_allocations` przechowuje osobne `order_id` i `order_item_id` w jednym `batch_id`.

## E. Minimalna zmiana

1. Structured GET w istniejącym `order_packing_list_download_admin_service` zwraca propozycję zamiast HTML.
2. `orders.packing_list.preview` wystawia ten podgląd jako GREEN READ z permission `packing.read`.
3. `orders.packing_list.generate` nadal jest istniejącym YELLOW WRITE z permission `packing.prepare` i HUMAN approval.
4. Dla wielu zamówień write wymaga dokładnego:
   - `packing_scope_fingerprint`;
   - `packing_items`;
   - `total_quantity`.
5. Backend porównuje te dane ze świeżym podglądem przed utworzeniem approval i ponownie przed wykonaniem.
6. Po zatwierdzeniu formularz POST jest zbudowany ze wszystkich pozycji propozycji, a dokument jest rejestrowany dla każdego wybranego zamówienia.
7. Blokady obejmują wszystkie wybrane `order_id`; pozostały zachowane RBAC, audit, idempotency, approval i version checks.

Nie zmieniono semantyki stock, numerów zamówień, faktur ani integracji przewoźnika.

## F. Zmienione pliki

- `routes/shipping.py`
- `fulfillment_operations.py`
- `test_multi_order_packing_agent.py`
- `test_fulfillment_status_hotfix.py`
- `test_fulfillment_hardening.py`

Istniejący `test_packing_list_autofill.py` pozostaje testem obliczania ilości wcześniej wysłanej i `available_to_package`.

## G. Testy

Fixture produkcyjna zawiera:

- Artystyczną Manufakturę z trzema zamówieniami;
- pozycję częściowo wysłaną (`5 - 2 = 3`);
- pozycję z dostępnością `0`;
- pozycję z dostępnością `2`;
- zamówienie innego klienta z tym samym produktem.

Zweryfikowano:

- preview zawiera tylko dwa kwalifikujące się zamówienia i łącznie 5 sztuk;
- pozycja z dostępnością `0` jest pominięta;
- zamówienie innego klienta jest pominięte;
- wcześniejsze 2 sztuki są odejmowane per `order_item_id`;
- agent wykonuje preview, pokazuje numery zamówień, SKU, ilości i sumę, a potem tworzy jedną decyzję approval;
- przed approval nie powstaje PDF;
- po approval powstaje jeden batch, jeden PDF i dwie alokacje z różnymi `order_id`;
- approval payload zawiera konkretne pozycje i sumę;
- audit zawiera sukces powiązany z approval;
- powtórzenie z tym samym idempotency key nie tworzy drugiego batcha ani PDF;
- brak zakresu preview lub zmiana dostępności powoduje `PACKING_SCOPE_CONFLICT` przed wykonaniem.

Wyniki:

- dedykowane testy multi-order: **6 passed**;
- packing/stock/shipping wraz z autofill: **23 passed**;
- pozostałe testy orkiestratora fulfillment: **26 passed**;
- fulfillment hardening: **21 passed**, 1 pominięty znany błąd równoległej numeracji faktur;
- Business Operations i Agent Runtime: **78 passed**, 1 istniejący błąd nieaktualnej listy rejestru, odtworzony na nietkniętym archiwum `(37)`;
- `py_compile`: bez błędów.

Zmiana nie wymaga migracji bazy danych.
