# Scenariusze staging

Wyniki tych testów nie są jeszcze potwierdzone na staging. Przed uruchomieniem publicznym wykonaj je na odrębnej gałęzi/projekcie i zachowaj identyfikatory rekordów oraz zrzuty wyników.

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
