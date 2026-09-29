# Orderchamp — analiza przed implementacją, 2026-09-29

## Baza

Źródła: `uchwyty_ZIP47_InPost_dokonczone_2026-09-29_pelne_zrodla.zip`,
SHA256 `7943accd2a9209b13de3165e6ebc82c64c24a14acda1e8eeb17f6d6acc0058a4`.
Analiza wykonana przed dodaniem kodu Orderchamp. Zakres bieżącej dostawy:
**przygotowanie odczytu i dry run na Render; automatyczny zapis pozostaje zablokowany**.

## Rzeczywisty przepływ magazynowy

| Obszar | Źródło w aplikacji |
|---|---|
| Produkt / SKU | `products.id`, `products.sku`; archiwalne produkty pomijane |
| Stan fizyczny | `stock.qty`, powiązanie przez `product_id` |
| Ekran „Stan magazynu” | `routes/inventory.py`, funkcja `stock`, wywołuje `build_replenishment_analysis()` |
| DOSTĘPNE | `inventory_analytics.py`, wynik tej funkcji: `available_qty` |
| Rezerwacje | Centralna funkcja czyta aktywne `orders` i `order_items`, uwzględnia `warehouse_issued` oraz `invoice_allocations` |
| W drodze / zarezerwowane w drodze | Ta sama funkcja zwraca `incoming_qty`, `reserved_incoming`, `available_incoming`; nie są eksportowanym stanem |
| Przyjęcie | `routes/china.py:receive_into_stock`, idempotencja przez `china_stock_receipts` |
| Wydanie | `app.py:issue_order_stock`, operacje fakturowania / alokacji |
| Korekty | `routes/inventory.py:api_stock_delta`, `stock_correction`; `business_operations.py:_inventory_adjust` |
| Remanent | `remanent.py`, korekty przez istniejące `inventory.adjust` |
| Zmiany dostępności | Przyjęcia, wydania, korekty, pozycje/status zamówienia, anulowanie, fakturowanie i alokacje, archiwizacja oraz istniejąca synchronizacja danych z Supabase |

Adapter ma **wywołać istniejącą funkcję**, a następnie pobrać `available_qty`.
Nie powiela równania dostępności ani logiki rezerwacji. Nie używa widoku panelu
klienta zamiast źródła ekranu wewnętrznego. Nie importuje `app.py`, ponieważ import
uruchamia inicjalizację aplikacji i może mieć skutki uboczne. Odczyt SQLite jest
otwierany w trybie `mode=ro` z transakcją spójnego odczytu, zamykaną przed HTTP.
Nie odświeża samodzielnie Supabase: raport dotyczy lokalnej bazy konkretnej instancji.

## Potwierdzone API

- [Endpoint](https://developers.orderchamp.com/getting-started): POST `https://api.orderchamp.com/v1/graphql`.
- [Uwierzytelnianie](https://developers.orderchamp.com/authentication): prywatny token Bearer; do dry run odczyt produktów, bez uprawnień zapisu.
- [SKU](https://developers.orderchamp.com/queries/productVariantBySku): dokładny argument `sku: String!`, wynik nullable `ProductVariant`.
- [Test odczytu](https://developers.orderchamp.com/queries/productVariants): `first: 1`, `nodes { id sku }`.
- [Wariant](https://developers.orderchamp.com/types/ProductVariant): `inventoryQuantity`, `inventoryPolicy`, `inventoryLevels`.
- [Poziom zapasu](https://developers.orderchamp.com/types/InventoryLevel): `quantity` oraz osobne `availableQuantity`, powiązane z lokalizacją.
- [Lokalizacja](https://developers.orderchamp.com/types/Location): identyfikator oraz `isPrimary`.
- [Aktualizacja](https://developers.orderchamp.com/manage-inventory): `inventoryLevelBulkAdjust`, identyfikacja SKU lub ID wariantu, akcja `SET` zastępuje ilość, `ADJUST` oznacza zmianę względną.
- [Kontrakt wejściowy](https://developers.orderchamp.com/types/InventoryLevelBulkAdjustInventoryLevelInput) zawiera `sku`, `adjustment`, `action`, `locationId` lub identyfikatory. Nie dokumentuje warunku porównania starej wartości ani wersji.
- [Błędy/limity](https://developers.orderchamp.com/rate-limits): koszt zapytań, ograniczony zasób punktów, błąd GraphQL `Throttled`; potrzebne ograniczone ponowienia odczytów.

Odczyt poziomów korzysta z udokumentowanego pola bez dodatkowych argumentów.
Sprawdza `pageInfo.hasNextPage`: niepełny wynik zostanie oznaczony w raporcie,
a nie uznany za pełny odczyt lokalizacji.

## Sprzedaż i warunek zatrzymania automatycznego PUSH

[Pomoc Orderchamp](https://orderchamp.zendesk.com/hc/en-150/articles/24743478544273-How-does-my-inventory-management-work)
opisuje Inventory jako zapas obejmujący sztuki zarezerwowane. Po złożeniu zamówienia
sztuki są rezerwowane do wysyłki. Nie należy utożsamiać `quantity`,
`availableQuantity` i lokalnego `available_qty` bez sprawdzenia na koncie.

Przykład ryzyka: lokalne 24, rezerwacja Orderchamp 3. Po odwzorowaniu sprzedaży
lokalne DOSTĘPNE wyniesie 21. Ustawienie Inventory na 21 może jeszcze raz odjąć
rezerwację po stronie kanału. Z kolei bez odwzorowania sprzedaży lokalna aplikacja
może nadal oferować te same sztuki i później przywrócić sprzedany zapas.

**Wniosek:** przed automatycznym pełnym PUSH potrzebne jest wiarygodne
odwzorowanie sprzedaży w lokalnych rezerwacjach/wydaniach albo potwierdzony
alternatywny kontrakt integracji. Sam odczyt zamówień przed `SET` nie dowodzi
bezpieczeństwa: sprzedaż może wystąpić pomiędzy odczytem i zapisem.
Nie potwierdzono atomowego warunku zapisu ani idempotencji mutacji po timeout.

[API zamówień](https://developers.orderchamp.com/queries/orders) pozwala czytać
aktualizacje; [statusy](https://developers.orderchamp.com/types/OrderStatus) obejmują
również stany przed potwierdzeniem oraz `CANCELLATION_REQUESTED`, które nie jest
`CANCELLED`. Nie ustalono jeszcze, które stany faktycznie rezerwują zapas tego konta.
Nie wprowadzamy zgadywanej obsługi statusów ani dodatkowej logiki rezerwacji.

Dry run nie wymaga importowania sprzedaży. Automatyczna synchronizacja WRITE
i minimalny import sprzedaży pozostają następnym, niedokończonym etapem.
Nie będzie przełącznika ENV umożliwiającego ominięcie tej blokady.

## Uruchomienie na Render

Użytkownik wybrał przygotowanie dry run na Render. Uruchomienie w Shell istniejącej
usługi korzysta z tego samego `APP_DATA_DIR/app.db`. Osobny Cron Job lub one-off job
nie ma dostępu do jej dysku: [ograniczenia Render](https://render.com/docs/disks#disk-limitations-and-considerations).
Na tym etapie nie tworzymy schedulera ani publicznego endpointu.
