# Statusy InPost ZIP47 — aktualna instrukcja

Aktualizacja z 29.09.2026 zastępuje wcześniejszą instrukcję testowania.
Pełna procedura, opis sześciu poprawek, migracja, konfiguracja i rollback:
[INPOST_COMPLETION_2026-09-29.md](INPOST_COMPLETION_2026-09-29.md).
Wyniki lokalne: [INPOST_TEST_RESULTS_2026-09-29.md](INPOST_TEST_RESULTS_2026-09-29.md).

- Zachowaj istniejący, trwały APP_DATA_DIR i jego bazę app.db oraz dokumenty.
- Migracja lokalna inpost_reconciliation_sqlite.sql uruchamia się przy starcie,
  jest addytywna i może być ponowiona. Nie wgrywa się jej do Supabase.
- Terminalny status kończy polling API, ale nie kończy uzgadniania lokalnych etapów.
- Pełny skład paczki jest historyczny; aktualizowane są tylko nadal bieżące zamówienia.
- Wywołanie API nie tworzy przesyłek, podjazdów, faktur ani ruchu magazynowego.
- E-mail przyjęty przez usługę nie oznacza doręczenia. Wyniku unknown nie ponawiamy.
- Domyślne odpytywanie: co 900 sekund dla aktywnej paczki, pętla co 60 sekund;
  INPOST_TRACKING_INTERVAL_SECONDS 300–86400, batch 20, odstęp 1 sekunda,
  ręczny cooldown 30 sekund. Proces i trwały dysk muszą być dostępne.

Tracking InPost nie zwraca statusów w sandboxie. Testy offline używają atrap.
Sandbox nie potwierdza end-to-end webhooka/pollingu zmian transportowych.
Po autoryzowanym wdrożeniu potrzebny jest osobny odbiór istniejącej integracji.
Dokumentacja: [Tracking](https://dokumentacja-inpost.atlassian.net/wiki/spaces/PL/pages/18153479).

Nie ustalono produkcyjnej przyczyny rozbieżności 131/133. Diagnostyka zakresu
porównuje finalną listę, bieżące shipment ID/trackingi i zgodne snapshoty historii.
Nie przypisuj członków na podstawie samego klienta lub trackingu.
