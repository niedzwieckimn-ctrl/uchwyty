# Niedźwieccy dla Architektów — wdrożenie po odbiorze staging

Stan: 28.10.2026. Produkcja wymaga osobnej decyzji właściciela. Ten dokument nie upoważnia do merge, zmian DNS ani migracji produkcyjnych.

## Architektura i reguły

Architekt i B2B korzystają z Supabase Auth i `public.portal_request`. Katalog, kody, próbki, koszyk, zamówienia i historia działają przy Render OFF. IKUGO obsługuje administrację, pakowanie, faktury i rozliczenia. Role architekta i dystrybutora są oddzielne. Program jest domyślnie OFF i wymaga indywidualnej aktywacji sklepu przez administratora.

Klient otrzymuje 10% rabatu detalicznego brutto; koszt rabatu netto dzieli się po połowie między Niedźwieckich i dystrybutora. Prowizja wynosi 7% potwierdzonej sprzedaży detalicznej netto po rabacie. Ceny i reguły są utrwalane przy użyciu kodu. Kod jest jednorazowy, przypisany do sklepu. Zapis zamówienia jest atomowy i idempotentny.

Faktury mogą być częściowe lub zbiorcze (007); zachowują alokacje i zapisane ceny. Wewnętrzna kompletna faktura B2B pozwala zweryfikować sprzedaż (016), ale nie udostępnia nieopublikowanego PDF klientowi (017). Nie obchodź istniejących warunków publikacji/KSeF. Zwrot tworzy osobny ujemny zapis, także po wypłacie. Panel zapisuje referencję wypłaty, sam nie zleca przelewu.

## Kolejność produkcyjna — dopiero po zatwierdzeniu

1. Odśwież trzy gałęzie test/render-independent-portals i main. Zapisz SHA oraz poprzednie deploy ID Netlify/Render. Sprawdź diff, konflikt i aktualny kod produkcyjny; nie zastępuj nowszej wersji starym ZIP-em.
2. Zrób backup bazy (schemat, dane, role/Auth), osobno obiektów Storage, konfiguracji i dysku/pliku SQLite IKUGO. Sprawdź przywrócenie w izolowanej bazie. Dump bez odtworzenia nie jest potwierdzonym backupem. Przygotowany `production-backup.ps1` wymaga lokalnego pg_dump/pg_restore i danych dostępu operatora; nie był uruchamiany na produkcji.
3. Uruchom `production-preflight.sql` tylko do odczytu. Zidentyfikuj wszystkie integracje korzystające ze starych anonimowych widoków/tabel. 002 odbiera surowy dostęp; stare B2B/sklep muszą przejść na autoryzowane RPC w tym samym oknie.
4. Odtwórz produkcyjny snapshot w izolacji i sprawdź sekwencję: 001,003,004,005,006,007,008,010,012,013,014,015,016,017,018. 002 wykonaj przy skoordynowanym przełączeniu czytników; nie pozostawiaj nowego programu z otwartymi tabelami. Numery 009 i 011 są bootstrapem pustego staging i NIE należą do migracji produkcyjnych. Nie uruchamiaj ponownie już zastosowanych migracji bez porównania definicji.
5. Wdróż zgodny backend IKUGO, następnie frontend dopiero po kontroli RPC/RLS. Program pozostaje OFF do zaliczenia testu kontrolnego.
6. Wdróż Edge portal-email, skonfiguruj serwerowe sekrety, uruchom jeden scheduler i sprawdź dostarczenie. Skrypt harmonogramu staging zawiera adres staging i wymaga osobnego wariantu produkcyjnego.
7. Dodaj dokładne zatwierdzone URL-e Auth, wdróż konfiguracje frontendów i dopiero po odbiorze aktywuj wybrane sklepy pilotażu. Domena/DNS wymagają osobnej zgody.

## Porównanie schematu z 28.10.2026

Produkcja qfzawzkynmqkbjlbtkjd; staging irhrzdsjnwjyqfvhiclk. Porównanie tylko do odczytu. 153 brakujące w produkcji kolumny względem staging: 152 nowe programu i orders.inpost_dispatch_order_id (text NULL). Ostatnią dodawaj dopiero po kontroli zależności aktualnego backendu. Produkcja ma dodatkowe wymagane product_images.filename, product_images.created_at i product_image_assignments.created_at — zachowaj je. Typy pricing_eur i china_packages różnią się; nie konwertuj istniejących danych do typów bootstrapu staging. Stare anonimowe polityki produktów/stock/china wymagają kontroli wszystkich odbiorców przed 002.

## E-mail niezależny od Render

018 dodaje gate jednego workera, trwałe próby i zamrożony request dostawcy. architect_portal.email_worker_control.active_worker: disabled/render/edge. W staging aktywny jest edge; stary worker Render nie może przejąć wiadomości. Cron co minutę czyta sekret z Vault. Nigdy nie wpisuj sekretu do repozytorium, SQL z literalną wartością ani logów.

Edge secrets: RESEND_API_KEY, PORTAL_EMAIL_FROM, PORTAL_EMAIL_MODE, PORTAL_EMAIL_TEST_RECIPIENT (staging), PORTAL_EMAIL_WORKER_BEARER. Ostatni i Vault portal_email_worker_bearer zawierają ten sam istniejący legacy service_role JWT tego projektu. verify_jwt=true pozostaje włączone. Runtime staging zwraca nowy sb_secret_ w zmiennej SUPABASE_SERVICE_ROLE_KEY; dlatego worker korzysta z osobnej jawnie wskazanej zmiennej. Nie wyłączaj JWT, aby naprawić 401. Przejście na nowy model kluczy wymaga oddzielnego testu.

Retry zachowuje body i Idempotency-Key. Wynik niepewny po 23 godzinach przechodzi do UNKNOWN i wymaga uzgodnienia z dostawcą (Resend deduplikuje przez 24h). Stałe 4xx blokują, 429/5xx/timeout ponawiają. Błąd potwierdzenia wyniku zatrzymuje batch. Awaria dostawcy nie blokuje zapisu zamówienia.

DELIVERED w istniejącej kolejce oznacza przyjęcie przez dostawcę; provider_status=accepted nie dowodzi doręczenia. Klucz Sending access nie pozwala odczytać receipt. Sześć testów ma rzeczywiste Delivered potwierdzone w dashboardzie Resend. Automatyczny delivery/bounce wymaga uzgodnionego webhooka albo prawa odczytu; nie poszerzaj dostępu samodzielnie. Testy: node --test test_portal_email_edge.mjs.

## Konfiguracja i rollback

B2B: PORTAL_ENV, PORTAL_SUPABASE_URL, PORTAL_SUPABASE_PUBLIC_KEY, PORTAL_IKUGO_URL. node build.cjs generuje dist/runtime-config.js i wyklucza narzędzia staging z produkcji. Architekt: publiczne VITE_* według .env.example; kontrola narzędzi staging w vite.config.ts. Tylko publiczne klucze w przeglądarce. Backend/Edge przechowują sekrety serwerowo.

Przy awarii: wyłącz program dla sklepów, ustaw active_worker=disabled i zatrzymaj Cron, zachowaj zamówienia/outbox/ledger. Cofnij aplikację do wersji zgodnej z nowym schematem. Nie przywracaj starego dumpu nad nowymi zamówieniami i nie usuwaj tabel/funkcji z nowymi danymi. Preferuj korekty roll-forward. Przywrócenie dawnych szerokich grantów/RLS wymaga osobnej decyzji. Wznawiaj e-mail po uzgodnieniu DELIVERING/UNKNOWN i tylko z jednym workerem.

Dokumenty, zapisy sprzedaży, wypłaty, zwroty i e-maile przyjęte przez dostawcę wymagają uzgodnienia danych; DDL down-migration ani backup nie cofa skutków zewnętrznych. Nie usuwaj historii jako rollbacku.

## Pilotaż i granice dowodów

5–10 ręcznie zatwierdzonych architektów, tylko wskazane sklepy ON. Próbki 1–3 jako osobny workflow z katalogiem. Miesiąc wynagrodzenia według daty sprzedaży Europe/Warsaw. Regulamin, informacja o danych, dane firmy i domeny wymagają akceptacji właściciela.

Testy staging obejmują świeże loginy Render OFF, rzeczywiste zamówienia i PDF IKUGO, prowizje/zwroty oraz e-mail. Pomiary 10/25/50/100 obejmują równoczesne scenariusze odczytu na istniejących kontach, nie 100 niezależnych logowań lub zamówień. Nie omijaj limitów kodów/zamówień. Publiczna rejestracja nowego konta, odtworzenie backupu produkcji i zgodność wszystkich starych integracji pozostają bramkami przed produkcją.
