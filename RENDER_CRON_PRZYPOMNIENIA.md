# Automatyczne przypomnienia o płatności

W Render utwórz nowy **Cron Job** z tego samego repozytorium i brancha co aplikacja.

- Schedule: `0 * * * *`
- Build Command: `pip install -r requirements.txt`
- Start/Command: `python run_payment_reminders.py`

Skopiuj do Cron Job te same zmienne środowiskowe co w usłudze WWW, w szczególności:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `SUPABASE_STORAGE_BUCKET`
- `RESEND_API_KEY`
- `EMAIL_FROM`
- `ADMIN_EMAIL`
- `EMAIL_ENABLED=1`
- `CLIENT_PANEL_URL`

Cron jest wywoływany co godzinę, lecz skrypt wysyła wiadomości tylko o godzinie
12:00 czasu `Europe/Warsaw`. Przypomnienie obejmuje wszystkie nieopłacone faktury,
których termin minął najpóźniej poprzedniego dnia. Dzięki temu awaria lub brak
uruchomienia zadania jednego dnia nie powoduje trwałego pominięcia faktury.
Opłacone faktury i faktury z już wysłanym przypomnieniem są pomijane.

Do jednorazowego testu można użyć polecenia:

`python run_payment_reminders.py --force`

Test wybiera wszystkie zaległe faktury, które nie mają jeszcze wysłanego przypomnienia.
