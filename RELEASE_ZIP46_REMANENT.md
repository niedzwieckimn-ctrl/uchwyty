# ZIP46 + remanent: raport i instrukcja

## Baza i zakres

- Baza: `uchwyty-main (46).zip`, SHA256 `5D59E36E91277D0E1C96174ACBB13B33CC88D14598BA9C05EEAA24628AC7EE04`.
- Materiał pomocniczy: `remanent-pliki-do-podmiany.zip`, SHA256 `B1D1EA00F5B03DC982D35A765FEDEBC529095DF133E6BCB275416136C6001F3F`.
- Kod zmieniono w osobnej kopii ZIP46. Starsze `app.py`, `agent_runtime.py`, `business_operations.py` i moduł faktur nie zostały podmienione w całości. Nie wykonano wdrożenia ani migracji produkcyjnej.
- Paczka wynikowa nie zawiera `data/app.db`, innych baz, sekretów ani lokalnych plików testowych. **Nie należy podmieniać produkcyjnej bazy plikiem z ZIP.**

## Diagnoza i zmiany

### Wyszukiwania

ZIP46 próbował budować rodzinę modelu jedynie z pola `products.name`. Dla produktów z nazwą będącą kodem wariantu prowadziło to do braku modelu; warianty o różnych nazwach mogły też rozdzielić model. Identyfikator rodziny powstaje teraz z kodu bazowego `model`, jeśli jest rzeczywistym kodem modelu, albo z rzeczywistej nazwy katalogowej. Nazwa, kod i SKU służą do wyszukiwania; kilka wariantów tej samej rodziny daje jeden model. Gdy pasuje kilka rodzin, wynik zostaje niejednoznaczny. Istniejące aliasy oparte na haszu nazwy są odwzorowywane na nową rodzinę tylko przy jednoznacznym przypisaniu.

Zapisane zdarzenia są ponownie rozpoznawane w odczycie względem aktualnego katalogu. Historia nie jest nadpisywana ani dopisywana ponownie. Intencje nadal grupują kolejne wpisy w 5 minut. Kafelek lidera, ranking i liczba klientów używają jednego przeliczenia po tych samych filtrach; ranking powstaje przed paginacją. Wynik historyczny może się zmienić po zmianie katalogu, ponieważ jest projekcją obecnego katalogu, a nie nowym zapisem zdarzenia.

### Import, dokumenty i 17TRACK

Menu i tytuł rzeczywistego szablonu `/china` mają nazwę **Import**. Adresy `/china` i powiązania P/O zostały zachowane. Formularz dokumentu był już w używanym szablonie, lecz brakowało pełnej trwałej ścieżki metadanych/pliku oraz czytelnego potwierdzenia. Upload waliduje PDF i limit 10 MB, wiąże dokument z `package_id`, zapisuje bajty w prywatnym Supabase Storage przy aktywnym Supabase albo obok lokalnej bazy, synchronizuje metadane `china_documents`, pokazuje listę oraz pozwala pobrać i usunąć plik. Lokalny test obejmuje restart inicjalizacji bazy i ponowne pobranie. Test na żywej konfiguracji Supabase Storage i wymianie instancji pozostaje do wykonania po osobnej autoryzacji wdrożenia.

Parser 17TRACK w ZIP46 brał ostatni element listy zdarzeń ostatniego przewoźnika, bez porównania dat i stref. Teraz porównuje daty `time_utc`/`time_iso` wszystkich `tracking.providers[].events`, bierze najnowszy skan, dodaje lokalizację do opisu i zachowuje oddzielnie czas zdarzenia oraz ostatnie odpytanie API. Brak skanu jest pokazany wprost. Zrzut nie zawiera surowej odpowiedzi API, więc nie można potwierdzić, czy dla wskazanej przesyłki dostawca udostępnił nowszy skan. Mapowanie sprawdzono względem [oficjalnej dokumentacji Tracking API v2.4](https://api.17track.net/en/doc); rejestracja i ręczne odświeżanie pozostają.

### Cennik

`/pricing` pozwala przełączyć zapisany cennik PLN lub EUR i edytować pojedynczy wiersz. PLN ma pola netto/brutto; EUR zachowuje oddzielne znaczenie `PREIS EUR` i `UVP EUR`. Nic nie jest przeliczane kursem. Zapis jest walidowany jako nieujemna liczba, przyjmuje przecinek i zaokrągla do dwóch miejsc; przy aktywnym Supabase najpierw zapisuje tabelę zdalną. Używa istniejącej sesji administratora, kontroli CSRF oraz audytu; import obu cenników pozostaje. Test potwierdza, że zapis ceny nie zmienia cen pozycji już istniejącego zamówienia. Nie wykonywano zapisu na produkcji.

### Remanent, faktury i Cash Flow

Do ZIP46 włączono moduł remanentu, PDF, szablon i schemat SQLite, bez podmiany aktualnego agenta i logiki faktur. Moduł tworzy szkic, wpisy stanu otwarcia/zakupów/sprzedaży/wartości, podgląd i import CSV z kontrolą duplikatów, potwierdzenie pokrycia zakupów, zamrożony snapshot przy rozpoczęciu, liczenie, różnice, zamknięcie, raport PDF i arkusz spisu. Brak znanych zakupów jest oznaczony i blokuje zamknięcie bez jawnego potwierdzenia pokrycia.

Otwarte liczenie roczne właściciela jest wspólną sesją dla ekranu i operacji głosowych; agent nie tworzy drugiego spisu. Wpis liczenia i import historii nie korygują `stock`. Korekta fizyczna nadal wymaga odrębnej operacji z dotychczasową kontrolą uprawnień, zatwierdzeniem, audytem i ochroną przed ponownym zapisem. Sprzedaż źródłowa dla remanentu i Cash Flow korzysta ze wspólnego odczytu opublikowanych kompletnych faktur, aby uniknąć podwójnego liczenia wersji roboczych.

## Trwałość danych: warunek uruchomienia na Render

Obecna architektura jest hybrydowa: część tabel synchronizuje się z Supabase, ale nowe tabele remanentu i istniejące sesje liczenia są w SQLite. Sama inicjalizacja schematu ani restart procesu nie potwierdzają przeżycia deployu. [Render opisuje domyślny system plików jako nietrwały](https://render.com/docs/disks) i potwierdza zachowanie wyłącznie zapisów pod punktem montowania trwałego dysku. Z tego powodu zapis POST remanentu na Render jest zablokowany, dopóki operator nie ustawi **obu** `APP_DATA_DIR` na zweryfikowany katalog trwałego dysku i `REMANENT_PERSISTENCE_READY=1`. Przy braku Supabase taki sam warunek chroni lokalny upload PDF. To flaga po weryfikacji, nie automatyczny dowód trwałości. Lokalnie można testować SQLite bez tej flagi.

Trwały dysk Render jest dostępny jednej instancji w czasie pracy; [nie jest dostępny w build/pre-deploy ani dla cron job](https://render.com/docs/disks). Jeżeli obecny system wymaga wielu instancji, nie należy uruchamiać tego modułu z lokalnym SQLite: trzeba wcześniej przenieść sesje i wszystkie tabele remanentu do współdzielonej bazy, wraz z transakcjami i testem konfliktów. Tego wariantu nie wykonano w tej paczce.

## Kolejność wdrożenia po osobnej zgodzie

1. Zrób spójny backup produkcyjnej SQLite metodą SQLite `backup` podczas wstrzymanych zapisów; zachowaj także katalog lokalnych dokumentów P/O, jeśli istnieje. Ustal bieżący rzeczywisty `DB_PATH`, wersję kodu, konfigurację Supabase, wersję schematu i plan przywrócenia. Nie kopiuj bazy w trakcie zapisu zwykłym kopiowaniem pliku.
2. W Supabase SQL Editor zastosuj `migrations/china_documents_supabase.sql` **przed** uruchomieniem nowego kodu; potwierdź istniejącą tabelę `china_packages` i zgodność typu `id`. Sprawdź prywatny bucket Storage oraz działanie server-side service role. Metadane nie udostępniają PDF przez publiczny URL; aplikacja pobiera je po uwierzytelnieniu. [Supabase opisuje zasady prywatnych bucketów](https://supabase.com/docs/guides/storage/buckets/fundamentals).
3. Utwórz i podłącz trwały dysk Render. Ustaw `APP_DATA_DIR` na katalog **wewnątrz** jego punktu montowania (przykładowo `/var/data`, tylko jeśli tak skonfigurowano mount). Przenieś tam spójny backup `app.db` oraz dotychczasowe lokalne pliki dokumentów w układzie `china_documents/`; zachowaj uprawnienia odczytu/zapisu procesu. Nie nadpisuj nowszej bazy starszą kopią. Nie włączaj jeszcze `REMANENT_PERSISTENCE_READY`.
4. Wdróż kod. Potwierdź, że aplikacja czyta tę samą przeniesioną bazę, nowy schemat z `migrations/remanent.sql` został zainicjalizowany, liczby kontrolne istniejących tabel i zamówień są zgodne, a dotychczasowe funkcje działają. Jeśli korzystasz z Supabase, sprawdź upload, listę i pobranie testowego PDF z prywatnego Storage oraz metadane po restarcie instancji.
5. W środowisku kontrolnym wykonaj pełny spis testowy, restart procesu i osobno nowy deploy/wymianę instancji; odczytaj tę samą sesję, snapshot i oba PDF-y. Dopiero po sprawdzeniu dysku i kopii zapasowej ustaw `REMANENT_PERSISTENCE_READY=1`. Zweryfikuj na żywej aplikacji czas agentowego odczytu/zatwierdzenia, bo lokalny test nie potwierdza opóźnienia produkcyjnego.

## Cofnięcie

Wstrzymaj zapisy, wykonaj dodatkowy backup stanu po wdrożeniu i zachowaj PDF/CSV nowo utworzonych remanentów. Wyłącz `REMANENT_PERSISTENCE_READY`, wróć do kodu ZIP46 i sprawdź krytyczne odczyty. Dodatkowa tabela Supabase i kolumny SQLite mogą pozostać, jeśli ZIP46 działa na tej bazie; nie usuwaj ich automatycznie. Jeżeli wymagane jest przywrócenie bazy sprzed wdrożenia, zrób to tylko na podstawie spójnego backupu po ocenie, które późniejsze zamówienia, faktury, stany i spisy zostałyby utracone. Usunięcie plików Storage również wymaga osobnego spisu i decyzji.

## Weryfikacja i granice

- 19/19 celowanych testów przechodzi: nazwa/kod/SKU/warianty/niejednoznaczność/aliasy, ponowne rozpoznanie historii, wieloprzewoźnikowy 17TRACK ze strefami, PDF przesyłki po restarcie i blokada zapisu na nietrwałym Render, oba cenniki i snapshot zamówienia, remanent od szkicu przez CSV i zamknięcie po PDF oraz wspólna sesja z głosem.
- Uruchomiona lokalna aplikacja: sprawdzono ekran Import ze szczegółami dokumentów i trackingu, ekran PLN/EUR, ekran remanentu oraz panel wyszukiwań z wcześniej nierozpoznanym wpisem, który pojawił się w rankingu i kafelku lidera. Użyto wyłącznie syntetycznej bazy podglądu.
- Szeroki zestaw przy limicie 25 błędów: wynik **25 failed, 419 passed** zarówno w kopii wynikowej, jak i w nienaruszonym ZIP46; nazwy pierwszych 25 błędów są takie same. Zestaw `test_warehouse_ux.py`: **7 failed, 18 passed** w obu wersjach, te same przypadki. Są to odziedziczone niespójności testów z obecną implementacją ZIP46, nie dowód poprawności wszystkich ścieżek.
- Nie potwierdzono rzeczywistej odpowiedzi 17TRACK dla wskazanej przesyłki, produkcyjnego Supabase Storage, trwałości po wymianie instancji Render ani czasu pracy agenta na żywo. Nie używano danych produkcyjnych.

Pełna lista zmienionych plików i ich SHA256 znajduje się w `MANIFEST_ZMIAN_SHA256.txt` obok tej instrukcji. Wewnętrzne manifesty z wcześniejszych etapów zawarte w ZIP46 są historyczne; niniejszy manifest opisuje tę paczkę.
