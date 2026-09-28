# ZIP47: konfiguracja, kopia bezpieczeństwa i cofnięcie wydania

## Zakres i stan weryfikacji

Paczka zawiera kod i migracje przygotowane lokalnie. Nie wdrożono jej na produkcję, nie wykonano produkcyjnych migracji i nie zweryfikowano wymiany instancji Render. Wyniki testów syntetycznych i prób na nowym procesie należy czytać w raporcie wydania. Żaden plik testowej bazy nie może zastąpić istniejącej bazy użytkownika.

Wybrana architektura remanentu to SQLite na trwałym dysku, jedna instancja aplikacji. Standardowa synchronizacja tabel biznesowych do Supabase nie replikuje kompletnego remanentu, sesji liczenia, roboczych parametrów rozmowy ani lokalnych kolejek. Ta paczka nie dodaje obsługi wielu instancji dla tych danych.

## Ścieżka danych

`app.py` ustala:

- `DATA_DIR = abspath(APP_DATA_DIR)`; bez tej zmiennej używa katalogu `data` obok `app.py`.
- `DB_PATH = DATA_DIR / app.db`. Zmienna środowiskowa `DB_PATH` nie nadpisuje tej ścieżki w tym kodzie.
- Pierwszy import `app` uruchamia inicjalizację i migracje SQLite oraz może uruchomić skonfigurowane procesy pomocnicze. Import nie jest poleceniem wyłącznie do odczytu.

Zachowaj dotychczasową rzeczywistą ścieżkę danych. Zmiana `APP_DATA_DIR` na pusty katalog utworzy nową pustą bazę; nie jest migracją danych. Sprawdź linię `STARTUP_DB path=... exists=... size_bytes=...` w logu bez ujawniania zawartości bazy.

Przykładowa konfiguracja usługi z dyskiem zamontowanym dokładnie w `/var/data`:

```text
APP_DATA_DIR=/var/data/uchwyty
REMANENT_DURABLE_ROOT=/var/data
REMANENT_STORAGE_MODE=sqlite_single_instance
WEB_CONCURRENCY=1
```

`REMANENT_DURABLE_ROOT` wskazuje punkt montowania, a nie dowolny podkatalog. Guard sprawdza rzeczywisty mount, położenie otwartego pliku SQLite wewnątrz niego i powyższy tryb. Zmienna `RENDER` jest dostarczana przez środowisko hostingu. Nie usuwaj jej, aby ominąć kontrolę. Konfigurację pojedynczej instancji trzeba także sprawdzić w ustawieniach usługi; sama zmienna `WEB_CONCURRENCY` nie dowodzi braku drugiej instancji.

Nie ustawiaj flag gotowości jako zamiennika dysku. Przy `COUNT_STORAGE_NOT_DURABLE` napraw montowanie i konfigurację. Na zwykłej instalacji lokalnej trzeba samodzielnie zapewnić trwały katalog i regularne kopie.

## Sekrety i uruchomienie

Sekrety dostarczaj przez środowisko usługi; nie dodawaj `.env`, kluczy, haseł ani produkcyjnego `data` do ZIP-a. Zachowaj aktualne wartości `FLASK_SECRET_KEY` i uprawnienia wewnętrznych kont. Logowanie używa `ADMIN_PASSWORD_HASH` albo istniejącej konfiguracji `ADMIN_PASSWORD`. Zmiana sekretu sesji wyloguje użytkowników.

Istniejące integracje używają m.in.:

| Integracja | Zmienne konfiguracyjne |
|---|---|
| Model agenta | `OPENAI_API_KEY`, `AI_OWNER_MODEL` |
| Voice | `AI_STT_MODEL`, `AI_TTS_MODEL`, `AI_TTS_VOICE`, opcjonalnie `AI_TTS_INSTRUCTIONS` |
| Supabase | `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, właściwy `SUPABASE_STORAGE_BUCKET` |
| InPost | `INPOST_API_TOKEN`, `INPOST_ORGANIZATION_ID`, świadomie dobrany `INPOST_SANDBOX` |
| E-mail | `RESEND_API_KEY`, `EMAIL_FROM`, `ADMIN_EMAIL`, `EMAIL_ENABLED` |

Nie zmieniaj adresów środowiska ani modelu przy okazji aktualizacji. Klucz `service_role` pozostaje wyłącznie na serwerze. Flagi `INPOST_PICKUP_WORKER=0`, `KSEF_SCHEDULER_WORKER=0` i `EMAIL_ENABLED=0` pomagają odłączyć automatyczne działania w kopii kontrolnej; trzeba również usunąć z niej wszystkie produkcyjne klucze i zablokować sieć. Pojedyncza flaga nie wyłącza wszystkich ręcznie wywoływanych integracji.

Zainstaluj zależności z `requirements.txt` w środowisku serwera. Dla kontrolnego startu Linux z jedną instancją można użyć:

```sh
gunicorn --workers 1 --threads 4 --bind 0.0.0.0:$PORT app:app
```

Zachowaj ustawienia proxy i limitów używane przez działającą usługę. Nie używaj `python app.py` jako produkcyjnego serwera: obecny blok uruchomieniowy włącza debug. Na Windows do lokalnej kontroli można uruchomić `python -m flask --app app run --host 127.0.0.1 --port 5000` bez debug. Przeglądarka produkcyjna wymaga HTTPS zgodnie z ustawieniami ciasteczek sesji.

## Kopia przed migracją

1. Wstrzymaj ruch zapisujący, harmonogramy, workerów i drugie procesy korzystające z tej samej bazy. Zanotuj wersję kodu, migracje zdalne oraz konfigurację bez wartości sekretów.
2. Wykonaj kopię SQLite przez `sqlite3.Connection.backup()`. Samo skopiowanie aktywnego `app.db` może pominąć dane z WAL.
3. Zachowaj cały trwały katalog dokumentów razem z bazą: m.in. `faktury`, `packing-history`, `fulfillment`, `ksef`, obrazy i inne podkatalogi `APP_DATA_DIR`. Sprawdź też starsze ścieżki plików zapisane w bazie poza `APP_DATA_DIR`; te zasoby wymagają osobnej kopii. Pliki przechowywane w Supabase Storage zabezpiecz oddzielnie.
4. Zachowaj również kopię Postgres/Supabase właściwego środowiska. Lokalna kopia SQLite nie zastępuje kopii zdalnych tabel, RPC i obiektów Storage.
5. Zweryfikuj `PRAGMA integrity_check`, liczbę istotnych rekordów i możliwość odczytu przykładowego historycznego PDF. Kopię przechowuj poza katalogiem wdrażanego kodu i poza dyskiem, którego awarię ma zabezpieczać.

Przykład lokalnego skryptu kopii, do uruchomienia po wstrzymaniu zapisów. Ustaw wcześniej `APP_DATA_DIR` na istniejący katalog i `ZIP47_BACKUP_DIR` na nowy, nieistniejący katalog docelowy poza źródłem. Skrypt nie importuje aplikacji ani nie uruchamia integracji:

```python
import hashlib, json, os, shutil, sqlite3
from pathlib import Path

source = Path(os.environ['APP_DATA_DIR']).resolve(strict=True)
target = Path(os.environ['ZIP47_BACKUP_DIR']).resolve()
assert source.is_dir() and (source / 'app.db').is_file()
assert target != source and not target.is_relative_to(source)
target.mkdir(parents=True, exist_ok=False)
copy = target / 'data'
shutil.copytree(source, copy,
    ignore=shutil.ignore_patterns('app.db', 'app.db-wal', 'app.db-shm'))
original = sqlite3.connect((source / 'app.db').as_uri() + '?mode=ro', uri=True)
backup = sqlite3.connect(copy / 'app.db')
try:
    original.backup(backup)
    assert backup.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
finally:
    backup.close()
    original.close()
hashes = {str(p.relative_to(target)): hashlib.sha256(p.read_bytes()).hexdigest()
          for p in target.rglob('*') if p.is_file()}
(target / 'sha256.json').write_text(json.dumps(hashes, indent=2), encoding='utf-8')
```

## Aktualizacja i kontrola

Przed publikacją wykonaj start na odizolowanej kopii danych, z wyłączonymi zewnętrznymi integracjami. SQLite aktualizuje `app.init_db()` według [instrukcji migracji](ZIP47_MIGRATIONS.md). Nie uruchamiaj wszystkich plików SQL jednym poleceniem: katalog zawiera zarówno SQLite, jak i Postgres oraz historyczne warianty numeracji.

Po zatwierdzonym wdrożeniu sprawdź: poprawną ścieżkę istniejącej bazy; zakończenie `database_init_complete`; możliwość odczytu otwartego spisu i jego historii; zachowany snapshot zamkniętego spisu; current LP i jej zakres; stan nierozstrzygniętych operacji oraz lokalnej kolejki płatności. `local_saved=true` nie znaczy `cloud_synced=true`. `PENDING`/`ERROR` kolejki wymaga sprawdzenia synchronizacji, a nie ponownej zmiany faktury. Brak zgodnych kolumn zdalnych pozostawia zapis lokalny i jawny błąd synchronizacji.

Przy niepewnej próbie InPost lub e-mail sprawdź trwały identyfikator istniejącego wykonania. Nie usuwaj rekordów idempotencji i nie powtarzaj zewnętrznego zlecenia w celu „sprawdzenia”. Dla błędu agenta zachowaj `request_id`, `turn_id`/`agent_run_id`, kategorię i kod błędu; nie publikuj treści danych klientów, audio ani kluczy.

## Cofnięcie kodu

Zatrzymaj ruch i procesy, wykonaj drugą kopię bieżącego stanu, zachowaj logi i stan operacji zewnętrznych. W pierwszej kolejności stosuj poprawkę naprawczą na aktualnym schemacie. Jeżeli trzeba cofnąć kod, sprawdź jego zgodność z nowymi tabelami, statusami `COUNT_ONLY`/`SUPERSEDED`, trwałymi szkicami i kolejkami; sam brak błędu importu nie dowodzi zgodności. Starszy kod może nie obsługiwać oczekującej synchronizacji płatności lub nowych decyzji remanentu. Do czasu potwierdzenia zgodności pozostaw zapisy wyłączone.

Pozostaw nowe tabele, kolumny, źródła i historię. Nie wykonuj `DROP`, `TRUNCATE` ani kasowania wpisów jako „rollback”. Nie przywracaj automatycznie starej bazy ponad nowszymi zapisami. Przywrócenie bazy i dokumentów do wcześniejszego punktu wymaga osobnej decyzji operatora, zachowania obu wersji i uzgodnienia wszystkich późniejszych płatności, faktur, maili i przesyłek. Zewnętrzne skutki nie cofają się wraz z SQLite.

## Próba trwałości na środowisku kontrolnym

Poniższe próby na rzeczywistym trwałym dysku i wymianie usługi Render pozostają **do wykonania** przez operatora środowiska. Nie uruchamiać ich jako produkcyjnego testu z prawdziwymi integracjami.

1. W jednej kontrolnej bazie utwórz spis, jawnie powiąż rozmowę, zapisz obserwację, pauzę i oczekującą korektę; zapisz wersję źródła kosztów, snapshot drugiego zamkniętego spisu oraz PDF i jego SHA256. Przy atrapach zapisz szkic paczki i oczekujący status płatności. Zanotuj identyfikatory i liczby rekordów.
2. Zamknij cały proces i uruchom nowy proces na tym samym `APP_DATA_DIR`. Zweryfikuj wymienione identyfikatory, dane i PDF, izolację nowej rozmowy i brak automatycznego powielenia operacji. To test restartu procesu.
3. Osobno wymień instancję/redeploy usługi na środowisku kontrolnym, zachowując ten sam zamontowany dysk i jedną instancję. Powtórz weryfikację. To test wymiany instancji; sam restart procesu nie zastępuje tej próby.
4. Na odrębnej pustej instancji bez dysku sprawdź odmowę zapisu remanentu `COUNT_STORAGE_NOT_DURABLE`. Nie naprawiaj odmowy przez wyłączenie kontroli.
5. Dopiero po odtworzeniu kopii na osobnej ścieżce i potwierdzeniu integralności uznaj procedurę backup/restore za sprawdzoną. Wynik, wersję kodu i konfigurację dysku wpisz do protokołu wdrożenia.
