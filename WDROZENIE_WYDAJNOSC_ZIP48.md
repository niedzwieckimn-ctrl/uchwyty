# Uchwyty — jeden pakiet wydajności na bazie ZIP48

Wydanie: 2026-09-29. Baza: `uchwyty-main (48).zip`, SHA256
`52BBBA9FEF383962853A9D49C8AE54F1A7D275628E65A60A15B57E3642900CA2`.

To jeden zestaw zmian do jednego wdrożenia. Nie trzeba instalować osobno etapów.
Starsze pliki `MANIFEST*`, `DEPLOY_MANIFEST.md` i README z oryginalnego ZIP-a
opisują inne wydania. W szczególności dawne polecenie usunięcia
`gunicorn.conf.py` nie dotyczy tego pakietu — tutaj ten plik jest potrzebny.

## Wdrożenie

1. Zaktualizuj kod aplikacji kompletem plików z `uchwyty-main` z tego ZIP-a
   i wykonaj jedno wdrożenie. Paczka nie zawiera `data/app.db`, plików WAL,
   sekretów, zależności testowych ani danych syntetycznych. Nie usuwaj ani nie
   zastępuj istniejącego katalogu danych przy wymianie kodu. Zachowaj obecną
   wartość `APP_DATA_DIR`, trwały dysk, sekret sesji i konfigurację integracji.
2. Serwer uruchamiany z katalogu zawierającego `app.py` i `gunicorn.conf.py`:
   `gunicorn app:app`. Konfiguracja w paczce wybiera jeden proces, cztery wątki
   `gthread` i istniejący port `PORT`. Jeżeli obecna komenda lub
   `GUNICORN_CMD_ARGS` jawnie nadpisuje workers/threads/worker-class albo ładuje
   inny plik konfiguracji, ujednolić te ustawienia w tym samym wdrożeniu.
   Zachowaj własne parametry timeoutu/proxy, jeśli ich używasz.
3. Przy starcie dotychczasowy `init_db()` doda trzy indeksy SQLite przez
   `CREATE INDEX IF NOT EXISTS`. Nie ma nowej migracji Supabase, usuwania danych
   ani przeliczania historycznych faktur. Budowa indeksów na dużej bazie może
   wydłużyć pierwszy start. Użyj dotychczasowej procedury kopii bazy przed
   aktualizacją; plik kopii nie jest elementem wdrażanego kodu.

Po starcie log serwera powinien wskazywać `gthread`. Konfiguracja jest zgodna
z [mechanizmem ładowania pliku Gunicorna](https://docs.gunicorn.org/en/stable/settings.html?highlight=heartbeat).
Pakiet przygotowano i przetestowano na Windows; procesu Gunicorna na Linux/Render
nie uruchamiano w tym środowisku. Nie zmieniono produkcyjnej usługi ani jej danych.

## Co zmieniono

- Faktury: zero pobrań PDF podczas wyświetlania listy. Pliki są sprawdzane przez
  istniejący endpoint pobierania. Opis na liście mówi o zapisanym pliku/snapshocie,
  nie o potwierdzonej odpowiedzi Storage.
- Filtrowanie, liczniki, sumy i stronicowanie faktur wykonuje SQL w jednym
  spójnym odczycie. Do Pythona wraca najwyżej 50 wierszy dokumentów; duże JSON-y
  pozycji nie są przesyłane do listy. Również widok klientów ma strony; jego
  sumy i liczba faktur dotyczą całego wyniku filtrów, a osobny opis wskazuje,
  ile faktur klienta widać na aktualnej stronie.
- Analiza uzupełnień: jedno dopasowanie dla każdego powtarzającego się tekstu
  wyszukiwania oraz wspólny cache obliczeń dla pulpitu, magazynu i Cash flow.
  Każdy zatwierdzony zapis lokalnej bazy unieważnia wynik, również zapis z
  innego procesu lub synchronizacji. Data i horyzont analizy należą do klucza.
  Obliczenia w istniejącej transakcji, np. Orderchamp, pomijają cache.
- Cache używa stałego połączenia obserwującego
  [SQLite `data_version`](https://www.sqlite.org/pragma.html#pragma_data_version),
  ma limit pamięci i TTL 60 s. Nie przechowuje stron HTML, sesji ani tokenów
  CSRF. Odczyt nie zmienia biznesowych reguł stanów i rezerwacji.
- Magazyn: pobiera przypisania zdjęć tylko dla widocznych produktów. Podpisy
  miniatur są pobierane jednym żądaniem po wyświetleniu tabeli. Udane podpisy
  są ponownie używane przed wygaśnięciem. CSP dopuszcza dokładnie skonfigurowane
  źródło obrazów Supabase. Pozostaje dotychczasowy fallback zdjęć.
- Jinja: cache skompilowanych szablonów, z osobnym renderowaniem danych i sesji
  dla każdego requestu.
- Supabase: najwyżej cztery równoległe odczyty niezależnych tabel; zapisy do
  SQLite, zabezpieczenia publikacji, kolejki i uzgadnianie pozostają w dotychczasowej
  kolejności. Identyczne wiersze nie są ponownie zapisywane. Początkowy bootstrap
  nadal wymaga poprawnych danych i nie pokazuje fałszywych zer.
- SQL: indeksy alokacji po pozycji, fakturze i zamówieniu; pulpit liczy wartości
  ostatnich ośmiu zamówień po ich wcześniejszym wyborze i korzysta z istniejącego
  indeksu cen EUR. Cash flow nie liczy drugi raz jednostek tej samej faktury.
- Nawigacja sygnalizuje otwieranie strony od razu po kliknięciu.

## Pomiary po wdrożeniu

`PERF_LOG_ENABLED=1` jest ustawieniem domyślnym. Zalogowany użytkownik otrzymuje
`Server-Timing` oraz `X-Request-ID`; ten sam identyfikator jest w logu `PERF`.
Log zawiera czas wykonania SQLite (bez pełnego fetchowania wszystkich wyników),
oczekiwania na procesową blokadę zapisu, renderu, podpisywania i pobierania
Storage oraz bootstrapu. Podetapy mogą się nakładać — nie sumuj ich do `total`.
`view_other` jest resztą czasu, nie pomiarem samych zapytań SQL.

Przeglądarka zapisuje w konsoli `UCHWYTY_NAV` oraz w elemencie
`meta[name="uchwyty-navigation-timing"]` czasy TTFB, pobrania dokumentu,
DOMContentLoaded i load. Są to pomiary Navigation Timing, bez narzutu sterowania
przeglądarką. `load` nie czeka na później pobierane miniatury z `loading="lazy"`.
Nie rejestrowane są hasła, klucze, zawartość dokumentów ani parametry wyszukiwania.

Pełne odświeżenie danych po starcie nadal zależy od Supabase. Cache obliczeń
śledzi lokalny snapshot; nie zastępuje istniejącej synchronizacji z chmurą.
Nie obiecujemy czasów Rendera na podstawie testów lokalnych. Porównaj po jednym
wdrożeniu tę samą trasę: pulpit → zamówienia → magazyn → klienci → faktury →
Cash flow → wydane → pulpit. `Server-Timing total` mierzy obsługę requestu;
dużo wyższe TTFB może oznaczać kolejkę przed aplikacją albo sieć.

## Cofnięcie kodu

W razie potrzeby przywróć poprzedni kod i poprzednią konfigurację uruchomienia
razem. Nie przywracaj starej kopii bazy w miejsce nowych transakcji biznesowych.
Dodane indeksy są zgodne z poprzednią wersją kodu i mogą pozostać w bazie.
