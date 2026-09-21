# Faza 3 — paczka produkcyjna względem ZIP 41

## Podmienić / dodać

- `agent_runtime.py`
- `app.py`
- `business_operations.py`
- `business_read_models.py`
- `dashboard_read.py` — nowy wspólny odczyt dashboardu
- `routes/admin.py`

## Usunąć z wdrażanej wersji

- `gunicorn.conf.py` — zawierał `gthread`; jego brak przy komendzie `gunicorn app:app` przywraca domyślny worker `sync`.

## Zakres

- istniejący SSE stream tekstu pozostaje jedynym transportem;
- `dashboard.read` korzysta z tej samej kalkulacji co pulpit;
- szybki remanent działa tylko dla jednoznacznej liczby w aktywnej sesji i używa istniejących `inventory.count.*` oraz approval `inventory.adjust`;
- brak zmian timeoutu, liczby workerów, schedulerów, TTS, RBAC i danych produkcyjnych.
