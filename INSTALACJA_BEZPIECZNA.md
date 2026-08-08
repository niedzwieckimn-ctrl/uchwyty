# Bezpieczna instalacja — jedna firma

Każda sprzedana kopia powinna mieć osobny projekt Supabase, usługę Render,
witrynę Netlify i komplet nowych sekretów. Nie wolno kopiować sekretów z firmy
sprzedającego do instalacji klienta.

## 1. Supabase

1. Utwórz osobny projekt Supabase dla firmy.
2. W SQL Editor uruchom dotychczasowy schemat aplikacji.
3. Uruchom `supabase_order_idempotency.sql`.
4. Uruchom `supabase_client_security.sql`.
5. W Authentication utwórz konta użytkowników panelu klienta. Adres e-mail konta
   musi być taki sam jak `customer_email` w zamówieniu.
6. Skopiuj Project URL, anon key i service_role key. Service role jest tajny i
   trafia wyłącznie do Render. Nigdy nie wklejaj go do HTML panelu klienta.

## 2. Hasło panelu magazynu

Lokalnie, w katalogu backendu, uruchom:

```powershell
python generate_admin_password_hash.py
```

Program poprosi o hasło i wypisze hash. Do Render wklejasz hash, a nie hasło.

## 3. Zmienne w Render

Ustaw co najmniej:

- `FLASK_SECRET_KEY` — losowy sekret, minimum 32 znaki; inny dla każdej firmy,
- `ADMIN_USERNAME` — login administratora,
- `ADMIN_PASSWORD_HASH` — wynik generatora z poprzedniego kroku,
- `SUPABASE_URL` — adres projektu tej firmy,
- `SUPABASE_SERVICE_ROLE_KEY` — tajny service_role tej firmy,
- `CLIENT_ALLOWED_ORIGINS` — dokładny adres panelu Netlify, bez ukośnika na końcu,
- `RESEND_API_KEY`, `EMAIL_FROM`, `ADMIN_EMAIL` — ustawienia poczty tej firmy.

Nie ustawiaj `ADMIN_PASSWORD`, jeśli używasz `ADMIN_PASSWORD_HASH`.
Po zmianie sekretów wykonaj redeploy.

## 4. Panel klienta

W `index.html` ustaw `SUPABASE_URL`, publiczny `SUPABASE_ANON_KEY` oraz
`MAGAZYN_API_BASE` odpowiadające tej instalacji. Anon key może znajdować się w
przeglądarce; bezpieczeństwo danych zapewniają logowanie, RLS i backend.

## 5. Kontrola po wdrożeniu

1. Wejście na adres Render ma przekierować do logowania magazynu.
2. Błędne hasło nie może zalogować użytkownika.
3. Klient A nie może zobaczyć zamówień ani faktur klienta B.
4. Złożenie zamówienia ma utworzyć tylko jeden rekord także po podwójnym kliknięciu.
5. Potwierdzenie ma wyjść z backendu; przycisk ponownej wysyłki działa w magazynie.
6. Stary endpoint `/api/client_order_email` ma zwracać HTTP 410.

## Ważne operacyjnie

- Włącz MFA na kontach właściciela Render, Supabase, Netlify i Resend.
- Nie przesyłaj service_role ani sekretów e-mailem bez szyfrowania.
- Przed przekazaniem instalacji zmień wszystkie sekrety na należące do klienta.
- Wykonuj kopie bazy i testuj ich odtworzenie.
- Aktualizacje bezpieczeństwa aplikacji trzeba później dostarczać każdej instalacji.

