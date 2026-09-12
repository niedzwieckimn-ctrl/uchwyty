# Agent Runtime — przepływ i eksploatacja

Runtime pracuje na jawnej historii, korzysta z Business Operations i odpowiada zwykłym tekstem. Nie interpretuje wypowiedzi użytkownika ani twierdzeń liczbowych po stronie Pythona.

## Przepływ turnu

1. Backend ponownie ładuje wewnętrzną tożsamość człowieka i AI, sprawdza własność rozmowy oraz zakłada krótką blokadę turnu.
2. Zapisuje wypowiedź użytkownika, buduje ograniczoną historię i dołącza terminologię oraz opcjonalne preferencje komunikacji. Dodaje aktualny czas Europe/Warsaw jako punkt odniesienia dla modelu.
3. Responses API otrzymuje historię jawnie, `store=false`, narzędzia z rejestru i `tool_choice=auto`.
4. Model może odpowiedzieć, dopytać albo wywołać operację. Backend sprawdza dostępność narzędzia i uprawnienia inicjującego człowieka, a następnie wywołuje istniejący execution gate Business Operations.
5. Model dostaje wynik lub kontrolowany błąd narzędzia i generuje zwykły tekst. Możliwe są kolejne operacje, jeśli pytanie faktycznie ich wymaga.
6. Runtime zapisuje odpowiedź i dowody narzędziowe, zwalnia blokadę oraz zapisuje audit i pomiary czasu.

Jedna zwykła odpowiedź bez narzędzi wymaga jednego wywołania modelu. Prosty odczyt: dwie odpowiedzi modelu, jedna operacja biznesowa. Nie ma dodatkowej rundy klasyfikowania intencji, walidowania liczb ani streszczania.

## Limity i historia

- Wiadomość użytkownika: 2000 znaków. Odpowiedź: maksymalnie 8000 znaków; przekroczenie zwraca kontrolowany błąd zamiast cichego ucięcia.
- Historia: do sześciu ostatnich zakończonych turnów, łącznie do 12 000 bajtów JSON.
- Oryginalne pary USER/ASSISTANT i kompletne grupy function_call/function_call_output. Duża grupa narzędziowa może zostać pominięta, ale pozostaje para oryginalnych wypowiedzi, jeśli mieści się w limicie. Historia nigdy nie jest syntetycznym wyborem „aktywnego produktu”.
- Nie ma semantycznego selektora „istotnych” wiadomości ani automatycznego streszczania przez osobny model. Okno jest oparte na kolejności i rozmiarze.
- Limit wyniku pojedynczego narzędzia: 16 000 bajtów. Przekroczenie zwraca modelowi informację o konieczności zawężenia zapytania, zamiast nieoznaczonego fragmentu danych.
- Cały dynamiczny input turnu: 48 000 bajtów. Maksymalnie sześć operacji; potem model może tylko wygenerować tekst. Timeout pojedynczego requestu modelu: 30 sekund.
- TTL rozmowy: 45 minut bez aktywności. Wygaśnięcie usuwa jej tekst przy ponownym otwarciu. Reset czyści historię, zachowując audit i pamięć firmy.
- Blokada turnu w SQLite działa między procesami, wygasa po pięciu minutach w razie awarii workera. Reset aktywnej rozmowy jest odrzucany.
- Redakcja sekretów pozostaje wyjątkiem od verbatim. Brak analizy liczb, dat biznesowych, zaimków i polskich zwrotów.

## Pamięć terminologii

`agent.terminology.search` odczytuje termin i wersję. To wyszukiwanie tekstu po nazwie terminu, bez interpretacji zdania.

`agent.terminology.remember` jest osobną operacją WRITE typu GREEN dla metadanych AI. Wymaga dedykowanego permission `agent.terminology.remember`, dostępnego domyślnie właścicielowi i AI Owner Assistant. Handler ponownie sprawdza uprawnienia inicjującego człowieka. Nie nadaje żadnych uprawnień do zapisu zamówień, faktur, stanów ani wysyłki.

LLM ocenia, czy użytkownik jasno wyjaśnił lub potwierdził definicję. Przy niepewności powinien dopytać. Backend nie analizuje słowa „tak” ani innych zwrotów. Sprawdza strukturę argumentów, `confirmed_by_user is True`, własność aktywnego turnu, źródło ustawione przez runtime, wersję i idempotency. Model nie może wskazać innego source_run_id. Jedno źródło nie zwiększa ponownie wersji przy powtórzeniu identycznego zapisu.

Zapisywane pola: term, meaning, scope=company, source=confirmed_by_user, source_run_id, confirmed_by_actor_id, version, updated_at. Zmiana znaczenia wymaga aktualnej wersji; audit przechowuje poprzednie i nowe znaczenie. Jeśli polityka Approval Engine wymaga akceptacji, pamięć nie zostaje zapisana. Runtime nie posiada narzędzia do zatwierdzania.

Semantyczne potwierdzenie jest oceną modelu, nie kryptograficznym dowodem zgody użytkownika. Błędne rozpoznanie potwierdzenia przez LLM należy sprawdzić w testach z rzeczywistym modelem; nie zastąpiono go parserem językowym.

Limit: 100 terminów, nazwa do 80 znaków, znaczenie do 500 znaków. Do promptu trafia do 4000 bajtów pamięci łącznie ze stylem; dalsze terminy są dostępne przez operację wyszukiwania. Zakres company oznacza jedną firmę w jednej bazie SQLite, zgodnie z obecną instalacją. Nie jest to wdrożenie wielofirmowe.

`internal_agent_user_style` jest lekkim punktem rozszerzenia na preferencje komunikacji użytkownika. Nie dodano profilowania ani automatycznego uczenia stylu. Brak publicznego endpointu zapisującego styl.

## Migracja

Plik `migrations/agent_runtime_history.sql` jest addytywny i idempotentny. Tworzy cztery nowe tabele wewnętrzne: `internal_agent_turns`, `internal_agent_turn_leases`, `internal_agent_terminology`, `internal_agent_user_style`. Zachowuje istniejącą tabelę `internal_agent_conversations`; stare state_json nie jest już odczytywane jako kontekst i jest zerowane przy otwarciu rozmowy. Nie rekonstruuje rozmów z syntetycznego stanu.

Istniejący `init_db()` wywołuje `initialize_agent_conversation_schema()`, który wczytuje ten plik. Trzeba wdrożyć go razem z kodem. Przy uruchomieniu nowego kodu na docelowej lokalnej bazie powstaną nowe tabele oraz zostaną zainicjalizowane nowe wewnętrzne permission/policies przez dotychczasowy mechanizm aplikacji. Sam plik SQL nie inicjalizuje RBAC/policies — robi to `init_db()`.

Nie jest to migracja Supabase i nie należy wykonywać jej w panelu klienta. Na produkcji nie wykonano żadnej migracji. Przed wdrożeniem potrzebna jest kopia lokalnej bazy i sprawdzenie trwałości wolumenu: historia i terminologia nie są synchronizowane do Supabase. Pełna retencja/archiwizacja nieaktywnych rozmów wymaga osobnej decyzji eksploatacyjnej; ograniczenie promptu nie zastępuje retencji bazy.

## Dane, bezpieczeństwo i ograniczenia

Usunięto refresh wszystkich tabel z endpointu AI i jego nieużywany helper. Pozostała synchronizacja aplikacji jest bez zmian. Operacje czytają bieżący lokalny model odczytu; nie ma gwarancji, że odpowiada on stanowi zdalnemu w tej samej chwili. Jeśli dana operacja potrzebuje silniejszego freshness, należy dodać je na poziomie jej odczytu danych.

Obecne operacje zapisu biznesowego pozostają ukryte przed runtime. Istniejące RBAC, risk classification, Approval Engine, wersjonowanie, audity, idempotency i obsługa wykonania zewnętrznego nie zostały usunięte ani zastąpione. Zmiany rejestrów są ograniczone do pamięci AI. Do `orders.search` dodano tylko strukturalny filtr product_id, aby móc znaleźć ostatniego nabywcę produktu.

Agent nie ma nowej operacji wyliczającej kompletną gotowość zamówienia do wysyłki. Może odczytać zamówienie, pozycje i stany; gdy brakuje danych o pełnych regułach wysyłki, powinien jasno powiedzieć o ograniczeniu zamiast potwierdzać gotowość na podstawie samej liczby sztuk.

Panel klienta, jego auth, HTML/JS, flow i moduły biznesowe nie są zmieniane. Jedyną zmianą w app.py jest usunięcie AI-only refreshu i jego helpera. Nie dodano klientom AI, voice ani internal RBAC.

## Provider i diagnostyka

Zachowane zmienne: `OPENAI_API_KEY`, `AI_OWNER_MODEL`, opcjonalnie `AI_OWNER_ACTOR_ID`. Runtime nie ustawia ani nie zmienia modelu za operatora. Responses API otrzymuje `store=false` i pełne potrzebne input items; previous_response_id nie jest wysyłane. Zaszyfrowane reasoning items są jawnie przenoszone wraz z wywołaniami narzędzi. Narzędzia mają proste schematy wejściowe i `strict=false`, z autorytatywną walidacją w Business Operations. Zgodność protokołu oparto na [oficjalnej dokumentacji function calling](https://developers.openai.com/api/docs/guides/function-calling).

Response endpointu zawiera timings: context_build_ms, first_model_call_ms, business_operation_ms (suma), final_model_call_ms (końcowa odpowiedź po narzędziach; zero dla odpowiedzi w pierwszym wywołaniu), total_ms, tool_calls_count. Ten sam zestaw jest logowany jako AI_TURN_TIMING. Nie wymaga to dodatkowego requestu modelu.

## Weryfikacja przed wdrożeniem

Testy lokalne z FakeModelProvider sprawdzają protokół i bezpieczeństwo, nie jakość językową. Testy z prawdziwym modelem nie zostały wykonane, ponieważ środowisko nie ma OPENAI_API_KEY ani AI_OWNER_MODEL. Przed wdrożeniem na staging należy przejść osiem rozmów z briefu, w tym warianty z literówkami, mieszanym językiem, zmianą omawianego produktu, dwoma zamówieniami oraz nauką terminu po niepewnej wypowiedzi. Należy mierzyć pełne czasy modelu i sprawdzić poprawność wybranych ID/operacji w audycie. Nie ma uczciwej podstawy, by wyprowadzić produkcyjne opóźnienie LLM z testów dostawcy testowego.

Uruchomienie regresji: `python -m pytest -q`. Sam runtime: `python -m pytest -q test_agent_runtime.py test_agent_conversation.py test_agent_history_runtime.py test_agent_assistant_ui.py`.

Na Windows do testów użyto zależności z requirements.txt oraz pytest i tzdata, bez serwerowych awsgi/gunicorn. awsgi ciągnie uvloop, które nie buduje się na Windows. Produkcyjne requirements.txt pozostaje bez zmian.
