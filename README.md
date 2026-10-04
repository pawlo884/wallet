# wallet-bot

Bot na Telegramie i Discordzie, który zapisuje wydatki i przychody do **BudgetBakers Wallet**.
Piszesz „biedronka 54,30” albo wysyłasz zdjęcie paragonu. Claude rozpoznaje kwotę, kategorię, sklep
i datę, bot pokazuje szkic, a po ✅ zapisuje rekord przez Wallet REST API. Działa też ↩️ *Cofnij*.

```
Telegram / Discord ──► kontener wallet-bot ──► Claude API   (parsowanie tekstu i paragonów)
                                         └──► Wallet API   (rest.budgetbakers.com/wallet)
```

Kontener łączy się wyłącznie na zewnątrz (long polling / gateway), więc **nie trzeba otwierać portów,
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
| `/saldo` (`!saldo` na Discordzie) | salda kont |
| `/miesiac` | przychody, wydatki, bilans, średnia dzienna i top kategorie w bieżącym miesiącu |
| `/zaplanowane` | płatności cykliczne: zaległe do potwierdzenia, najbliższe 30 dni i lista do odhaczenia opłaconych z góry |
| `/plan <opis>` | nowa płatność cykliczna, np. `/plan netflix 49 co miesiąc 15-go` (szkic + ✅ Dodaj) |
| `/plany` | wszystkie płatności cykliczne z przyciskami 🗑 do usuwania |
| `/kurs [kwota] [waluta] [na]` | kursy NBP; np. `/kurs`, `/kurs 100 eur`, `/kurs 50 usd eur` |
| `/wyciag <treść>` | uzgodnienie wyciągu z banku z Wallet (albo wklej / wyślij .eml) |
| `/dlug <nazwa> <kwota>` | śledzenie długu bez stałych rat, np. `/dlug A6 9100` |
| `/dlugi` | ile zostało do spłaty (pasek postępu), usuwanie z listy |
| `/prognoza` | prognoza na 12 miesięcy (przepływy, długi, oszczędności, wnioski) + link do strony z wykresami |
| `/odswiez` | ponowne pobranie kont i kategorii + przeładowanie `config/schedule.yaml` |
| `/whoami` | pokazuje Twoje ID (do konfiguracji) |

## Pamięć rozmowy i waluty

- Bot pamięta ostatnie ~30 minut rozmowy (najwyżej 8 wiadomości, tylko w RAM). Możesz więc dopowiadać
  („to wydatek”, „15$”) i poprawiać: „zmień na 45”, „to było wczoraj”, „kategoria restauracje”.
  Poprawka szkicu podmienia szkic. Poprawka zapisanego rekordu daje przycisk **✅ Zapisz poprawkę**,
  który usuwa starą wersję i zapisuje nową.
- Kwoty w obcej walucie („anthropic 15$”, „obiad 20 euro”) bot przelicza na walutę konta po średnim
  kursie NBP z dnia transakcji. Oryginał trafia do notatki, np. `15,00 USD po 3,8881 (NBP 02.10)`.

## Głosówki (mowa → tekst)

Wyślij wiadomość głosową na Telegramie albo Discordzie, np. *„biedronka pięćdziesiąt cztery trzydzieści”*.
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
- **udostępnij maila jako plik `.eml`** albo `.txt` (Telegram, Discord);
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

### 3a. Telegram
1. Napisz do **@BotFather** → `/newbot` → skopiuj token do `TELEGRAM_BOT_TOKEN`.
2. Uruchom bota, napisz do niego `/whoami` i wpisz zwrócone ID do `TELEGRAM_ALLOWED_USERS`. Zrestartuj bota.
3. Opcjonalnie w BotFather ustaw `/setcommands`:
   ```
   saldo - salda kont
   miesiac - podsumowanie miesiąca
   zaplanowane - zaległe i najbliższe płatności
   plan - dodaj płatność cykliczną
   plany - lista i usuwanie płatności
   odswiez - odśwież kategorie
   pomoc - pomoc
   ```

### 3b. Discord
1. https://discord.com/developers/applications → **New Application** → zakładka **Bot** → *Reset Token*,
   skopiuj go do `DISCORD_BOT_TOKEN`.
2. W tej samej zakładce włącz **Message Content Intent**.
3. **OAuth2 → URL Generator**: zakres `bot`, uprawnienia *Send Messages*, *Read Message History*,
   *View Channels*. Otwórz link i dodaj bota na swój serwer (DM wymagają wspólnego serwera).
4. Napisz do bota w DM `!whoami` i wpisz ID do `DISCORD_ALLOWED_USERS`.
   Jeśli bot ma działać też na kanale, w `DISCORD_CHANNEL_IDS` podaj ID kanału
   (Tryb dewelopera → PPM na kanale → Kopiuj ID).

> Bot obsługuje tylko użytkowników z listy `*_ALLOWED_USERS`. Przy pustej liście odpowiada wyłącznie na `whoami`.

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
  main.py          start obu botów w jednej pętli asyncio
  core.py          logika: szkic → zapis → cofnij, salda, podsumowanie miesiąca
  planned.py       płatności cykliczne: harmonogram, przypomnienia, stan
  parser.py        Claude (structured outputs) → lista rekordów
  wallet_api.py    klient Wallet REST API (paginacja, 409 sync, 429 limit)
  telegram_bot.py  adapter Telegram (python-telegram-bot)
  discord_bot.py   adapter Discord (discord.py)
  config.py        zmienne środowiskowe
```

## Uwagi

- Szkice, przycisk „Cofnij” i oczekiwanie na kwotę (✏️) są trzymane w pamięci, więc po restarcie kontenera stare przyciski przestają działać.
  Same rekordy w Wallet zostają.
- Limit Wallet API to 500 zapytań na godzinę. Bot zużywa 1 zapytanie na zapis i kilka na `/miesiac`.
- Kategorie i konta są cache'owane przez godzinę. Po zmianach w aplikacji użyj `/odswiez`.
