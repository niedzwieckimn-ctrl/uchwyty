# Scenariusze staging

Przed testami wykonaj kolejno migracje `architect_program_001.sql`,
`architect_security_002.sql`, `architect_commission_rounding_003.sql`,
`architect_service_role_grants_004.sql` i `architect_sale_timezone_005.sql`.
Ostatnia migracja ustawia polską datę biznesową dla potwierdzenia sprzedaży;
poprzednia nadaje istniejącej
roli serwerowej dostęp do nowych tabel programu. Odosobniona, syntetyczna
baza staging wymaga dodatkowo grantów do bazowych tabel IKUGO; w produkcji
te uprawnienia są zarządzane niezależnie.

## Stan testu staging, 28.10.2026

Izolowane, darmowe środowiska: Supabase `niedzwieccy-architekci-staging`,
Render `uchwyty-architekci-staging` i prywatne strony Netlify. Produkcja nie
została zmieniona. Poniższe potwierdzenia dotyczą jednego syntetycznego
produktu i dystrybutora A, bez realnego klienta ani wysyłki e-maila.

- Kod architekta przeszedł wycenę i został użyty dokładnie raz w zamówieniu
  `ZAM-2610285` (`orders.id=5`, `order_items.id=5`). Hurt netto 100,00 →
  92,75 PLN, detal brutto 178,34 → 160,51 PLN.
- B2B pokazał zamówienie i obie ceny; oba PDF zamówienia zwróciły HTTP 200.
  Nie potwierdzono wizualnie treści PDF.
- Do **syntetycznej bazy staging** dodano fakturę techniczną
  `STAGING-ARCH-5-TEST` (`invoices.id=5`, `invoice_allocations.id=5`) z
  `publication_state=complete`. To nie jest dowód wystawienia faktury przez IKUGO.
- Potwierdzono w B2B jedną testową sprzedaż `STAGING-PAR-5-001` z polską datą
  28.10.2026. Ledger `fcdc408f-3fe2-4c5d-b448-0b407b930511` ma 9,14 PLN
  netto i status `pending`. Druga próba nie dopisała prowizji (1 rekord,
  1 sztuka, 9,14 PLN).
- Pierwsza próba sprzedaży ujawniła różnicę dat UTC/Polska. Migracja `005`
  naprawiła tę granicę w staging; po niej zapis przeszedł.

Pełne scenariusze poniżej pozostają otwarte tam, gdzie nie ma wyraźnego
potwierdzenia wyżej. W szczególności nie przetestowano realnego wystawienia
faktury przez IKUGO, częściowej faktury, próbek z wydaniem magazynowym,
zatwierdzenia prowizji, rozliczenia miesiąca, zwrotu po wypłacie ani pełnego
testu RLS trzema tokenami. Potwierdzenie e-mail zamówienia z kodem jest nadal
oznaczone przez API jako `pending_retry` i wymaga dokończenia przed produkcją.

1. Zarejestruj architekta, potwierdź e-mail, odrzuć próbę wygenerowania kodu przy statusie `pending_verification`, następnie zatwierdź profil w IKUGO.
2. Zamów 1–3 różne warianty próbek. Spróbuj drugi raz bez dodatkowego uprawnienia. W kolejce IKUGO przejdź `requested → preparing → shipped → delivered` i sprawdź jeden zapis wydania magazynowego na wariant.
3. Wygeneruj kod dla dystrybutora A. Potwierdź cztery cyfry sufiksu, datę ważności i brak danych B2B w katalogu architekta.
4. Dystrybutor B wpisuje kod A: odmowa. Dystrybutor A otrzymuje wycenę z bieżącym cennikiem. Dla przykładu 2000,00 PLN detal brutto i 1121,39 PLN hurt netto sprawdź 1800,00 PLN dla klienta, 1040,09 PLN hurt netto po rabacie oraz 102,44 PLN prognozy prowizji.
5. Zmień ilość. Kwoty muszą być ponownie wyliczone przez serwer. Usuń kod i sprawdź powrót standardowych cen.
6. Wyślij zamówienie z kodem dwukrotnie przy tym samym `Idempotency-Key`. Potwierdź jeden rekord zamówienia, jeden redemption i jedną migawkę na pozycję. Równocześnie wyślij dwa różne żądania z tym samym kodem; drugie musi zostać odrzucone.
7. Wystaw częściową, potem końcową fakturę. Każda pozycja i suma muszą być zgodne z ceną zamówienia. Zmień cenę w lokalnym cache lub w danych formularza: wystawienie powinno zakończyć się 409. Drugi rabat na fakturze powinien być odrzucony.
8. Dystrybutor potwierdza sprzedaż mniejszej liczby sztuk niż zakupiono, z numerem dokumentu i datą. Prowizja powstaje tylko od tej liczby i początkowo ma status `pending`. Próba sprzedaży nadmiarowej lub ponownego użycia tego samego dokumentu musi być odrzucona.
9. Zatwierdź prowizję, zamknij miesiąc sprzedaży i zapisz numer wypłaty. Zatwierdź kolejną prowizję za ten sam miesiąc i utwórz uzupełniający batch. Wprowadź zwrot częściowy po wypłacie: oryginalna wypłata pozostaje, korekta jest nowym ujemnym zapisem.
10. Sprawdź RLS i API trzema tokenami: architekt, dystrybutor A, dystrybutor B. Architekt nie czyta magazynu ani cen B2B, B nie widzi zamówień A, nieaktywny `client_accounts` traci dostęp. Sprawdź też bezpośrednie żądania jako `anon`.
