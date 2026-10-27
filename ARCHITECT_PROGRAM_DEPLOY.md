# Program „Niedźwieccy dla Architektów” — wdrożenie

Zmiany powstały na podstawie `uchwyty-main (51).zip`, a następnie zostały przeniesione na aktualny `main` repozytorium `niedzwieckimn-ctrl/uchwyty` (commit `af18f92051715a1e374103ec66b18a4b7e2ef47d`), połączonego z usługą Render `uchwyty`. Porównano pliki `app.py`, `routes/orders.py` i `routes/invoices.py`; ich treść z ZIP51 była taka sama po normalizacji końców linii. Nowsze pliki produkcyjnego repozytorium pozostały zachowane. Przed scaleniem ponownie sprawdź aktualny `main` i schemat bazy. Nie zastępuj nowszego kodu pełnym ZIP-em.

## Kolejność

1. Zrób kopię bazy i sprawdź ją na izolowanym środowisku. Potwierdź kolumny `client_accounts(user_id,customer_id,is_active)`, `orders.idempotency_key`, `order_items.unit_net_price`, `invoice_allocations.order_item_id`, `invoices.publication_state`, `stock.qty`.
2. Na staging uruchom `migrations/architect_program_001.sql`. Jest to migracja jednorazowa. Zawiera nowe tabele, ograniczenia, RLS i funkcje transakcyjne; nie aktualizuje istniejących zamówień.
3. Wdróż ten backend z obecnymi sekretami Supabase tylko w konfiguracji serwera. Dodaj `ARCHITECT_ALLOWED_ORIGINS=https://<testowy-adres-netlify>`. Sprawdź, że nowe konto Auth bez aktywnego `client_accounts` dostaje 403 od każdego endpointu `/api/client/*`.
4. Wdróż zmieniony Panel Klienta B2B na testowym adresie i Panel Architekta na testowym Netlify. Dodaj adres testowy do przekierowań Supabase Auth. Wypełnij publiczne zmienne Vite zgodnie z `.env.example` panelu.
5. Uruchom scenariusze w `TESTY-WDROZENIOWE.md`, łącznie z dwoma kontami dystrybutorów, kontem architekta, częściową fakturą, dwukrotnym zamówieniem, zwrotem po wypłacie i odczytami RLS. Testy muszą działać na staging z kopiami danych testowych.
6. Dopiero po sprawdzeniu, że B2B korzysta z API serwerowego, zastosuj `migrations/architect_security_002.sql` na staging, następnie sprawdź ponownie koszyk, zamówienia, faktury i magazyn. W produkcji zastosuj tę migrację w tej samej kolejności, w krótkim oknie z planem cofnięcia.
7. Włącz produkcyjne adresy w CORS i Supabase Auth. Domena `architekci.niedzwieccy.com` oraz publikacja zmian produkcyjnych wymagają zatwierdzenia.

## Reguły programu

Klient otrzymuje 10% od ceny detalicznej brutto. Wartość rabatu netto dzieli się po połowie: 50% obniża cenę hurtową netto, a 50% obciąża dystrybutora. Prowizja architekta wynosi 7% wartości netto faktycznie potwierdzonej sprzedaży detalicznej po rabacie. Stawki i czas ważności kodu są wersjonowane w `architect_program_settings`; transakcje przechowują migawki.

Kod można wykorzystać raz i tylko u wybranego, aktywnego dystrybutora. Zamówienie, wykorzystanie kodu i ceny są zapisywane atomowo w PostgreSQL. Powtórzenie z tym samym kluczem idempotencji odtwarza wynik. Faktura dla zamówienia z kodem sprawdza utrwalone ceny i sumy oraz blokuje ponowny rabat. Faktury mogą obejmować częściowe ilości, ale nie mogą łączyć zamówienia programu z innym zamówieniem.

Prowizja powstaje dopiero po potwierdzeniu faktycznej sprzedaży i wystawieniu kompletnej faktury B2B. Administrator zatwierdza ją i zapisuje zewnętrzny numer wypłaty; system nie wykonuje przelewów. Opóźnione zatwierdzenie tworzy dodatkową partię rozliczenia dla miesiąca pierwotnej sprzedaży. Zwrot tworzy odrębny ujemny zapis, także po wypłacie.

## Ograniczenia przed produkcją

- Zamówienia programu są obecnie dopuszczone tylko przy VAT 23%, ponieważ istniejący generator faktur krajowych narzuca tę stawkę. Katalog umie pokazać inną stawkę; użycie kodu na taki wariant kończy się 409 do czasu przebudowy fakturowania i testu podatkowego.
- Skuteczność funkcji SQL została sprawdzona składniowo, nie transakcyjnie na PostgreSQL. Nie uruchamiaj migracji na `main` przed testem staging.
- Weryfikacja Instagrama jest manualną decyzją administratora. Kwestie regulaminu i tekstu informacji o danych osobowych wymagają zatwierdzonej treści przed publiczną rejestracją.
- Eksporty i lista administratora mają limit 5000 rekordów na odczyt. Przed większą skalą dodaj paginację.

## Test po wdrożeniu

Zapisz numer zamówienia, kod, `idempotency_key`, kwotę netto/brutto koszyka i faktury, identyfikator sprzedaży detalicznej oraz prowizji. Ponów identyczne żądanie zamówienia i potwierdź, że zwraca ten sam rekord. Spróbuj wykonać kod z innego konta dystrybutora. Z konta architekta zapytaj bezpośrednio o `stock`, `china_items`, `china_packages`, `client_stock_catalog_v` oraz `/api/client_stock_catalog`; każdy odczyt danych operacyjnych musi być odrzucony.
