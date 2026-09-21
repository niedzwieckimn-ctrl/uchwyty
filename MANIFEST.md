# Remanent i odczyt wysyłek — tylko pliki do podmiany

Baza: bieżący katalog ZIP41-phase2 po poprzednich zmianach fazy 3 i KSeF.
Przed podmianą porównaj hash produkcyjnego pliku z kolumną przed; jeśli się różni, przejrzyj changes.patch.
Brak gunicorn.conf.py i zmian Gunicorna, TTS lub schedulerów.

| Plik | SHA-256 przed | SHA-256 po |
|---|---|---|
| app.py | `b892e8f5c100f19fdb6e45d8b1a5ad69a304c43f809a98034ae8f823257c55f9` | `d73a87a10ac5b950459b4db75908aa3610ea66473443259f862860c76fdba5d1` |
| agent_runtime.py | `938ddc159d0ed98979ce1c22678983eb57dd6f01d9c65d41f8c7feaabbc0e277` | `54affbdc78c93176344e319f42bfae127411dd0395424b4c81389863b30ede4b` |
| business_operations.py | `b0b37ce6a69bc7e65100e7429e15958e75c072bb21a5feefb0f6907b12301bd7` | `6f78fb02e87de805c7d73d714c99e6460b60803228381634cb9a678cd9385d74` |
| packing_history.py | `03368461c59ba4d82c8ede63af60b79d1eb4e2f0b489beb17253b7a6c728aaa2` | `49671eb2bac6b6e4d5991ed3e6f5ae6898cad2474a0e5070d7c2f6f1dc278486` |
| migrations/warehouse_operations.sql | `23feebf29aab4c32dcc71e693faefb319059ba7630eb1784ee3ec8be3d12952d` | `affb26859b6e1bd87564464560a72d78ca534c5fa1ac2422181db66c63cb4fdc` |

Testy zmienione w repozytorium (nie do wdrożenia): test_warehouse_ux.py, test_packing_history_read.py, test_phase2_regressions.py.
Testy: 192 passed, 9 deselected w szerszym przebiegu; 100 passed w końcowym przebiegu obu ścieżek, potem 3 passed dla migracji i odczytu PDF.
SQLite: aktywny produkt w istniejącej sesji dodaje idempotentnie business_operations.initialize_schema przy init_db. Nowa baza ma pole w migrations/warehouse_operations.sql. Nie modyfikować danych ręcznie.
Lokalny sztuczny provider: referencja modelowa 202.24 ms, fast path 196.39 ms; to nie jest pomiar sieci/TTS. Model calls 2+1 vs 0+0. Approval HTTP 46.93 ms.
Zweryfikowana lista ma pierwszeństwo; przy braku listy fallback jest oznaczony. Niezweryfikowany PDF nie uruchamia fallbacku.
