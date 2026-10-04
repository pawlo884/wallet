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
| `/odswiez` | ponowne pobranie kont i kategorii + przeładowanie `config/schedule.yaml` |
| `/whoami` | pokazuje Twoje ID (do konfiguracji) |

## Pamięć rozmowy i waluty

- Bot pamięta ostatnie ~30 minut rozmowy (najwyżej 8 wiadomości, tylko w RAM). Możesz więc dopowiadać
  („to wydatek”, „15$”) i poprawiać: „zmień na 45”, „to było wczoraj”, „kategoria restauracje”.
  Poprawka szkicu podmienia szkic. Poprawka zapisanego rekordu daje przycisk **✅ Zapisz poprawkę**,
  który usuwa starą wersję i zapisuje nową.
- Kwoty w obcej walucie („anthropic 15$”, „obiad 20 euro”) bot przelicza na walutę konta po średnim
  kursie NBP z dnia transakcji. Oryginał trafia do notatki, np. `15,00 USD po 3,8881 (NBP 02.10)`.

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
