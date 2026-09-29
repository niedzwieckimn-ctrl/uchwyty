# Wyniki lokalne 29.09.2026

## Przed zmianami

- Dostarczony test odbiorczy: 6 failed, 1 passed (reproduce.log, reproduce.xml).
- Wybrane istniejące testy: 71 passed (baseline.log, baseline.xml).
- Voice + streaming UI: 59 passed JS (baseline-js.log).

## Po zmianach — jeden końcowy przebieg

**124 passed Python, 0 failed, 0 skipped**, 140.65 s (final.log, final.xml).

| Plik | Przypadki zakończone poprawnie |
|---|---:|
| test_inpost_additional_review.py | 7 |
| test_inpost_completion.py | 35 |
| test_inpost_tracking47.py | 13 |
| test_inpost_module.py | 4 |
| test_agent_shipping_draft47.py | 14 |
| test_fulfillment_orchestrator.py | 28 |
| test_shipping_does_not_issue_stock.py | 1 |
| test_remanent_integration.py | 11 |
| test_invoice_payment_sync.py | 11 |

**59 passed JS**, 0 failed (final-js.log):
`node --test test_voice_ui.mjs test_agent_stream_ui.mjs`.

35 nowych przypadków obejmuje chronologię, osobne endpointy Shipment/Tracking,
brak historii, atomowy rollback, terminalne lokalne retry, rzeczywisty adapter
poczty z atrapą timeout/503/400/200/disabled, częściową awarię chmury i restart,
warunkowy PATCH, ochronę snapshotu, wygaśnięcie dzierżawy API i sync,
spóźniony commit, wszystkich historycznych członków, fałszywą historię,
UI po awarii potwierdzenia, ponawialną migrację, integrację payment outbox,
przygotowanie PDF przed wysyłką oraz całkowitą awarię zapisu po próbie e-maila.

Dostarczone 7 testów zachowano bez zmiany oczekiwań. To wybrany zakres;
nie uruchamiano całej kolekcji projektu. Transport sieciowy był zablokowany,
APP_DATA_DIR izolowany, wywołania usług zastąpione atrapami.

Python 3.12, zależności w osobnym katalogu testowym, workery wyłączone.
Reprodukowalny runner: scripts/run_inpost_offline_tests.py.
Dowody nie potwierdzają produkcyjnego webhooka, rekordów 131/133 ani konfiguracji
Render/Supabase. Nie wykonano wdrożenia ani migracji produkcyjnej.
