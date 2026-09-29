# Raport Orderchamp — etap dry run na Render, 2026-09-29

## Status

**Gotowe lokalnie: izolowany odczyt API, porównanie SKU i przygotowanie dry run
do uruchomienia w istniejącej usłudze Render.**

**Nieukończone: automatyczny PUSH, minimalne odwzorowanie sprzedaży oraz
rzeczywisty dry run na koncie.** Nie wykonano wdrożenia ani zapisu do Orderchamp.
Użytkownik wybrał przygotowanie dry run na Render; lokalnie nie ma tokenu
Orderchamp ani aktualnej bazy produkcyjnej. Poniższe wyniki lokalne nie są
wynikami weryfikacji produkcji.

## 1. Dokładne źródło DOSTĘPNE

`routes/inventory.py:stock()` wywołuje
`inventory_analytics.build_replenishment_analysis(conn, ...)`. Adapter wywołuje
tę samą funkcję i pobiera `available_qty`. Nie odejmuje samodzielnie rezerwacji,
nie korzysta z innego widoku ani nie dodaje `incoming_qty` / `available_incoming`.
SQLite jest otwierane przez URI `mode=ro`, `PRAGMA query_only=ON`, `BEGIN`.
Po odczycie połączenie jest zamykane przed komunikacją z Orderchamp.

## 2. Mapowanie SKU

Dokładne `products.sku` → `productVariantBySku(sku: ...)` → `id` wariantu.
Identyfikator jest zapisany tylko w raporcie, nie wymaga tabeli mapowań.
Brak wariantu to `MISSING`; nie tworzymy produktu. Duplikaty lokalnych SKU,
puste SKU, zewnętrzne spacje i odpowiedź z innym SKU są błędami. Pozostałe
produkty są nadal sprawdzane. Produkty archiwalne pomija centralna funkcja.

## 3. API stocku

Oficjalne API GraphQL używa POST i Bearer tokenu.
[`inventoryLevelBulkAdjust`](https://developers.orderchamp.com/manage-inventory)
obsługuje SKU, lokalizację i `SET`/`ADJUST`. SET ustawia ilość, ADJUST zmienia ją
względnie. To potwierdzenie istnienia mechanizmu zapisu, a nie jego bezpieczeństwa
przy równoczesnej sprzedaży. Mutacji nie włączono w tym etapie.

## 4. Wykorzystywane query/mutation

- `StockConnectionTest`: `productVariants(first: 1) { nodes { id sku } }`.
- `StockDryRunVariant`: `productVariantBySku(sku: $sku)` z `id`, `sku`,
  `inventoryQuantity`, `inventoryPolicy` oraz `inventoryLevels` (`quantity`,
  `availableQuantity`, `updatedAt`, identyfikator i `isPrimary` lokalizacji,
  `pageInfo.hasNextPage`).
- **Mutacje: żadne.** Transport akceptuje tylko dwie stałe kwerendy READ.

Źródła kontraktu: [SKU](https://developers.orderchamp.com/queries/productVariantBySku),
[wariant](https://developers.orderchamp.com/types/ProductVariant),
[poziom zapasu](https://developers.orderchamp.com/types/InventoryLevel).

## 5. Czy potrzebny jest odczyt sprzedaży i dlaczego

Do diagnostycznego dry run — nie. Do automatycznej synchronizacji przy aktywnej
sprzedaży potrzebne jest odzwierciedlenie jej skutków w lokalnym magazynie lub
potwierdzony inny mechanizm. Bez tego lokalna aplikacja nie wie o zużyciu zapasu
przez Orderchamp. Obecna analiza nie wykazała kompletnego bezpiecznego rozwiązania
opartego wyłącznie na cyklicznym `SET`.

## 6. Czego nie można jeszcze potwierdzić

Nie potwierdzamy, że import sprzedaży jest zbędny. Nie zaimplementowano go
w dostawie dry run. Nie implementowano pełnych zamówień na zapas.

## 7. Minimalny potrzebny zakres kolejnego etapu

Jeżeli nie ma już innego kanału przekazującego sprzedaż do lokalnych zamówień,
potrzebne będzie minimum: zewnętrzny order ID, SKU, ilość, status, update timestamp;
unikalność order ID, obsługa zmian/anulowania i istniejący mechanizm rezerwacji.
Przed implementacją trzeba potwierdzić statusy rezerwujące zapas oraz przejście
rezerwacja → wydanie. `CANCELLATION_REQUESTED` nie wolno traktować jak anulowania.
Nie dodano tabel ani sztucznych zamówień bez ustalonego kontraktu.

## 8. Dry run

`dry_run_stock_sync()` odczytuje lokalną dostępność, testuje odczyt produktów,
sprawdza każde SKU i generuje JSON. `--sku` ogranicza raport do jednego produktu.
`would_send` to nieujemna lokalna dostępność; raport jawnie oznacza tę wartość
jako kandydata, nie zatwierdzony parametr Inventory SET. Niepełne poziomy zapasu,
brak jednoznacznego głównego magazynu i backordery są oznaczone ostrzeżeniami.

## 9. Full sync

Pełny **odczyt/dry run** działa dla wszystkich lokalnych aktywnych produktów.
Pełny **WRITE** nie jest zaimplementowany. Nie ma pozornego przycisku,
harmonogramu ani funkcji udającej udaną synchronizację. `synchronized=0`.
Wywołanie jest izolowane od aplikacji: żaden request magazynu nie czeka na API.

## 10. Ochrona przed przywróceniem sprzedanego stanu

W tej paczce jest zapewniona przez brak ścieżki zapisu. Nie oznacza to, że została
już rozwiązana ochrona przyszłego PUSH. Orderchamp
[opisuje Inventory jako stan obejmujący rezerwacje](https://orderchamp.zendesk.com/hc/en-150/articles/24743478544273-How-does-my-inventory-management-work).
`quantity` i `availableQuantity` mogą się różnić. Wysłanie lokalnej dostępności
do niewłaściwego pola grozi podwójnym odjęciem rezerwacji albo przywróceniem
sprzedaży. Sam schemat „odczytaj zamówienia, potem SET” nie eliminuje wyścigu
ze sprzedażą pomiędzy tymi operacjami. W udokumentowanym wejściu mutacji nie
potwierdzono atomowego warunku starej wartości.

## 11. Awarie

Maksymalnie 3 próby odczytu, timeout połączenia 5 s i odczytu 20 s, sekwencyjne
wywołania maksymalnie 2/s. Ograniczony backoff dla timeout, sieci, 5xx i throttlingu.
HTTP 429 uwzględnia `Retry-After`; żądanie oczekiwania ponad 60 s zatrzymuje kolejne
wywołania w tym przebiegu. 401/403 nie są powtarzane dla każdego SKU. Inne błędy
pozycji są raportowane oddzielnie. Przekierowania HTTP są wyłączone, dozwolony
jest wyłącznie oficjalny endpoint. Logi zawierają lokalne kody błędów, bez treści
zewnętrznych wyjątków, nagłówków i tokenu. Awaria adaptera nie zatrzymuje aplikacji.

## 12. ENV i Render

`ORDERCHAMP_API_TOKEN`, opcjonalne `ORDERCHAMP_API_URL`; użycie istniejącego
`APP_DATA_DIR`. Szczegółowe komendy: `ORDERCHAMP_RENDER_DRY_RUN.md`.
Uruchomienie w Shell istniejącej usługi, która ma bieżącą bazę. Osobny Cron Job
nie współdzieli jej dysku. **Nie potrzeba żadnego SQL w Supabase.**

## 13. Nowe pliki aplikacji — 7

1. `orderchamp_client.py`
2. `orderchamp_stock_sync.py`
3. `run_orderchamp_stock.py`
4. `test_orderchamp_stock.py`
5. `docs/ORDERCHAMP_ANALIZA.md`
6. `docs/ORDERCHAMP_RENDER_DRY_RUN.md`
7. `docs/ORDERCHAMP_RAPORT.md`

Manifest dostawy i sumy kontrolne są osobnymi plikami wydania.

## 14. Zmienione/usunięte dotychczasowe pliki

**0 zmienionych, 0 usuniętych** względem wskazanej paczki po poprawkach InPost.
Brak zmian magazynu, zamówień, rezerwacji, remanentu, InPost, agenta, UI i SQL.
Adapter można wycofać przez usunięcie wyłącznie nowych plików i ENV Orderchamp.

## 15. Testy

**56 passed, 0 failed**: 45 testów/przypadków Orderchamp oraz 11 istniejących
testów centralnego silnika `inventory_analytics`. Testy HTTP są mockowane,
prawdziwa sieć jest blokowana w testach adaptera. Bazy są tymczasowymi SQLite.

Zakres: dokładne SKU, dodatnie/zerowe/ujemne dostępne, brak wariantu, duplikat,
przekroczenie zakresu/niepoprawna ilość, timeout, błędy HTTP/GraphQL, partial data,
429 i Retry-After, 5xx, auth, kontynuacja po błędzie SKU, połączenie read-only,
zgodność z centralnym wynikiem (alokacje, rezerwacje, towar w drodze, archiwum),
niezmieniona baza, brak mutacji, ochrona tokenu i raportu, pusta/błędna baza,
jedno SKU, lokalizacje oraz backordery.

Odtworzenie lokalnie, z zainstalowanym pytest i requests, w katalogu aplikacji:

```bash
python -m pytest -q test_orderchamp_stock.py test_inventory_analytics.py
```

Nie jest to potwierdzenie zgodności z odpowiedziami rzeczywistego konta Orderchamp.
Nie uruchamiano ponownie wszystkich testów całej aplikacji; jej istniejące pliki
pozostały identyczne bajtowo z bazą.

## 16. Wynik dry run

| Środowisko | Local SKU | Matched | Missing | Errors | Zapisane |
|---|---:|---:|---:|---:|---:|
| Lokalny scenariusz z atrapą API | 4 | 2 | 1 | 1 celowo wywołany | 0 |
| Rzeczywisty Render / Orderchamp | jeszcze nie uruchomiono | — | — | — | 0 |

Raport demonstracyjny jest oznaczony `MOCK_API_SYNTHETIC_SQLITE`. Nie zawiera
rzeczywistych stanów firmy. Do właściwego wyniku potrzebne jest uruchomienie
komend z instrukcji na Render po dodaniu tokenu w Environment.

## 17. Pozostawione poza dostawą

- Zapis stanów i harmonogram: **niedokończone wymagania**, zależne od powyższych
  ustaleń, a nie deklarowana gotowa integracja.
- Minimalne odwzorowanie sprzedaży: oczekuje na kontrakt stanów/rezerwacji.
- Celowo poza zakresem: zdjęcia, ceny, opisy, nazwy, tworzenie/usuwanie produktów,
  pełne zamówienia, klienci, faktury, wysyłki, tracking, rozbudowane UI.
- Produkcja: brak zmian i brak potwierdzenia rzeczywistego mapowania SKU.
