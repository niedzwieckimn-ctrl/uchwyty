> Raport historyczny pierwszej poprawki. Poniższe liczby i opisy odnoszą się
> do wcześniejszego wydania, które było bazą dokończenia. Aktualny wynik odbioru,
> zamknięcie sześciu usterek i ograniczenia są w
> [INPOST_COMPLETION_2026-09-29.md](INPOST_COMPLETION_2026-09-29.md)
> oraz [INPOST_TEST_RESULTS_2026-09-29.md](INPOST_TEST_RESULTS_2026-09-29.md).

# Raport poprawki statusów InPost na bazie ZIP47

## Baza i zakres

Źródło: `uchwyty_ZIP47_poprawki_remanent_zrodla.zip`, SHA256 `a911fa9a0da7f516351a2c1aa8ef82c71a224270a7ff411181ca39ff1498604a`. Pracowano na osobnej kopii. Zmiany w istniejących plikach: `app.py`, `inpost_module.py`, `routes/orders.py`, `routes/shipping.py`. Nowe: `inpost_tracking.py`, `test_inpost_tracking47.py`, `migrations/inpost_tracking_sqlite.sql`, `docs/INPOST_STATUS_ZIP47.md`, ten raport. Pełny manifest SHA256 wszystkich plików źródłowych jest w korzeniu paczki jako `SHA256_INPOST_STATUS.txt`; suma samego ZIP-a jest w osobnym pliku `.sha256` obok paczki.

## Lokalnie odtworzone i poprawione

- Odczyt InPost mógł się udać, podczas gdy zastosowanie statusu do zamówień kończyło się błędem. Widok używał `if status / elif error`, więc błąd znikał. Teraz oba fakty są widoczne równocześnie, a etap błędu jest odróżniony od API.
- Status przewoźnika znikał z karty po zwykłym przeładowaniu. Nowa tabela przechowuje surowy kod, polską etykietę widoku, czas weryfikacji, czas zdarzenia z ShipX, ostatnią próbę i jej wynik. Nieudane pobranie nie usuwa wcześniejszego statusu.
- Tekst „Wysłane po nadaniu” był wyliczany z etapu zamówienia. Widok korzysta teraz z trwałej ewidencji wiadomości dla przesyłki lub starszych dowodów `email_events`; nie myli przyjęcia przez dostawcę z doręczeniem.
- Ręczny odczyt, webhook i proces okresowy korzystają z jednej ścieżki. Webhook jest wskazówką, a status jest ponownie pobierany z uwierzytelnionego ShipX. Ręczny przycisk ma 30 sekund ograniczenia, a wspólna paczka ma jedną dzierżawę i jedno odpytywanie API w cyklu.
- Aktualizacja zamówień jest atomowa dla dokładnego zbioru członków potwierdzonej listy. Brakujące powiązanie jest naprawiane tylko przy zgodnej finalnej liście, trackingu, przewoźniku i braku historycznego użycia tej przesyłki. Pozostałe konflikty ID lub trackingu zatrzymują zapis i zwracają ID do diagnostyki. Historyczny tracking sam nie dołącza zamówienia do innej paczki. Starsze zdarzenia nie cofają nowszego statusu; poprzednia przesyłka częściowego zamówienia nie jest traktowana jako bieżąca.
- Lokalnie wykonana zmiana zamówień ma odrębny stan synchronizacji Supabase. Gdy zapis chmurowy się nie uda, pobranie zdalnej starszej kopii chroni te pola, a ponowienie próbuje wysłać lokalny stan bez drugiego e-maila lub nowej operacji transportowej.
- Jedno powiadomienie na shipment ID jest rezerwowane transakcyjnie przed próbą wysyłki. Awaria po możliwej akceptacji jest oznaczona jako wynik niepewny i nie prowadzi do automatycznego ponowienia.

## Odpowiedzi operacyjne

- **Jak często automat?** Każda aktywna przesyłka staje się ponownie kwalifikowana po 15 minutach domyślnie, gdy działa proces aplikacji z tokenem InPost; pętla sprawdza zaległe rekordy co 60 sekund. Duża kolejka, błędy albo uśpienie usługi mogą opóźnić sprawdzenie. Konfiguracja i limity w `INPOST_STATUS_ZIP47.md`.
- **Kiedy webhook?** Gdy InPost dostarczy POST po zmianie statusu i URL jest poprawnie skonfigurowany. Produkcyjne dostarczanie webhooka nie zostało potwierdzone.
- **Co po kliknięciu?** Ostatni status i czas pozostają widoczne; osobno pokazuje się błąd API, konflikt zakresu, oczekująca synchronizacja albo problem wiadomości. Ponowienie w ciągu 30 sekund pokazuje komunikat o ograniczeniu.
- **Wspólna paczka?** Jedno shipment ID oraz dokładnie zgodny zbiór zamówień z finalnej lub bieżącej listy; aktualizacja wszystkich członków w jednej transakcji. Jednoznaczny brak ID można naprawić na podstawie finalnej listy i dodatkowych dowodów; przy innym konflikcie nie ma cichego częściowego sukcesu.
- **Błąd zapisu/e-maila?** Są pokazane odrębnie od potwierdzonego statusu przewoźnika i od lokalnie wykonanego zapisu zamówień.

## Granica ustaleń

Nie odczytywano produkcyjnych rekordów 131/133 ani konfiguracji webhooka, więc nie ustalono faktycznej przyczyny ich rozbieżności. Sekcja diagnostyczna i procedura uzgodnienia w `INPOST_STATUS_ZIP47.md` wskazują, co porównać bez automatycznego dopisywania członków. Nie wykonywano produkcyjnych migracji, wysyłek, e-maili, faktur ani wdrożenia. Testy używają izolowanego `APP_DATA_DIR` i atrap sieci/poczty; nie dowodzą działania wdrożonego webhooka ani trwałości dysku Render.

## Weryfikacja lokalna

- Nowe testy syntetyczne: 13 przejść (cztery zamówienia, jednoznaczna naprawa brakującego ID, trwałość po inicjalizacji, UI, konflikt zakresu i trackingu, brak API, starszy webhook, równoległość, harmonogram, awaria chmury i poczty).
- Łącznie z testami regresji InPost, zamówień, remanentu i Voice: **139 testów Python przeszło** w wybranym zakresie.
- `node --test test_voice_ui.mjs`: **28 testów JS przeszło**.
- Kompilacja składni zmienionych modułów i kontrola integralności archiwum ZIP: przeszły.

Zakres jest celowo wybrany; nie deklaruje przejścia całej dostarczonej kolekcji testów.
