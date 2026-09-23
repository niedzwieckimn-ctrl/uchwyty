# Roczne Rozliczenie — ETAP 4: kontrola zgodności z magazynem

Kod źródłowy testowej aplikacji Windows. Ten ZIP nie zawiera pliku EXE.
Program portable może być zbudowany osobno przy użyciu dostarczonego speca.

## Co działa

- Nawigacja: Pulpit, Produkty, Importy, Zakupy, Sprzedaż, Magazyn, Remanent,
  Kontrola zgodności z magazynem, Rozliczenie roczne, Ustawienia.
- Kartoteka: dodawanie, edycja modelu/masy/statusu, wyszukiwanie, duplikaty.
- Import kartoteki i wartościowego stanu początkowego z CSV/XLSX: arkusz,
  mapowanie, podgląd, błędy, atomowe zatwierdzenie.
- Ręczny import: faktura, dostawca, waluty/kursy, pozycje z masą pobraną po
  SKU, podgląd kalkulacji, transport rozdzielany według masy, załącznik PDF.
- Lista i szczegóły importów oraz techniczne podsumowanie roczne.
- Kontrolne porównanie importów z przyjęciami ZIP46 oraz ręczny eksport CSV
  zgodnego z podglądem zakupów remanentu.

Nie ma jeszcze parsera PDF, wpisywania sprzedaży, spisu końcowego, finalnej
metody księgowej wyceny ani instalatora.

## Kontrola zgodności z magazynem

Ekran wczytuje snapshot JSON przyjęć. Nie łączy baz i nie zmienia danych
głównej aplikacji. Rok ogranicza importy RoczneRozliczenie; przyjęcia z
wybranego roku oraz przyjęcia z pasującym numerem dokumentu lub P/O są
porównywane. Filtr statusu i podsumowanie ułatwiają przegląd. Każdy wynik
zawiera uzasadnienie (`match_reason`) i pewność (`match_confidence`).

Snapshot należy wygenerować z **osobno przygotowanej, spójnej kopii** lokalnej
bazy SQLite ZIP46, a nie z pliku aktywnej bazy ani serwera produkcyjnego.
Przykład dla programisty/operatora, po uzyskaniu takiej kopii:

```powershell
python -m annual_inventory.warehouse_snapshot --sqlite-copy "C:\kopie\magazyn-copy.db" --out "C:\kopie\przyjecia-2026.json"
```

Pole `china_stock_receipts.quantities_json` jest źródłem ilości faktycznie
przyjętych. Nagłówki dokumentu/P/O pochodzą z `china_packages`, a SKU/nazwa z
`products` w tej samej kopii. Historyczne wiersze z `quantities_json=[]` są
pokazane jako przyjęcie o nieznanej ilości. Nie wyliczamy jej z aktualnej
zawartości P/O. Kwota `cost_amount` opisuje całą P/O, więc jest tylko
kontekstem i nie służy do automatycznego dopasowania pozycji.

Lokalna kopia może nie zawierać wszystkich danych widocznych w hybrydowym
magazynie SQLite/Supabase. Statusy są kontrolą tylko względem zawartości
wczytanego snapshotu. Przed decyzją o imporcie CSV trzeba upewnić się, że
snapshot jest kompletny i aktualny.

Przycisk eksportu zachowuje trzy podstawy wyceny: pełny landed cost, towar z
transportem oraz sam towar netto. Przed zapisem pokazuje ostrzeżenie o
potencjalnym podwójnym policzeniu, zwłaszcza dla `MATCHED`. Użytkownik może
świadomie potwierdzić. Program zapisuje wyłącznie CSV; w głównej aplikacji
należy ręcznie uruchomić podgląd importu remanentu. Nie ma automatycznego
importu, deduplikacji ani korekty magazynu.

## Dane użytkownika

`%LOCALAPPDATA%\RoczneRozliczenie`: baza w `database`, kopie PDF w `documents`,
log w `logs`, katalogi `backups` i `config`. Program tworzy je sam i wykonuje
migracje. Katalog aplikacji portable nie zawiera bazy. Wymiana aplikacji nie
usuwa danych; mimo to przed większymi aktualizacjami zaleca się kopię katalogu
danych. Automatycznych backupów jeszcze nie ma.

## Dla programisty

Python 3.12 i zależności z `pyproject.toml`; budowanie na Windows wymaga
PyInstaller 6.22.3 i konfiguracji `RoczneRozliczenie.spec`. Projekt testowano
z PySide6 6.11.2, SQLAlchemy 2.0.54 i Alembic 1.20.0. W izolowanym
środowisku deweloperskim:

```powershell
python -m unittest discover -s tests -v
pyinstaller --noconfirm RoczneRozliczenie.spec
```

Spec zawiera migracje i usuwa kolidujące biblioteki ICU, które mogą zostać
znalezione w ścieżkach narzędzi dokumentowych hosta budującego. Tych poleceń
nie wykonuje użytkownik końcowy.
