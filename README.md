# wallet-bot

Bot na Telegramie, który zapisuje wydatki i przychody do **BudgetBakers Wallet**.
Piszesz „biedronka 54,30” albo wysyłasz zdjęcie paragonu. Claude rozpoznaje kwotę, kategorię, sklep
i datę, bot pokazuje szkic, a po ✅ zapisuje rekord przez Wallet REST API. Działa też ↩️ *Cofnij*.

```
Telegram ──► kontener wallet-bot ──► Claude API   (parsowanie tekstu i paragonów)
                                         └──► Wallet API   (rest.budgetbakers.com/wallet)
```

Kontener łączy się wyłącznie na zewnątrz (long polling), więc **nie trzeba otwierać portów,
domeny ani HTTPS** na VPS.

## Co umie

| Wiadomość | Efekt |
|---|---|
| `biedronka 54,30` | wydatek, Zakupy spożywcze, Biedronka, dziś |
| `paliwo 250 orlen wczoraj` | wydatek z wczorajszą datą |
| `kawa 14 i ciastko 9` | dwa rekordy |
| `wypłata 6200` | przychód |
| `obiad 20 euro` | rekord na koncie w EUR |
| 📷 zdjęcie paragonu | suma z paragonu, sklep, data |
| `/saldo` | salda kont |
| `/miesiac` | przychody, wydatki, bilans, średnia dzienna i top kategorie w bieżącym miesiącu |
| `/zaplanowane` | płatności cykliczne: zaległe do potwierdzenia, najbliższe 30 dni i lista do odhaczenia opłaconych z góry |
| `/plan <opis>` | nowa płatność cykliczna, np. `/plan netflix 49 co miesiąc 15-go` (szkic + ✅ Dodaj) |
| `/plany` | wszystkie płatności cykliczne z przyciskami 🗑 do usuwania |
| `/kurs [kwota] [waluta] [na]` | kursy NBP; np. `/kurs`, `/kurs 100 eur`, `/kurs 50 usd eur` |
| `/wyciag <treść>` | uzgodnienie wyciągu z banku z Wallet (albo wklej / wyślij .eml) |
| `/dlug <nazwa> <kwota>` | śledzenie długu bez stałych rat, np. `/dlug A6 9100` |
| `/dlugi` | ile zostało do spłaty (pasek postępu), usuwanie z listy |
| `/prognoza` | prognoza na 12 miesięcy (przepływy, długi, oszczędności, wnioski) + link do strony z wykresami |
| `/korekta [konto] <kwota>` | saldo jak w banku: różnica jako wpis „Korekta salda” albo zmiana salda początkowego |
| `/inwestycja <opis>` | nowa inwestycja, np. `/inwestycja srebro 2 uncje kupione 2024 za 600 zł` |
| `/inwestycje` | wycena na żywo w PLN (metale: gold-api.com, ETF/akcje: Yahoo, kurs NBP), zysk/strata |
| `/multisport` | czy karta Multisport się opłaca: wejścia ze Stravy, koszt wejścia, próg opłacalności |
| `/lotto` | Eurojackpot: ile wydane, ile wygrane, zwrot, szanse na wygraną |
| `/strava` | jednorazowe połączenie konta Strava (OAuth) |
| `/google` | jednorazowe połączenie z Kalendarzem Google (OAuth) |
| `/kalendarz` | dziś i jutro: spotkania z Kalendarza Google i zadania z terminem |
| `/pamiec` | zapamiętani sprzedawcy (kategoria, konto) i zapominanie |
| `jutro zadzwonić do ubezpieczyciela` | nowe zadanie z terminem (bez transakcji) |
| `/zadania` | zaległe, dziś, najbliższe i bez terminu; przyciski ☑️ zrobione / ⏭ jutro / 📅 za tydzień |
| `/zadanie <opis>` | nowe zadanie wprost, np. `/zadanie do soboty wymienić opony` |
| 📷 screen z książkami | książki na listę (okładka, strona sklepu, półka w Legimi/Empik/Kindle) |
| `/ksiazki` | czytam (z postępem), do przeczytania, do kupienia, przeczytane; strony dziś/tydzień/miesiąc/rok |
| `przeczytałem 30 stron Diuny` | postęp czytania (też `jestem na 120 stronie`, `Diuna ma 600 stron`) |
| `/ksiazka <tytuł>` | nowa książka wprost, np. `/ksiazka Diuna Frank Herbert` |
| `/log` | dziennik: ostatnie wiadomości, przyciski, wywołania modelu i koszt w tym miesiącu |
| `/odswiez` | ponowne pobranie kont i kategorii + przeładowanie `config/schedule.yaml` |
| `/whoami` | pokazuje Twoje ID (do konfiguracji) |

## Pamięć rozmowy i waluty

- Bot pamięta ostatnie ~30 minut rozmowy (najwyżej 8 wiadomości, tylko w RAM). Możesz więc dopowiadać
  („to wydatek”, „15$”) i poprawiać: „zmień na 45”, „to było wczoraj”, „kategoria restauracje”.
  Poprawka szkicu podmienia szkic. Poprawka zapisanego rekordu daje przycisk **✅ Zapisz poprawkę**,
  który usuwa starą wersję i zapisuje nową.
- Kwoty w obcej walucie („anthropic 15$”, „obiad 20 euro”) bot przelicza na walutę konta po średnim
  kursie NBP z dnia transakcji. Oryginał trafia do notatki, np. `15,00 USD po 3,8881 (NBP 02.10)`.

## Pamięć sprzedawców (uczenie się z wpisów)

- Po każdym zapisie (✅) bot zapamiętuje sprzedawcę: nazwę, typ, kategorię, konto i notatkę.
  Kolejna wiadomość albo paragon z tego samego miejsca („julka 8”, zdjęcie paragonu z Piekarni Julka)
  dostaje tę samą nazwę, kategorię i konto, a notatkę w podobnym stylu. Szkic pokazuje wtedy
  `🧠 Jak ostatnio: Piekarnia Julka`.
- Najnowszy zapis wygrywa: poprawka („kategoria restauracje” → ✅ Zapisz poprawkę) od razu zmienia pamięć.
- Przy pierwszym starcie pamięć wypełnia się historią z Wallet z ostatniego roku (najczęstsza
  kategoria i konto dla każdego sprzedawcy), więc działa też dla wpisów dodanych w aplikacji.
- `/pamiec` pokazuje najczęstszych sprzedawców i pozwala zapomnieć wybranego. Plik: `data/learned.json`
  na wolumenie (usunięcie go = ponowne wczytanie historii z Wallet przy następnym starcie).

## Zadania i książki

Pierwszy krok do osobistego asystenta (plan: `plan-asystenta.md` w plikach projektu). Piszesz normalnie,
a ten sam model, który rozpoznaje wydatki, rozpoznaje też zadania i książki. Jedna wiadomość może mieć
wszystko naraz: „kupiłem Atomowe nawyki 45 zł” to szkic wydatku do ✅ i od razu zmiana statusu książki
na 📚 *do przeczytania*.

- **Zadania** trafiają na listę od razu (z ↩️ Cofnij), z terminem, jeśli wynika z wiadomości („jutro”,
  „w piątek”, „do 20-go”). Odhaczasz zdaniem („zadzwoniłem do ubezpieczyciela”) albo w `/zadania`.
  Bot liczy przesunięcia; przy trzecim pyta, czy rozbić zadanie albo je skreślić.
- **Książki** mają cztery statusy: 🛒 do kupienia → 📚 do przeczytania → 📖 czytam → ✅ przeczytane.
  Nowa książka jest zawsze 🛒 *do kupienia*, chyba że dopiszesz inaczej (np. podpis pod screenem „mam”,
  „czytam”, „przeczytana”). Co widać na screenie (półka w Legimi, Kindle), statusu nie zmienia.
- **Okładki** w panelu bot znajduje sam po tytule i autorze (Google Books, a gdy nie ma — Open Library),
  w tle zaraz po dodaniu książki. Brak okładki = kafelek z tytułem. Przy okazji bierze liczbę stron.
- **Postęp czytania**: napisz, ile stron przeczytałeś albo na której jesteś. Każdy przyrost trafia do
  dziennika czytania (tabela `reading`), z którego bot liczy strony na dziś, tydzień, miesiąc i rok
  (`/ksiazki`, panel, pytanie „ile stron przeczytałem w tym tygodniu?”). Ostatnia strona albo ✅ przeczytana
  = książka przeczytana, z resztą stron doliczoną do statystyk.
  Każda widoczna książka osobno, z autorem. Ta sama książka drugi raz nie jest dublowana.

### Kalendarz Google

Po jednorazowym `/google` zadania z terminem są wydarzeniami w osobnym kalendarzu **„Zadania”** (bot
zakłada go sam): bez godziny jako całodniowe, z godziną („jutro o 15 zadzwonić do ZUS”) jako 30 minut.
Odhaczenie, przesunięcie i usunięcie — w bocie albo w panelu — zmienia wydarzenie; zrobione zostają
w kalendarzu jako „✅ …” na szaro. Bot czyta też Twoje widoczne kalendarze: dzisiejsze spotkania są
w `/zadania`, dziś i jutro w `/kalendarz`, 7 dni w panelu, a przy zadaniu z godziną bot ostrzega
„⚠️ O 15:00 masz już: …”. Działa też w drugą stronę: wydarzenie dodane w kalendarzu „Zadania” to nowe
zadanie, usunięte usuwa zadanie, przeniesione zmienia termin, zmieniona nazwa tytuł, a „✅” na początku
nazwy odhacza (zdjęte otwiera ponownie). Wydarzenia powtarzające się nie są zamieniane na zadania. Bot
sprawdza co 5 minut i przy `/zadania`, `/kalendarz` i w panelu.

Konfiguracja (raz):
1. https://console.cloud.google.com → nowy projekt → *APIs & Services → Library* → **Google Calendar API** → Enable.
2. *OAuth consent screen*: User type **External**, nazwa dowolna, Twój e-mail; potem **Publish app**
   (w trybie *Testing* Google unieważnia dostęp po 7 dniach). Weryfikacja nie jest potrzebna.
3. *Credentials → Create credentials → OAuth client ID*, typ **Desktop app**.
4. `GOOGLE_CLIENT_ID` i `GOOGLE_CLIENT_SECRET` do `.env`, `docker compose up -d`.
5. W bocie `/google` → przycisk → zgoda (przy „aplikacja niezweryfikowana”: *Zaawansowane → Przejdź*)
   → skopiuj adres `http://localhost/?code=…` z paska → `/google <adres>`.

Tokeny i ID kalendarza leżą w `data/google.json` na wolumenie. Błędy synchronizacji widać w `/log`
i w panelu (agent „kalendarz”); bot działa dalej bez kalendarza.

## Baza i dziennik zdarzeń

Zadania, książki i dziennik leżą w `data/assistant.db` (SQLite) na wolumenie `wallet-data`, więc przetrwają
restart i przebudowę. Dziennik zapisuje każdą wiadomość, przycisk, komendę, wiadomość wysłaną przez bota
i każde wywołanie modelu: który „agent” obsłużył sprawę (wydatki / zadania / ksiazki), tokeny, koszt w USD
i czas. Wpisy jednej wiadomości łączy wspólny `trace`. Wpisy starsze niż 180 dni są usuwane przy starcie.

- `/log` pokazuje ostatnie 15 zdarzeń i koszt modelu dziś i w tym miesiącu.
- `LLM_BUDGET_USD` w `.env` (np. `5`) włącza miesięczny limit: po przekroczeniu bot raz w miesiącu
  wysyła ostrzeżenie. 0 albo puste = bez limitu.
- Podgląd z serwera: `docker compose exec wallet-bot python -c "import sqlite3; [print(dict(r)) for r in
  sqlite3.connect('data/assistant.db').execute('select * from events order by id desc limit 5')]"`.

## Panel WWW (React)

Pod `/panel/` na tym samym adresie co prognoza (np. `https://wallet.sowa.ch/panel/`) bot serwuje panel
w React (Vite + Tailwind, ten sam styl co Life OS):

- **Zadania**: dodawanie z terminem, odhaczanie, „Jutro” / „+7 dni”, usuwanie, zrobione z ostatniego tygodnia.
- **Książki**: kolumny czytam / do przeczytania / do kupienia, przycisk następnego kroku („Kupiona →”,
  „Zaczynam →”, „Przeczytana →”), dodawanie i usuwanie.
- **Wydatki**: przychody, wydatki, bilans i największe kategorie w tym miesiącu (z Wallet) + link do prognozy.
- **Co robi bot**: koszt modelu dziś i w miesiącu (z limitem), mapa aktywności z 13 tygodni, koszt dzienny,
  dziennik zdarzeń z filtrami (wiadomości, model, przyciski, komendy, od bota, panel, błędy); kliknięcie
  wiersza pokazuje wejście i odpowiedź.

Panel odświeża się co minutę, więc zmiany z Telegrama widać bez przeładowania. Zmiany z panelu trafiają
do dziennika jako zdarzenia `panel`. Nie ma własnego logowania: dostęp ustawia lista dostępu w NPM, tak jak
dla prognozy. API przyjmuje zapisy tylko jako JSON (`/api/v1/...`, `bot/panel_api.py`).

Front buduje się w pierwszym etapie `Dockerfile` (`frontend/` → `bot/static/panel`). Lokalnie:
`cd frontend && npm install && npm run dev` (zapytania `/api` idą do bota na `localhost:8080`,
inny adres: `BOT_URL=http://… npm run dev`).

## Głosówki (mowa → tekst)

Wyślij wiadomość głosową na Telegramie, np. *„biedronka pięćdziesiąt cztery trzydzieści”*.
Bot rozpoznaje mowę **lokalnie na serwerze** (faster-whisper, model `small`, polski), pokazuje
transkrypt 🎤 i dalej działa jak przy zwykłej wiadomości: szkic, ✅, poprawki.

- Nagrania nie wychodzą z serwera i nic nie kosztują.
- Model (~0,5 GB) pobiera się raz, w tle przy pierwszym starcie, do wolumenu `wallet-data`.
- Kontener ma limit 1,5 GB RAM. Ustawienia `STT_MODEL` (tiny/base/small/medium), `STT_THREADS`
  i `STT_ENABLED` są w `.env`.

## Wyciągi z banku (uzgadnianie)

Bot porównuje wyciąg z wpisami w Wallet i przysyła **tylko brakujące operacje** jako szkice
(✅ Zapisz / ❌ Pomiń). Operacje pasujące do niepotwierdzonej płatności cyklicznej dostajesz jako
przypomnienie z ✅ Zapłacone. Dopasowanie: kwota + data ±3 dni (zakup a księgowanie), a dla wpisów
przeliczonych kursem NBP tolerancja 4% (bank liczy po swoim kursie).

Jak podać wyciąg:
- **wklej treść maila** do bota (długi tekst z wieloma kwotami rozpozna sam) albo `/wyciag <treść>`;
- **udostępnij maila jako plik `.eml`** albo `.txt`;
- **przekaż maila na skrzynkę bota**: bot sprawdza ją co `MAIL_POLL_MINUTES` min przez IMAP. Ustaw w poczcie
  filtr auto-przekazywania wyciągów z banku, a wszystko będzie działo się samo.

Konfiguracja skrzynki (`.env`): `MAIL_USER`, `MAIL_PASSWORD` (hasło aplikacji), `MAIL_ALLOWED_FROM`.
Maile od innych nadawców są tylko pokazywane w bocie (np. kod weryfikacyjny przekierowania z Gmaila),
nie są przetwarzane.

## Długi bez stałych rat

`/dlug A6 9100` zakłada dług i etykietę „Dług: A6” w Wallet. Spłaty wpisujesz normalnie („spłata A6 500”,
głosówką, albo przychodzą z wyciągu). Claude rozpoznaje spłatę, a bot przypina etykietę i od razu pokazuje,
ile zostało. Stan = kwota początkowa − suma wydatków z tą etykietą od dnia dodania, więc działa też etykieta
dodana ręcznie w aplikacji. Lista: `data/debts.json` na wolumenie.

## Prognoza i oszczędności

Prognoza liczy się na żywo z płatności cyklicznych, rat, długów i sald kont, więc zmiana planu
(`/plan`, `config/schedule.yaml`) albo długu od razu zmienia wynik. To, czego bot nie wie
(kwota „na życie” w trzech wariantach, cele i miesięczne wpłaty na konta **Awaryjne** i **Poduszka
finansowa**), ustawiasz w `config/forecast.yaml`. Po pushu deploy działa sam.

- `/prognoza` w bocie: skrót z najważniejszymi wnioskami.
- Strona z wykresami i tabelą: kontener wystawia ją na porcie 8080 tylko w sieci Nginx Proxy Managera
  (`nginx_proxy_manager_network`). W NPM dodaj Proxy Host, np. `wallet.sowa.ch → http://wallet-bot:8080`,
  z listą dostępu `internal-panels`. Adres wpisz do `.env` jako `FORECAST_URL`, wtedy bot podaje link.
- Wpłaty na oszczędności to przelewy: napisz „300 na awaryjne”. Bot zapisze przelew, a nie wydatek.

## Płatności cykliczne

Bot zastępuje „transakcje zaplanowane” z Wallet: API Wallet pozwala je tylko czytać, więc nie da się
ich potwierdzać zdalnie. Lista płatności jest w pliku `config/schedule.yaml` (wzór pól: `config/schedule.example.yaml`).

W dniu terminu, od godziny `REMINDER_HOUR`, bot wysyła przypomnienie:

> 📅 **Dziś:** Ubezpieczenie −300,00 PLN  [✅ Zapłacone] [✏️ Inna kwota] [⏭ Pomiń]

- **✅** zapisuje rekord z datą terminu.
- **✏️** prosi o kwotę: odpisujesz np. `312,40`.
- **⏭** oznacza termin jako pominięty.

Nowe płatności dodajesz też z poziomu bota (`/plan …`). Trafiają do `data/payments.json` na wolumenie,
nie do repo. `/plany` → 🗑 usuwa płatność dodaną przez bota albo wyłącza tę z pliku.

Niepotwierdzone terminy przypominają się codziennie do skutku. Stan (co zapłacone, a co pominięte)
leży w wolumenie `wallet-data`, więc przetrwa restart i przebudowę. „Cofnij” po zapisie przywraca
termin na listę.

## Konfiguracja krok po kroku

### 1. Token Wallet
Wallet Web (web.budgetbakers.com) → **Settings → API token** → wygeneruj. Wymaga planu Premium.

### 2. Klucz Claude
console.anthropic.com → API Keys. Domyślny model `claude-haiku-4-5` kosztuje grosze miesięcznie
przy kilku wpisach dziennie. Jeśli paragony będą źle odczytywane, zmień `CLAUDE_MODEL` na
`claude-sonnet-5-5`.

### 3. Telegram
1. Napisz do **@BotFather** → `/newbot` → skopiuj token do `TELEGRAM_BOT_TOKEN`.
2. Uruchom bota, napisz do niego `/whoami` i wpisz zwrócone ID do `TELEGRAM_ALLOWED_USERS`. Zrestartuj bota.
3. Listy komend nie trzeba ustawiać w BotFather: bot sam ją rejestruje przy starcie (podpowiedzi po „/” i menu).


> Bot obsługuje tylko użytkowników z listy `TELEGRAM_ALLOWED_USERS`. Przy pustej liście odpowiada wyłącznie na `/whoami`.

## Uruchomienie na VPS

```bash
# na VPS
git clone <repo> wallet && cd wallet     # albo: scp -r wallet pawel@192.168.50.31:~/
cp .env.example .env && nano .env
docker compose up -d --build
docker compose logs -f
```

Aktualizacja: automatycznie po pushu na `main` (sekcja CI/CD), ręcznie: `git pull && docker compose up -d --build`.

W Portainerze: *Stacks → Add stack → Repository* (albo wklej `docker-compose.yml`) i zmienne z `.env`
wpisz w sekcji *Environment variables*.

## CI/CD (GitHub Actions)

`.github/workflows/deploy.yml`:
- **każdy push i PR**: build obrazu i testy offline. Sprawdza też poprawność `config/schedule.yaml`.
- **push na `main`**: po zielonych testach łączy się z VPS przez SSH, robi `git reset --hard` na nowy commit
  i `docker compose up -d --build`, a potem sprawdza, czy kontener działa bez pętli restartów.
  Jeśli coś jest nie tak, job jest czerwony, a w logu zobaczysz ostatnie 30 linii z kontenera.

Jednorazowa konfiguracja (te same sekrety co w repo `nc`):

| Sekret (Settings → Secrets and variables → Actions) | Wartość |
|---|---|
| `VPS_HOST` | host/IP VPS osiągalny z internetu |
| `VPS_USER` | `pawel` |
| `VPS_SSH_KEY` | prywatny klucz SSH z dostępem do VPS |

Opcjonalnie zmienna `DEPLOY_PATH` (zakładka *Variables*), domyślnie `/home/pawel/apps/wallet`.
Bez sekretów workflow robi tylko testy i pomija deploy.

Zmiana płatności cyklicznych = edycja `config/schedule.yaml` i push. Bot sam wczytuje nowy plik
(katalog `config/` jest podmontowany, zmiana wykrywana po dacie modyfikacji).

## Lokalnie (Windows)

```bash
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   # uzupełnij
python -m bot.main
```

Test bez sieci (atrapy Wallet i Claude): `python -m tests.test_offline`

## Struktura

```
bot/
  main.py          start: Telegram, pętla przypomnień, strona prognozy, skrzynka z wyciągami
  core.py          logika: szkic → zapis → cofnij, salda, podsumowanie miesiąca, korekta salda
  session.py       szkice i przyciski na wolumenie (przeżywają restart)
  memory.py        pamięć sprzedawców: kategoria i konto jak ostatnio (data/learned.json)
  store.py         baza asystenta SQLite: zadania, książki, dziennik zdarzeń (data/assistant.db)
  lists.py         zadania i książki: akcje z modelu, /zadania, /ksiazki, przyciski, cofanie
  events.py        dziennik: ceny modeli, koszt wywołania, trace jednej wiadomości
  parser.py        Claude (structured outputs) → lista rekordów
  planned.py       płatności cykliczne: harmonogram, przypomnienia, stan
  debts.py         długi bez stałych rat i kredyty ratalne (etykiety w Wallet)
  forecast.py      prognoza na 12 miesięcy
  web.py           strona prognozy i panel /panel/ (port 8080, sieć NPM)
  panel_api.py     JSON API panelu: zadania, książki, dziennik, podsumowanie (/api/v1)
  statement.py     wyciągi z banku: uzgadnianie z Wallet, skrzynka IMAP
  investments.py   metale i ETF/akcje z wyceną na żywo
  multisport.py    opłacalność karty Multisport
  lotto.py         Eurojackpot: bilans, zwrot, szanse (kategoria Lottery, gambling)
  strava.py        połączenie ze Stravą (OAuth, aktywności)
  gcal.py          Kalendarz Google (OAuth, kalendarz „Zadania”, odczyt wydarzeń, kolizje)
  fx.py            kursy NBP
  stt.py           mowa → tekst (faster-whisper, lokalnie)
frontend/          panel w React (Vite + Tailwind), budowany w Dockerfile
  wallet_api.py    klient Wallet REST API (paginacja, 409 sync, 429 limit)
  telegram_bot.py  adapter Telegram (python-telegram-bot)
  config.py        zmienne środowiskowe
  templates/       szablon strony prognozy
```

## Uwagi

- Szkice, „Cofnij”, korekty salda i oczekiwanie na kwotę (✏️) są zapisywane w `data/session.json` na wolumenie,
  więc przyciski działają też po restarcie i deployu. Szkice wygasają po dobie, „Cofnij” po 7 dniach.
  Pamięć rozmowy (ostatnie 30 min) jest tylko w RAM i znika przy restarcie.
- Limit Wallet API to 500 zapytań na godzinę. Bot zużywa 1 zapytanie na zapis i kilka na `/miesiac`.
- Kategorie i konta są cache'owane przez godzinę. Po zmianach w aplikacji użyj `/odswiez`.
