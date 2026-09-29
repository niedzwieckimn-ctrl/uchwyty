# Dokończenie InPost ZIP47 — 29.09.2026

## Baza i granica odbioru

Jedyna baza: `uchwyty_ZIP47_statusy_InPost_pelne_zrodla.zip`, SHA256
`05721b957e2cead4ec08d0b9676cafc5028122eafb2303e77df099a4437b734b`.
Praca w oddzielnej kopii. Wcześniejsze zmiany magazynu, remanentu, finansów
i asystenta pozostają w paczce. Manifest porównuje bajty do tej dokładnej bazy.

Nie odczytywano produkcyjnej bazy ani rekordów 131/133, nie wdrażano aplikacji,
nie wykonywano rzeczywistych przesyłek, podjazdów ani wiadomości. Wyniki poniżej
są lokalne. Nie potwierdzają konfiguracji webhooka, uprawnień Supabase,
wydajności serwera ani trwałości dysku Render.

## Odtworzenie przed zmianami

- Dostarczone `test_inpost_additional_review.py`: **6 failed, 1 passed**.
- Wybrana regresja: **71 passed Python**.
- `node --test test_voice_ui.mjs test_agent_stream_ui.mjs`: **59 passed JS**.
- Test odbiorczy skopiowano bez zmian; jego hash jest kontrolowany przy pakowaniu.

Logi i JUnit są w osobnej paczce dowodów. Nie zawierają baz testowych.

## F1 — transakcja i synchronizacja

`work.stage()` zapisuje zamówienia, wersję zamiaru wysyłki do chmury i potwierdzony
zakres razem z finalną listą/dowodami w jednej transakcji SQLite. Przerwanie po
commit nie wymaga handlera wyjątku, aby starszy pull był blokowany. Zapisany payload
zawiera shipment ID, finalny batch, pełny skład, bieżące cele, wartości i warunki
zapisu. Retry publikuje ten payload; nie buduje go na nowo z dowolnych bieżących wierszy.

Dowody pakowania nadal publikuje `packing_versions` / `reconciliation_store`.
Istniejący payment outbox zachowuje swoje powiązania z fakturami; jeżeli zawiera
ten sam status zamówienia, jego snapshot i rewizja są aktualizowane w transakcji
wysyłki. Zapis statusu przez payment outbox jest serializowany z lokalnym zapisem
InPost. Nie tworzymy fikcyjnych faktur ani odrębnej kopii zamówień w Supabase.

Przy częściowym błędzie grupy pozostaje `pending`. Idempotentne ponowienie kończy
grupę bez nowej wiadomości. Ack wymaga tej samej rewizji, payloadu, tokena i ważnej
dzierżawy. Każdy PATCH jest warunkowy względem poprzedniej/bieżącej przesyłki,
statusu i znacznika rozchodu; wymaga potwierdzonego wiersza w odpowiedzi.
Inny shipment lub niezgodny status powoduje `conflict`, bez cichego nadpisania.
Kod nie zmienia `warehouse_issued`, zapasów ani faktur.

Pojedynczy PATCH InPost trzyma lokalną blokadę zapisu na czas żądania z timeoutem
20 sekund. To zapobiega wyprzedzeniu zapisu przez nową lokalną wersję; wolna chmura
może czasowo opóźnić innych lokalnych zapisujących. Nie oznacza to transakcji
rozproszonej obejmującej całą grupę w Supabase. Częściowa grupa jest jawnie `pending`.

## F2 — zakończony transport a lokalne etapy

`terminal` zatrzymuje polling przewoźnika. Tabela `inpost_reconciliation` oddzielnie
przechowuje zweryfikowaną obserwację, rewizję, `apply_state`, czas lokalnego zapisu,
sync intent i payload powiadomienia. Automat co cykl podejmuje niedokończone
lokalne etapy z zapisanej obserwacji, zwykle po 60 sekundach, bez ponownego API.
Sprawdza wersję, dzierżawę i tożsamość bieżącego lub prawidłowego historycznego
członka. Błąd zakresu nie jest omijany przez retry.

Nieudane przygotowanie załącznika przed wysyłką pozostaje `pending` i może być
ponowione. Zarezerwowane `sending` po utracie procesu staje się `unknown` po
wygaśnięciu dzierżawy. `unknown`, `accepted`, `failed` i pominięcie przez adapter
nie powodują automatycznej ponownej wysyłki. Stare zweryfikowane stany po migracji
są włączane do uzgodnienia bez kasowania wcześniejszych potwierdzeń poczty.

## F3 — chronologia i kontrakty

Wiarygodny późniejszy czas zdarzenia ma pierwszeństwo nad umownym RANK, również
przy ponownym skanie w oddziale. Starszy czas, także dla tego samego kodu, jest
odrzucany. Sprzeczne kody z równym czasem nie nadpisują poprzedniego dowodu.
Przy braku porównywalnych czasów obowiązuje konserwatywna osłona: nie cofamy
etapu według RANK ani statusu końcowego. Nieznany kod nie uruchamia skutków
biznesowych; może zastąpić fizyczny skan tylko z jawnie nowszym czasem. Kod nadal
jest widoczny jako diagnostyka, jeśli został przyjęty. Zmiana opisu transportu
nigdy nie cofa już zastosowanych skutków wysyłki części zamówienia.

Najpierw odczytujemy uwierzytelnione `/v1/shipments/{id}` i sprawdzamy ID oraz
tracking względem zamówienia. Następnie opcjonalnie `/v1/tracking/{tracking_number}`.
Czas bierzemy wyłącznie z pasującego zdarzenia dla zgodnego trackingu i statusu.
Brak historii, niedostępność lub rozbieżność zasobów nie zamienia `updated_at`
ani czasu pobrania w czas fizycznego zdarzenia. Zasób Shipment pozostaje źródłem
uwierzytelnionej tożsamości i statusu; bez wiarygodnego czasu stosujemy powyższe
reguły zachowawcze. Widok pokazuje brak potwierdzonego czasu.

Kontrakty zweryfikowano z dokumentacją dostawcy:
[Shipment](https://dokumentacja-inpost.atlassian.net/wiki/spaces/PL/pages/18153485/Shipment),
[Tracking](https://dokumentacja-inpost.atlassian.net/wiki/spaces/PL/pages/18153479).
Tracking jest odrębnym zasobem i **nie zwraca statusów w sandboxie**. Dostawca
opisuje dostępność historii przez 45 dni od utworzenia przesyłki. Sandbox nie
stanowi więc dowodu działania całego procesu zmian statusów.

## F4 — następna paczka częściowego zamówienia

Pełny skład starej paczki pochodzi z niezmiennej finalnej listy. Członek, który
przeszedł do nowej paczki, musi mieć snapshot historii o zgodnym order ID,
shipment ID, trackingu i przewoźniku. Sama obecność wpisu lub wspólny klient
nie wystarcza. Zmieniamy wyłącznie nadal bieżących członków; nowy shipment/status
pozostałych jest zachowany. Dotyczy to także sytuacji, gdy wszyscy członkowie
przeszli dalej. Zgodna historia usuwa fałszywy alarm w diagnostyce zakresu.
Rzeczywisty brak, uszkodzona historia lub sprzeczny tracking nadal blokują zapis.

## F5 — wynik poczty

Trwały stan respektuje `delivery_outcome`: `accepted` oznacza przyjęcie przez
dostawcę, `unknown` niepewny wynik, `rejected` stan `failed`, a `skipped` brak
próby wysłania. `email_events` zapisuje ten sam wynik i znacznik `attempted`.
Odczyt starszych wpisów również uwzględnia ich JSON, nie tylko `ok`.
Testy używają rzeczywistego adaptera z atrapą HTTP: timeout, 503, 400, 200 oraz
wyłączona poczta. Akceptacja przez dostawcę nie jest dowodem doręczenia.

## F6 — prawdziwy wynik po commit

Błąd zapisu potwierdzenia wiadomości nie zmienia `orders_applied=True`.
Zachowana rezerwacja blokuje drugą wiadomość; wynik API i widok rozdzielają
zapis zamówień od niepewnego potwierdzenia. Przy częściowo dostępnej bazie
utrwalamy `unknown` i opis problemu. Przy całkowitej awarii zapisu po wysłaniu
API zachowuje wynik zakończonej transakcji w pamięci; trwałe `sending` pozostaje
do bezpiecznego uzgodnienia po odzyskaniu bazy. Nie usuwaj rezerwacji, aby wymusić retry.

## Weryfikacja końcowa

Zakres: dostarczone testy odbiorcze, nowe próby dokończenia, tracking47,
inpost_module, agent_shipping_draft47, fulfillment_orchestrator,
shipping_does_not_issue_stock, remanent_integration i invoice_payment_sync.
Końcowy wynik i liczby przypadków: plik `INPOST_TEST_RESULTS_2026-09-29.md`.
Jest to wybrany zakres regresji, a nie deklaracja uruchomienia całej kolekcji.

## Aktualizacja i migracje

1. Zatrzymaj instancje zapisujące oraz workery. Zachowaj wdrożone źródła i
   konfigurację. Zrób spójną kopię **całego trwałego APP_DATA_DIR**, obejmującą
   `app.db`, pliki dokumentów i historię. Przy działającej bazie użyj SQLite
   Backup API; zwykłe kopiowanie samego `app.db` podczas zapisu może pominąć WAL.
2. Pełny ZIP rozpakuj jako nową wersję kodu; paczkę różnicową nakładaj wyłącznie
   na wskazaną bazę SHA256. Zachowaj dotychczasowe sekrety i trwały katalog danych.
   Nie podmieniaj bazy, nie usuwaj historii ani stanów poczty. ZIP nie zawiera baz.
3. `app.init_db()` przy starcie stosuje addytywną migrację
   `migrations/inpost_reconciliation_sqlite.sql`. Tworzy jedną lokalną tabelę
   i indeks. Można ją ponowić. Istniejące `inpost_tracking_sqlite.sql`,
   payment outbox i reconciliation pozostają wymagane. Nie trzeba ręcznie
   wgrywać nowego SQL do Supabase dla tej poprawki.
4. Pierwszy start wykonaj z `INPOST_TRACKING_WORKER=0`. Sprawdź tożsamość
   `APP_DATA_DIR`, strukturę bazy, dostępność historycznych PDF i widok statusu.
   Sam import modułu uruchamia inicjalizację aplikacji; nie używaj go jako
   neutralnego narzędzia diagnostycznego na produkcji.
5. Po autoryzowanym wdrożeniu uruchom uzgodnienie/polling przez
   `INPOST_TRACKING_WORKER=1`. Dotychczasowe parametry częstotliwości, cooldown
   30 sekund oraz deduplikacja wspólnej paczki pozostają aktywne.
6. Oddzielny odbiór produkcyjny wykonaj na istniejącej, znanej przesyłce:
   poprawne powiązania ID/trackingu, zgodny odczyt ShipX, ostatni status po
   przeładowaniu, zakończone lokalne etapy, potwierdzona synchronizacja,
   ewidencja poczty i faktyczny POST webhooka. Wymaga to osobnej autoryzacji;
   żadnego z tych warunków nie potwierdzono w niniejszym zadaniu.

## Rollback

- Wstrzymaj workery oraz zapisy. Zrób świeżą spójną kopię danych po aktualizacji.
- Można wrócić do poprzednich źródeł przy pozostawieniu addytywnej tabeli,
  **lecz starszy kod nie obsługuje nowych zamiarów i nie ma tych sześciu poprawek**.
  Pozostaw tracking wyłączony do uzgodnienia stanów `pending`, `conflict`,
  `sending` i `unknown`. Nie traktuj rollbacku kodu jako cofnięcia przesyłek/poczty.
- Nie przywracaj automatycznie starej bazy po nowych zapisach — utraciłoby to
  późniejsze zamówienia, statusy i rezerwacje wiadomości. W razie konieczności
  odtworzenia danych wybierz spójny punkt po uzgodnieniu zmian z Supabase
  i potwierdzeń dostawcy. Zachowaj kopie obu stanów i historię.
- Nie kasuj tabel ani znaczników deduplikacji. Nie wysyłaj ponownie wiadomości
  `unknown` bez potwierdzenia u dostawcy, co stało się z poprzednią próbą.

## Powtórzenie testów offline

W osobnym venv Python 3.12. Na Windows użyj zależności z lokalnej walidacji
(bez serwerów wdrożeniowych wymagających innych platform):

```powershell
python -m pip install -r requirements-inpost-offline-tests.txt
python scripts/run_inpost_offline_tests.py --output C:\testy\inpost-nowy-przebieg
node --test test_voice_ui.mjs test_agent_stream_ui.mjs
```

Katalog `--output` ma być nowy. Runner usuwa konfigurację usług z procesu,
ustawia izolowany APP_DATA_DIR, wyłącza workery i blokuje transport sieciowy.
Testy jawnie podstawiają atrapy. Zainstaluj zależności przed uruchomieniem
runnera; katalogów venv/deps i wygenerowanych baz nie dołączaj do wdrożenia.
