# Agent i remanent — instrukcja po aktualizacji ZIP47

## Jedna paczka i cztery zamówienia

1. Zapytaj o spakowaną, niewysłaną paczkę i jej listę pakową. Sprawdź zakres oraz faktycznie spakowane ilości.
2. Wskaż wspólną paczkę i przewoźnika InPost. Podaj wymiary i wybór powiadomień, np. „45 na 20 na 20 cm, SMS i e-mail”.
3. Odpowiedź „pięć kilogramów” uzupełnia wagę tej paczki. Agent pobiera świeże dane i przygotowuje podsumowanie do zatwierdzenia. Brakujące dane adresowe wymagają uzupełnienia.
4. Sprawdź paczkę, adres i parametry na karcie operacji. Zatwierdzenie utworzenia przesyłki jest oddzielne od zamówienia odbioru przez kuriera.
5. Przy przerwanym połączeniu sprawdź odzyskany wynik istniejącej operacji. Status niepewny wymaga uzgodnienia z dostawcą; powtarzanie wagi nie tworzy nowego zlecenia.

Zakres „wskazane zamówienie” jest domyślny. Szerszy zakres klienta wymaga jawnego wskazania i jest widoczny przed zatwierdzeniem. Częściowe zamówienie może mieć poprawnie spakowaną paczkę; jej zawartość wynika z alokacji, a nie całej ilości zamówionej.

## Koszty i uzgodnienie dostaw

1. W Rocznym Rozliczeniu otwórz **Kontrolę zgodności z magazynem**. Wybierz zakres, podstawę kosztów i stany początkowe; wyeksportuj JSON v2. Dla dokumentu i przyjęcia w różnych latach użyj odpowiedniego zakresu, np. „Wszystkie lata”.
2. W magazynie utwórz szkic remanentu i przejdź do **Uzgodnienia źródeł i wyceny**. Wczytaj plik i rozstrzygnij każdą pozycję: konkretne przyjęcie, historia zakupów, stan początkowy albo wykluczenie.
3. Koszt dostawy już przyjętej powiąż z jej rzeczywistym przyjęciem. Dla kilku przyjęć podaj podział ilości. Historia wymaga daty fizycznego przyjęcia. SKU identyfikuje produkt; podobna data i ilość są tylko sugestią.
4. Zatwierdź te same dane źródłowe z podglądu. Powtórzenie jest idempotentne; zmiana danych wymaga nowego podglądu. Import kosztów i historii zachowuje fizyczny stan magazynu.
5. Wybierz osobno **średnią ważoną okresową** i podstawę kosztów: towar netto / towar + transport / pełny koszt. Potwierdź podstawę ręcznych kosztów, jeżeli ich używasz, oraz kompletność zakupów. Brak stanu początkowego wymaga ustalenia; zero trzeba potwierdzić.

Średnia = (wartość stanu początkowego + wartość uzgodnionych zakupów) / (ilość początkowa + ilość zakupów). Cena zachowuje pełną precyzję Decimal; wartości pozycji zaokrąglane są do groszy. Wybór metody jest technicznym ustawieniem aplikacji.

## Liczenie i korekty

- Zwykłe liczenie: rozpocznij rozmową **„Robimy remanent”**, następnie podawaj produkt i ilość. Szkic rocznego spisu nie przejmuje rozmowy.
- Wspólny spis roczny: najpierw rozpocznij przygotowany spis na ekranie. Otwórz rozmowę Asystenta i jawnie wybierz ją w polu **Wspólne liczenie z rozmową** na ekranie spisu.
- Przy wariantach wybierz właściwy, np. **„tę drugą”**. Kolejny produkt nie dziedziczy poprzedniej ilości. Samo „tak” odnosi się tylko do aktywnej, jednoznacznej decyzji.
- Zapis fizycznego wyniku zachowuje stan magazynu. Rozbieżność możesz zachować bez korekty albo przygotować i zatwierdzić osobną zmianę magazynu. Nierozstrzygnięte wcześniejsze różnice pozostają w sesji.
- **„Kończymy na dziś” / „przerwa”** zatrzymuje pomiar aktywnego czasu. **„Wznów liczenie”** kontynuuje tę samą sesję. Roczny spis zamyka się ostatecznie na ekranie.
- Przy tekście wysłanym podczas transkrypcji późniejszy rozpoznany tekst pozostaje w polu jako szkic do świadomego wysłania.

## Zamknięcie i wynik

Rozstrzygnij różnice i aktywne korekty, sprawdź ilości oraz wycenę i potwierdź zamknięcie. Niepoliczone SKU wymagają jawnego przyjęcia zera. Znaleziona ilość bez kosztu blokuje zamknięcie. Zamknięty spis utrwala źródła, metodę, koszty, ilości i daty.

Pobierz **PDF remanentu**, **Arkusz spisu z natury** i **Eksport wyniku do Rocznego Rozliczenia**. W Rocznym Rozliczeniu wybierz **Wczytaj wynik zamkniętego spisu**, sprawdź podgląd i zapisz. Ten sam wynik nie dodaje się drugi raz, a konflikt treści jest odrzucany.

## Płatności i komunikaty błędów

Agent może przygotować zmianę statusu płatności z wymaganą autoryzacją. Potwierdzenie lokalnego zapisu i synchronizacja z chmurą mają osobne stany. Dokument w trakcie publikacji nie jest traktowany jako gotowa należność. Zaległości pokazują waluty oddzielnie, chyba że dany moduł ma istniejącą jawną regułę przeliczenia.

Przy błędzie zachowaj identyfikator żądania/wykonania i kod błędu. Komunikat o problemie z odpowiedzią nie dowodzi braku zapisu. Instrukcje konfiguracji, kopii i wdrożenia: `ZIP47_CONFIGURATION_BACKUP_ROLLBACK.md` oraz `ZIP47_MIGRATIONS.md`.
