# Warehouse Operations

Moduł udostępnia asystentowi jawne operacje remanentu i pakowania przez istniejący Business Operations Registry, RBAC, Approval Engine, idempotency i append-only audit. Nie daje dostępu do SQL ani ogólnego zapisu.

## Operacje

- `inventory.count.get_expected` — GREEN READ; zwraca fizyczny stan `stock.qty` i wersję produktu.
- `inventory.count.record` — GREEN WRITE; zapisuje wynik fizycznego liczenia, ale nie zmienia `stock`.
- `inventory.count.summary` — GREEN READ; pokazuje pozycje zgodne, rozbieżności, korekty i nierozwiązane pozycje.
- `inventory.count.complete` — GREEN WRITE; kończy sesję tylko bez nierozwiązanych rozbieżności.
- `inventory.adjust` — YELLOW WRITE; po zatwierdzeniu ustawia `stock.qty` na zapisaną ilość policzoną. Backend wylicza różnicę i zapisuje istniejący ledger `stock_adjustments`.
- `orders.packing.check` — GREEN READ; korzysta z `calculate_fulfillment_readiness`, wspólnego z istniejącym UI.
- `orders.packing.shortage.report` — GREEN WRITE; zapisuje fizycznie potwierdzony brak, bez zmiany stanu i statusu zamówienia.
- `orders.packing.confirm` — YELLOW WRITE; wymaga potwierdzenia fizycznego i zgody człowieka, ponownie sprawdza wersję oraz kompletność w transakcji i ustawia status `packed` z `packed_at`.

Potwierdzenie pakowania nie odejmuje zapasu i nie ustawia `warehouse_issued`. Dzięki temu nie powiela istniejącego wydania magazynowego wykonywanego przy fakturze lub przez dedykowany flow wydania.

## Sesje i wersje

Migracja `migrations/warehouse_operations.sql` tworzy wewnętrzne sesje `OPEN`, `COMPLETED`, `CANCELLED`, pozycje remanentu, raporty braków, sidecar wersji zapasu oraz wiązanie execution z inicjującym człowiekiem. Migracja jest addytywna i idempotentna; nie zmienia schematu panelu klienta i nie jest migracją Supabase.

Zmiana `stock` zwiększa wersję produktu. Zapis liczenia i korekta wymagają wersji zwróconej przez `inventory.count.get_expected`. Korekta po zmianie stanu kończy się kontrolowanym konfliktem. Approval jest konsumowane w tej samej transakcji co zapis biznesowy i audit.

Po lokalnym commicie korekta synchronizuje istniejący rekord `stock`, a potwierdzenie pakowania rekord `orders`, jeżeli Supabase jest skonfigurowany. Niezależnie od wyniku synchronizacji aktualizowane są właściwe markery freshness, aby bieżący proces nie nadpisał świeżego zapisu starym snapshotem.

## Uprawnienia

`AI_OWNER_ASSISTANT` i `AI_WAREHOUSE` mogą czytać i przygotowywać operacje. `inventory.adjust` i `packing.confirm` pozostają decyzjami approval-required. Endpoint decyzji wykonuje tylko trzy jawnie dozwolone operacje YELLOW: zmianę statusu zamówienia, korektę remanentową i potwierdzenie pakowania.

Asystent może ustawić `human_confirmed=true` dla pakowania tylko wtedy, gdy człowiek rzeczywiście potwierdził fizyczne spakowanie. W przeciwnym razie powinien dopytać; backend odrzuca wartość `false`.

## Weryfikacja

Testy `test_warehouse_operations.py` obejmują odczyt oczekiwanego stanu, obserwacyjny zapis liczenia, rozbieżności, approval, idempotency, RBAC, stale version, rollback, wspólną kontrolę kompletności, raport braków, brak podwójnego wydania, konkurencyjne potwierdzenie, audit, endpoint approval i karty Rich UI.
