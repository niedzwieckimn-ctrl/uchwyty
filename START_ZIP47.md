# Poprawki ZIP47 i integracja z Rocznym Rozliczeniem

Ta paczka zawiera pełne źródła oparte na `uchwyty-main (47).zip`, SHA256 `ca6bcc8eead662c208653eaee308a8ace1a8dfc2ea23ce22a7ec7df81fb7c00e`. Nie zawiera baz danych ani sekretów. Historyczne raporty i manifesty pozostawione w źródłach opisują wcześniejsze wydania; bieżące zmiany opisują pliki `docs/ZIP47_*` oraz zewnętrzny raport i manifest wydania.

1. Przed uruchomieniem przeczytaj [konfigurację, kopie i cofnięcie](docs/ZIP47_CONFIGURATION_BACKUP_ROLLBACK.md).
2. Sprawdź [migracje lokalne oraz Supabase](docs/ZIP47_MIGRATIONS.md). Import `app.py` inicjalizuje schemat lokalnej bazy. Pierwszy start wykonaj na odizolowanej kopii z właściwym `APP_DATA_DIR`.
3. [Instrukcja użytkowa](docs/ZIP47_INSTRUKCJA_UZYTKOWA.md) opisuje agenta, jedną paczkę, koszty, liczenie, korekty i zamknięty spis.
4. Dla drugiej aplikacji użyj dołączonej osobno paczki źródłowej Rocznego Rozliczenia z mostem JSON v2. Stary EXE nie zawiera tej aktualizacji; budowa EXE nie wchodziła w zakres tego wydania.

To lokalnie zweryfikowane źródła, bez wdrożenia na Render i bez produkcyjnych operacji. Testy używają atrap InPost, poczty i modelu. Odbiór na kopii rzeczywistych danych, konfiguracji zdalnych RPC, katalogu oraz trwałego dysku pozostaje wymagany przed użytkowaniem produkcyjnym.

Testy Voice/streaming: `node --test test_agent_stream_ui.mjs test_voice_ui.mjs`. Osobny odziedziczony `test_i18n.mjs` wymaga brakującego już w ZIP47 pliku panelu `index.html`; nie dotyczy on tych 59 scenariuszy. Python: zależności z `requirements-dev.txt`, testy `test_*.py`; nie uruchamiaj ich ze wskazaniem produkcyjnej bazy lub sekretów. Skrypty odizolowanego uruchomienia dołączono w paczce dowodów weryfikacji.
