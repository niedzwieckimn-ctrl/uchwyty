# Lokalne porównanie STT

ZIP47 zawierał test importujący `compare_voice_stt.py`, ale brakowało samego pliku. Dodano mały CLI wykorzystujący istniejący `OpenAIVoiceIOProvider`.

```powershell
python compare_voice_stt.py recording.webm --models gpt-4o-mini-transcribe gpt-4o-transcribe
```

Klucz jest pobierany z `OPENAI_API_KEY`, tak jak w aplikacji. Model trzeba wskazać jawnie. Nazwa podana w poleceniu ani test z atrapą nie potwierdzają dostępności modelu na koncie. Uruchomienie polecenia wysyła to samo nagranie do każdego wskazanego modelu i może naliczać koszt API.

CLI zachowuje format żądania istniejącego adaptera: polski `language` i wspólny `STT_CONTEXT_PROMPT`. Wybieraj modele zgodne z tym adapterem. Inne rodziny modeli mogą wymagać zmiany kontraktu, np. osobnego pola `languages` zamiast `language`; tego porównywacz nie dopisuje automatycznie. [Dokumentacja transkrypcji OpenAI](https://developers.openai.com/api/docs/guides/speech-to-text).

Wynik to JSONL na standardowym wyjściu: model, status, czas żądania i transkrypcja albo bezpieczny kod błędu. Kod zakończenia: 0 — wszystkie odpowiedzi udane, 1 — co najmniej jeden błąd dostawcy, 2 — nieprawidłowe wejście. Limit wejścia wynosi 10 MB, zgodnie z aplikacją. Program nie zapisuje kopii audio i nie korzysta z mechanizmu debug audio.

W teście zmieniono niejawne porównanie z `gpt-transcribe` na jawne `--models`. Test sprawdza przekazanie wybranych nazw, identyczne audio, język i prompt, bez wnioskowania o jakości lub dostępności modeli. Produkcyjnych wywołań w ramach naprawy nie wykonano.

Stare testy strony Asystenta również zaktualizowano do istniejących kontraktów: ograniczony payload z `voice_fast_mode`, deterministyczny odczyt fizycznego stock bez modelu oraz dwa obecne wejścia TTS (wczesny `speech_ready` i tekst końcowy). Kontrole braku danych uprawnień w żądaniu oraz bezpieczeństwa renderowania pozostają aktywne; wyścigi Voice i recovery weryfikują osobne testy JavaScript.
