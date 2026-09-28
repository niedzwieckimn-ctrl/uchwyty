# Migracje wydania opartego na ZIP47

## Zasady

Źródłem prawdy dla lokalnego schematu jest aktualny kod inicjalizacji. `app.init_db()` wykonuje go podczas importu aplikacji; także na istniejącej instalacji. Przed startem nowego kodu wykonaj [kopię danych i dokumentów](ZIP47_CONFIGURATION_BACKUP_ROLLBACK.md). Nie podmieniaj bazy na plik testowy.

Nowe pliki `zip47_*_sqlite.sql` są wyciągiem rzeczywistego schematu uzyskanego z initializerów, sprawdzonym na SQLite w pamięci. Dwukrotne wykonanie CREATE dało ten sam schemat. Są dostarczone do przeglądu i przygotowania tabel przez operatora; aplikacja nadal używa tych samych initializerów, a nie drugiego niezależnego mechanizmu migracji. Nagłówek każdego pliku zawiera moduły źródłowe i ich SHA256 w momencie eksportu. Nie kopiuj tabel remanentu pod inne nazwy ani nie uruchamiaj tych plików w Supabase SQL Editor.

## Kolejność lokalna

1. Podstawowe tabele aplikacji i faktur inicjalizuje `app.init_db()`.
2. `invoice_payment_sync.initialize()` dodaje kolejkę trwałej synchronizacji płatności.
3. Inicjalizowane są RBAC, audyt, approvals i Business Operations. Te ostatnie uruchamiają istniejący schemat magazynowy, `inventory_recount.initialize()`, `inventory_voice_context.initialize()` oraz `inventory_count_lifecycle.initialize()`.
4. `remanent.initialize_schema()` aktualizuje istniejące sesje i snapshoty, uruchamia `migrations/remanent.sql` oraz `remanent_sources.initialize()`.
5. Inicjalizowane są rozmowy agenta i trwałe wykonania. Szkic paczki jest przechowywany w istniejącym `state_json` rozmowy, bez nowej równoległej tabeli.

| Nowy plik SQLite | Moduł wykonujący migrację | Nowe obiekty |
|---|---|---|
| `migrations/zip47_count_activity_sqlite.sql` | `inventory_count_lifecycle.py`, `inventory_voice_context.py` | `internal_count_activity`, indeks sesji, `internal_inventory_voice_clarifications` |
| `migrations/zip47_remanent_sources_sqlite.sql` | `remanent_sources.py` | instalacja źródłowa, głowy wersji, niezmienne paczki i pozycje źródłowe, decyzje, idempotentne zatwierdzenia, ustawienia wyceny oraz triggery ochronne |
| `migrations/zip47_payment_outbox_sqlite.sql` | `invoice_payment_sync.py` | `invoice_payment_sync_outbox`, `invoice_payment_sync_links` |

Nie dodawaj do SQL losowego stałego `installation_id`. `remanent_sources.initialize()` tworzy jeden identyfikator lokalnej instalacji przez `INSERT OR IGNORE`; przy ponownym starcie zachowuje poprzedni. Nie resetuj go po imporcie źródeł.

## Aktualizacja istniejących tabel

Samo `CREATE TABLE IF NOT EXISTS` nie dodaje kolumn do istniejącej tabeli. Dlatego start aktualnego kodu pozostaje wymagany także po ręcznym przygotowaniu nowych tabel.

- `remanent_valuation_settings.manual_basis_confirmed INTEGER NOT NULL DEFAULT 0`: initializer odczytuje `PRAGMA table_info` i dodaje kolumnę tylko gdy jej brak. Wyciąg SQL zawiera już pełną definicję dla nowej tabeli.
- Dotychczasowe rozszerzenia `internal_inventory_count_sessions` (rok, numer, faza, data, fingerprint i dane firmy) oraz `internal_remanent_snapshots.assumed_zero` nadal aktualizuje `remanent.initialize_schema()`.
- `inventory_recount.initialize()` rozpoznaje stary constraint statusów pozycji, przenosi istniejące rekordy w obrębie SAVEPOINT i dodaje `SUPERSEDED`/`COUNT_ONLY` oraz unikalność bieżącej pozycji. Nie uruchamiaj ręcznie usuwania/rebudowania tej tabeli; zachowaj kopię i sprawdź liczby rekordów oraz historyczne wersje przed i po starcie.
- Istniejące inicjalizatory faktur, LP, prób zewnętrznych, audytu i rozmów nadal są wykonywane. Nowe pliki nie zastępują pełnego `app.init_db()`.

Przy kontrolnym odbiorze porównaj `PRAGMA integrity_check`, liczbę sesji, obserwacji, źródeł, snapshotów i zatwierdzeń. Powtórny start nie może tworzyć drugiej sesji ani kolejnej instalacji źródłowej. Nie usuwaj znaczników migracji, danych historii lub rekordów idempotencji w celu ponowienia startu.

## SQLite i Supabase to różne migracje

Tabele sesji, remanentu, roboczych szkiców oraz kolejki płatności z nowych plików są lokalne. Ich trwałość wymaga prawdziwego trwałego dysku i pojedynczej instancji. Nie są automatycznie replikowane przez standardowy pull tabel biznesowych.

W katalogu pozostają odrębne migracje Postgres używane przez funkcje zdalne. Dla instalacji Supabase operator musi sprawdzić już zastosowany stan schematu i wykonać brakujące migracje w środowisku kontrolnym przed wdrożeniem:

- `fulfillment_shipping_claims.sql`: trwałe rezerwacje nadania i idempotencja paczki.
- `fulfillment_hardening_141.sql`: wymagane metadane realizacji oraz wcześniejszy schemat numeracji; zależy od migracji rezerwacji nadania.
- `invoice_numbering_max_existing.sql`: docelowa reguła tego wydania, zgodna z Pythonem: maksimum istniejącego numeru okresu + 1, przejściowa rezerwacja współbieżna i trwała ochrona numerów objętych wysyłką do KSeF. Wykonać po `fulfillment_hardening_141.sql` i po historycznym `invoice_numbering_cursor.sql`, **jeżeli cursor był już wcześniej wdrożony**. Nie uruchamiaj ponownie historycznego `invoice_numbering_cursor.sql`: zawiera TRUNCATE pomocniczych rezerwacji. Nowa migracja zastępuje poprzednie triggery/RPC i nie renumeruje istniejących faktur.

Zastosowania tych RPC na produkcji nie zweryfikowano. Zmiany Postgres wymagają kopii oraz uprawnień administratora. Nie rozszerzaj RLS klientom ani roli `anon` w celu obejścia błędu. Kolejność i zgodność z ewentualnymi dodatkowymi migracjami konkretnej instalacji sprawdza operator.

## Płatności: zapis lokalny i synchronizacja

Aktualizacja flag faktury, powiązanych statusów zamówień i `invoice_payment_sync.stage()` odbywa się w tej samej transakcji SQLite. Synchronizacja jest wykonywana po commit. Każdy wpis ma rewizję i token dzierżawy; ACK starszej rewizji nie może usunąć nowszego oczekiwania. Przy ponownym pobraniu danych chmurowych pending pola są chronione przed cofnięciem.

W Supabase muszą być dostępne istniejące tabele `invoice_meta` i `orders` oraz pola wymagane przez konkretny zapis, m.in. `paid`, `paid_at`, `payment_reminder`, `seen_by_client`, `seen_at`, `updated_at` i `orders.status`, z typami zgodnymi z istniejącym modelem aplikacji. Ten pakiet nie zgaduje brakujących produkcyjnych typów i nie zastępuje schematu użytkownika. Niezgodność daje `REMOTE_SCHEMA_INCOMPATIBLE` i trwały oczekujący wpis, zamiast „sukcesu” po usunięciu `paid` z payloadu.

Znaczenie wyniku: `local_saved` potwierdza zapis SQLite, `sync_status` rozróżnia `LOCAL_ONLY`, `NOT_TRACKED`, `PENDING`, `ERROR`, `SYNCED`, a `cloud_synced` opisuje potwierdzenie zdalne. Po naprawieniu schematu/dostępu ponów synchronizację istniejącej kolejki. Nie twórz nowej operacji opłacenia faktury tylko po to, by wznowić transport.

## Cofnięcie

Nie dostarczono destrukcyjnej migracji down. Cofnięcie wersji kodu nie usuwa historii ani nowych tabel/kolumn. Przed uruchomieniem starszego kodu trzeba sprawdzić zgodność statusów, niezmiennych źródeł, snapshotów i niewysłanych wpisów kolejki. Procedurę zatrzymania, zachowania bieżących danych i kontrolnego odtworzenia opisuje instrukcja konfiguracji/backup/rollback. Produkcyjne zastosowanie i rollback nie zostały wykonane w tym zadaniu.
