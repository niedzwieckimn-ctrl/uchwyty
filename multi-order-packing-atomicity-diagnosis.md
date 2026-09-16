# Multi-order packing — diagnoza partial commit i minimalna poprawka

## A. Dokładna kolejność zapisów przed poprawką

Po zatwierdzeniu `orders.packing_list.generate` wykonywał się następujący ciąg:

1. Business Operation blokowała zamówienia, sprawdzała wersję i konsumowała approval.
2. Audit fazy `started` był commitowany osobno.
3. `order_packing_list_download_admin_service()` wyliczał wspólny zakres paczki.
4. `save_packing_selection()` tworzył `packing_batches` i `packing_allocations`, po czym natychmiast robił `commit`.
5. Generator tworzył PDF na filesystemie.
6. `mark_orders_packed()` aktualizował `orders.status` i `packed_at`, po czym robił drugi niezależny `commit`.
7. Service zwracał `path`, `batch_id` i `order_ids` do Business Operation.
8. `save_document()` zapisywał osobny rekord `fulfillment_documents` dla każdego zamówienia; każdy rekord miał własny `commit`.
9. Metadane były publikowane przez `reconciliation_store.publish()` osobno dla członków paczki.
10. Finalny audit, status wykonania Business Operation i wynik dla narzędzia były zapisywane później, w kolejnych transakcjach.

W modelu nie ma tabeli `packing_batch_items`. Jej rolę pełni `packing_allocations`.

## B. Co zdążyło się zapisać na produkcji

Zmiana statusu dowodzi, że:

- `save_packing_selection()` zakończył commit batcha i jego alokacji;
- generator PDF zwrócił ścieżkę bez wyjątku, ponieważ zmiana statusu następowała dopiero po jego powrocie;
- `mark_orders_packed()` zakończył commit statusów.

Z samego komunikatu UI nie da się ustalić, czy nie zapisał się żaden rekord `fulfillment_documents`, czy zapisał się tylko pierwszy z kilku. Brak listy w UI wskazuje na awarię po commicie statusów: zapis dokumentu, publikację metadanych albo finalizację wyniku. Dokładny wyjątek wymaga logu konkretnego execution/correlation ID.

## C. Root cause

Root cause to granica transakcji, a nie SSE. Istniejący service wykonywał trzy niezależne commity domenowe:

```text
COMMIT batch + allocations
generate PDF
COMMIT order statuses
COMMIT fulfillment_document dla order 1
COMMIT fulfillment_document dla order 2
...
```

Wyjątek generatora PDF zostawiał batch bez dokumentu. Wyjątek podczas pierwszego lub kolejnego `save_document()` zostawiał zmienione statusy oraz brak lub tylko część rekordów dokumentu. Komunikat o niepełnym wyniku był objawem późniejszej awarii, ale nie tworzył partial commit.

Archiwum nie zawiera historii Git, więc nie można przypisać regresji do konkretnego commita. Kod bazowy w archiwum potwierdza jednak tę kolejność bezpośrednio.

## D. Kolejność po poprawce

```text
consume approval + audit started (control plane)
prepare selection
generate PDF
verify PDF hash
BEGIN IMMEDIATE
  reuse exact legacy batch albo INSERT packing_batches
  INSERT packing_allocations
  UPDATE wszystkich orders.status + packed_at
  INSERT/REPLACE wszystkich fulfillment_documents
  INSERT audit phase=domain_committed
COMMIT
post-commit sync + retry-safe notifications
reconciliation publish
final state + final audit + operation result
```

Jeżeli PDF nie powstanie, nie rozpoczyna się transakcja domenowa. Jeżeli zapis batcha, statusów, dowolnego dokumentu albo audytu rzuci wyjątek, cała nowa transakcja jest wycofywana. Plik przygotowany przed nieudanym commitem może pozostać na filesystemie, ale nie jest podłączony do batcha ani widoczny jako dokument realizacji.

## E. Minimalna poprawka

- Service packing-list dostał tryb `defer_persistence`, który przygotowuje zakres i PDF bez wcześniejszych zapisów domenowych.
- `save_packing_selection()` i aktualizacja statusów przyjmują połączenie należące do transakcji wywołującej.
- `finalize_packing_list()` zapisuje batch, alokacje, statusy, dokumenty wszystkich orderów i audit w jednej transakcji SQLite.
- Sync i e-mail są uruchamiane dopiero po commicie.
- Retry używa istniejącego pojedynczego batcha, jeżeli `(order_id, order_item_id, qty)` jest dokładnie zgodne.
- Zakres różny albo wiele otwartych batchy zwraca błąd wymagający kontroli i nie tworzy duplikatu.
- Pełność istniejącej listy jest sprawdzana dla wszystkich orderów należących do batcha, więc częściowo zapisany `fulfillment_documents` nie blokuje naprawczego retry.
- `packed_partial` z historycznym `shipped_at` może wejść wyłącznie w operację odzyskania/generowania packing list.

## F. Retry i idempotency

- Powtórzenie zakończonego sukcesem tego samego idempotency key zwraca zapisany wynik i nie generuje drugiego PDF, batcha, statusu ani powiadomienia.
- Powtórzenie terminalnego błędu tym samym key zwraca zapisany błąd. Naprawczy retry wymaga ponownego odczytu stanu, nowego idempotency key i nowego approval.
- Pojedynczy zgodny historyczny batch jest dokańczany: dostaje brakujące dokumenty, bez nowej alokacji.
- Status już `packed/packed_partial` nie powoduje drugiego e-maila; dodatkowo istnieje rejestr `email_events`.
- Packing nie odejmuje stocku, więc retry nie może odjąć go drugi raz.
- Niezgodny lub wielokrotny otwarty batch zatrzymuje operację przed zapisem.

## G. Obecny stan produkcyjny

Przed jakąkolwiek zmianą danych trzeba odczytać:

- `orders.status`, `packed_at` dla wszystkich członków paczki;
- otwarte `packing_batches` dla root order;
- odpowiadające `packing_allocations`;
- `fulfillment_documents` dla każdego orderu i ich `document_id/path/file_hash`;
- execution, approval i audit po correlation ID.

Po zastosowaniu poprawki pojedynczy batch zgodny z aktualną propozycją może zostać dokończony przez świeże preview + nowy approval, bez ręcznej edycji DB. Brak batcha przy już zmienionym statusie również może zostać uzupełniony bez ponownego powiadomienia. Niezgodny zakres, wiele otwartych batchy albo dokument powiązany z innym batch ID wymagają ręcznej rekonsyliacji; kod celowo nie zgaduje i nie usuwa historii.

## H. Zmienione pliki

- `app.py`
- `routes/shipping.py`
- `fulfillment_operations.py`
- `test_multi_order_packing_agent.py`

## I. Testy

Dodane regresje:

1. wyjątek generatora PDF: brak nowego batcha, alokacji, statusu i dokumentu;
2. świeży retry po błędzie PDF kończy się jednym kompletem zapisów;
3. wyjątek przy zapisie drugiego dokumentu: rollback batcha, wszystkich alokacji, statusów i pierwszego dokumentu;
4. sukces + replay: jeden PDF, jeden batch, dwie alokacje i dokument dla każdego orderu;
5. zgodny historyczny partial batch jest dokańczany bez duplikatu;
6. niezgodny historyczny batch zatrzymuje retry bez drugiego batcha.

Wyniki:

- focused packing/autofill/status: `23 passed`;
- fulfillment hardening z wyłączeniem istniejącego testu numeracji faktur: `21 passed, 1 deselected`;
- fulfillment orchestrator z wyłączeniem istniejącego problemu testowego sesji Flask: `26 passed, 1 deselected`;
- business operations + agent runtime: `78 passed`, 1 istniejący błąd oczekiwanej zamkniętej listy registry;
- `py_compile`: sukces.

Nie zmieniono schematu bazy ani danych historycznych. Migracja nie jest wymagana. Nie wykonano deploymentu.
