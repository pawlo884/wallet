"""Zamiana wiadomości (tekst i/lub zdjęcie paragonu) na rekordy Wallet przy pomocy Claude."""

import base64
import time
from datetime import date
from typing import Callable
from typing import Literal

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field


class ParsedRecord(BaseModel):
    amount: float = Field(description="Kwota dodatnia, w walucie z pola currency (albo konta, gdy currency=null)")
    currency: str | None = Field(
        description="Kod ISO 4217 waluty kwoty, jeśli użytkownik/paragon podaje walutę (USD, EUR, ...); inaczej null"
    )
    type: Literal["expense", "income"]
    category_id: str = Field(description="ID kategorii dokładnie z listy")
    account_id: str = Field(description="ID konta dokładnie z listy")
    date: str = Field(description="Data transakcji YYYY-MM-DD")
    counterparty: str | None = Field(description="Sklep / płatnik / odbiorca, jeśli znany")
    transfer_to: str | None = Field(
        default=None,
        description="ID konta docelowego z listy KONTA, gdy to przelew między kontami użytkownika "
        "(np. 'odłożyłem 500 na poduszkę', '300 na awaryjne'); inaczej null",
    )
    debt_id: str | None = Field(
        default=None, description="ID długu z listy DŁUGI, jeśli ten wydatek to spłata tego długu; inaczej null"
    )
    note: str = Field(
        description="ZAWSZE: krótki opis po polsku, na co konkretnie poszły pieniądze "
        "(np. 'doładowanie API Anthropic', 'kawa i ciastko', 'mleko, chleb, karma dla psa')"
    )


class TaskAction(BaseModel):
    action: Literal["add", "done", "postpone"] = Field(
        description="add = nowe zadanie; done = zadanie z listy ZADANIA zrobione; postpone = przesunięcie terminu"
    )
    task_id: int | None = Field(description="ID zadania z listy ZADANIA (dla done/postpone); dla add null")
    title: str = Field(description="Krótko, w bezokoliczniku, po polsku, np. 'Zadzwonić do ubezpieczyciela'")
    due: str | None = Field(description="Termin YYYY-MM-DD, jeśli wynika z wiadomości (dla postpone: nowy termin); inaczej null")
    time: str | None = Field(
        default=None,
        description="Godzina HH:MM, tylko gdy padła w wiadomości ('o 15' → '15:00', 'o wpół do 10' → '09:30'); inaczej null",
    )


class BookAction(BaseModel):
    action: Literal["add", "status", "progress"] = Field(
        description="add = nowa książka na liście; status = zmiana statusu książki z listy KSIĄŻKI; "
        "progress = postęp czytania książki z listy (strona / przeczytane strony / liczba stron)"
    )
    book_id: int | None = Field(description="ID książki z listy KSIĄŻKI (dla status i progress); dla add null")
    title: str = Field(description="Tytuł książki (bez autora)")
    author: str | None = Field(description="Autor, jeśli znany z wiadomości lub zdjęcia; inaczej null")
    status: Literal["to_buy", "to_read", "reading", "read"] = Field(
        description="to_buy = do kupienia, to_read = mam / kupiona, czeka na czytanie, reading = czytam, read = przeczytana"
    )
    page: int | None = Field(default=None, description="progress: strona, na której użytkownik jest teraz "
                             "(„jestem na 120 stronie”); inaczej null")
    pages_read: int | None = Field(default=None, description="progress: ile stron przeczytał od ostatniego razu "
                                   "(„przeczytałem 30 stron”); inaczej null")
    total_pages: int | None = Field(default=None, description="Liczba stron książki, jeśli użytkownik ją podał "
                                    "(„Diuna ma 600 stron”); inaczej null")


class BookSeen(BaseModel):
    title: str = Field(description="Tytuł dokładnie tak, jak widać na okładce (bez autora)")
    author: str | None = Field(description="Autor z okładki; inaczej null")
    status: Literal["to_buy", "to_read", "reading", "read"]


class BooksResult(BaseModel):
    books: list[BookSeen] = Field(default_factory=list)


BOOKS_INSTRUCTIONS = """Jakie książki pokazuje użytkownik na tych zdjęciach? Bierz książki, które są tematem
zdjęcia: okładka z przodu, trzymana w ręce, wskazywana palcem, na środku kadru, albo pozycje na zrzucie
listy/sklepu/półki. Kadr z filmiku (TikTok, Instagram) też się liczy. Pomijaj książki w tle, w stosach i same
grzbiety. Tytuł i autora przepisz z okładki; nie zgaduj — nieczytelny tytuł pomiń. Każda książka raz.
status: to_buy, chyba że podpis użytkownika mówi inaczej ("mam" → to_read, "czytam" → reading,
"przeczytałem" → read). Brak książek → pusta lista."""


class ParseResult(BaseModel):
    records: list[ParsedRecord]
    tasks: list[TaskAction] = Field(
        default_factory=list, description="Zadania do dodania / odhaczenia / przesunięcia (zwykle pusta lista)"
    )
    books: list[BookAction] = Field(
        default_factory=list, description="Książki do dodania / zmiany statusu (zwykle pusta lista)"
    )
    amends: bool = Field(
        description="true, gdy wiadomość poprawia/uzupełnia transakcję pokazaną wcześniej w rozmowie "
        "(szkic lub zapisany rekord) — wtedy records to PEŁNA poprawiona wersja tamtej transakcji"
    )
    question: str | None = Field(
        description="Jeśli nie da się ustalić transakcji (np. brak kwoty) — krótkie pytanie po polsku. "
        "Gdy użytkownik o coś pyta — tu krótka odpowiedź. Inaczej null."
    )


class PlanDraft(BaseModel):
    name: str = Field(description="Krótka nazwa płatności, np. 'Netflix', 'Czynsz'")
    amount: float = Field(description="Kwota dodatnia")
    type: Literal["expense", "income"]
    category_id: str = Field(description="ID kategorii dokładnie z listy")
    freq: Literal["MONTHLY", "YEARLY", "WEEKLY"]
    interval: int = Field(description="Co ile okresów (1 = każdy miesiąc/rok/tydzień, 3 = co kwartał przy MONTHLY)")
    first_date: str = Field(description="Data pierwszego/kolejnego terminu YYYY-MM-DD")
    count: int | None = Field(description="Liczba powtórzeń, jeśli ograniczona (np. 12 rat); inaczej null")
    counterparty: str | None = Field(description="Odbiorca/płatnik, jeśli podany")


class PlanResult(BaseModel):
    plan: PlanDraft | None
    question: str | None = Field(description="Gdy brakuje kwoty lub nie wiadomo co to — pytanie po polsku")


class InvestmentDraft(BaseModel):
    name: str = Field(description="Nazwa po polsku, np. 'Srebro', 'Złoto', 'ETF VWCE'")
    kind: Literal["metal", "ticker"]
    symbol: str = Field(description="Metal: XAG/XAU/XPT/XPD. Inaczej ticker z Yahoo Finance, np. VWCE.DE, IWDA.AS, CDR.WA")
    quantity: float = Field(description="Metal: w uncjach trojańskich (1 oz = 31,1035 g). Ticker: liczba sztuk")
    unit: str = Field(description="'oz' dla metali, 'szt.' dla tickerów")
    cost_pln: float | None = Field(description="Łączny koszt zakupu w PLN, jeśli podany; inaczej null")
    bought: str | None = Field(description="Kiedy kupione (rok lub data), jeśli podane; inaczej null")


class InvestmentResult(BaseModel):
    investment: InvestmentDraft | None
    question: str | None = Field(description="Gdy nie wiadomo co to lub ile — pytanie po polsku")


INVESTMENT_INSTRUCTIONS = """Tym razem użytkownik dodaje INWESTYCJĘ do śledzenia (nie transakcję), np.
"srebro 2 uncje kupione w 2024 za 600 zł", "złoto 10 g", "VWCE 3 sztuki", "Orlen 20 akcji".
- Metale: kind=metal, symbol XAG (srebro), XAU (złoto), XPT (platyna), XPD (pallad); quantity w uncjach
  trojańskich — gramy przelicz (1 oz = 31,1035 g); "uncja"/"oz" bez dopisku = uncja trojańska.
- ETF/akcje: kind=ticker, symbol w formacie Yahoo Finance (giełda warszawska: .WA, Xetra: .DE).
- cost_pln: tylko gdy podano cenę zakupu (przelicz na łączną kwotę w PLN; gdy podano za sztukę — pomnóż).
"""


PLAN_INSTRUCTIONS = """Tym razem NIE zapisujesz transakcji, tylko definiujesz PŁATNOŚĆ CYKLICZNĄ
(stałe zlecenie, abonament, rata, pensja), np. "netflix 49 co miesiąc 15-go",
"czynsz 1800 10-tego", "OC 1200 co rok 20 marca", "rata 450 do lutego", "pensja 6200 10-go".

- Domyślnie co miesiąc (freq=MONTHLY, interval=1). "co kwartał" = MONTHLY + interval 3.
- first_date: najbliższy termin od dzisiaj (włącznie). Jeśli podano tylko dzień miesiąca
  ("15-go") — najbliższy taki dzień. Bez dnia — dzisiaj.
- count: tylko gdy liczba powtórzeń wynika z wiadomości ("12 rat", "do lutego" → policz terminy).
- Pensja/wypłata/zwrot = income. Reszta = expense.
- name: zwięźle, z wielkiej litery, po polsku tak jak użytkownik nazwał płatność.
"""


STATEMENT_INSTRUCTIONS = """Tym razem dostajesz WYCIĄG / ZESTAWIENIE OPERACJI z banku — zwykle
przekazany mail (z nagłówkami, stopką, reklamami). Wyodrębnij KAŻDĄ zaksięgowaną operację jako rekord:
- date: data operacji (transakcji), a gdy jest tylko data księgowania — ta.
- amount: kwota w walucie rachunku (dodatnia), type: obciążenie = expense, uznanie = income.
  Jeśli operacja była w obcej walucie, użyj kwoty PO przeliczeniu przez bank (w walucie rachunku), currency=null.
- counterparty: oczyszczona nazwa odbiorcy/nadawcy ("ZAKUP PRZY UŻYCIU KARTY ... BIEDRONKA 1234 WARSZAWA"
  → "Biedronka"), bez numerów kart, miast i kodów.
- note: krótko po polsku, na co to prawdopodobnie poszło (jak przy zwykłych wpisach).
- category_id: najlepiej pasująca kategoria z listy.
- POMIŃ: salda, sumy, limity, blokady/autoryzacje oczekujące, reklamy, stopki.
amends=false. Jeśli tekst nie jest wyciągiem — records=[] i krótko wyjaśnij w question.
"""


SYSTEM_TEMPLATE = """Jesteś asystentem, który zapisuje transakcje do aplikacji finansowej Wallet.
Użytkownik pisze po polsku, skrótowo, np. "biedronka 54,30", "paliwo 250 orlen wczoraj",
"wypłata 6200", "kawa 14 i ciastko 9". Może też przysłać zdjęcie paragonu lub potwierdzenia.

Zasady:
- Każda osobna transakcja to osobny rekord. Paragon to zwykle JEDEN rekord na sumę do zapłaty;
  rozbij go tylko, gdy użytkownik o to prosi albo pozycje wyraźnie należą do bardzo różnych kategorii.
- amount zawsze dodatnie; type mówi, czy to wydatek, czy przychód. Domyślnie wydatek.
- category_id i account_id wybieraj WYŁĄCZNIE z list poniżej. Wybierz najbardziej szczegółową
  pasującą kategorię. Jeśli nic nie pasuje, użyj kategorii "Unknown"/"Nieznane" odpowiedniego typu.
- Konto: jeśli użytkownik go nie wskazał, użyj konta domyślnego. Waluta w wiadomości
  (np. "20 euro", "€", "15$") wskazuje konto w tej walucie, jeśli takie istnieje.
- Waluta: gdy kwota jest w walucie (USD, EUR, ...), wpisz ją do currency i podaj amount w TEJ walucie,
  nawet jeśli nie ma konta w tej walucie — bot sam przeliczy po kursie NBP. Nie pytaj o kwotę w PLN.
- Konta z listy to WYŁĄCZNIE pieniądze użytkownika. "Doładowanie/zasilenie konta" w zewnętrznej usłudze
  (np. Anthropic, OpenAI, Steam, telefon na kartę, karta miejska) to zwykły WYDATEK na tę usługę
  (counterparty = usługa), nie przelew między kontami.
- Nie dopytuj, gdy znasz kwotę: kategorię, datę i konto wybierz sam najlepiej jak umiesz —
  użytkownik i tak widzi szkic i może go poprawić.
- Daty względne ("wczoraj", "w piątek") licz od dzisiejszej daty podanej w wiadomości.
  Brak daty = dzisiaj. Nigdy data w przyszłości.
- counterparty: nazwa sklepu/firmy/osoby z wiadomości lub paragonu, w mianowniku i naturalnej formie
  ("w sowie" → "Sowa", "na orlenie" → "Orlen"). Gdy nie podano — null; nie wymyślaj ogólników
  w stylu "Kawiarnia" czy "Sklep".
- note: ZAWSZE wypełnij. To ma pozwolić użytkownikowi za kilka miesięcy przypomnieć sobie,
  na co dokładnie poszły pieniądze — kategoria i sklep tego nie mówią. 2–8 słów, po polsku,
  konkretnie: co kupione / za co zapłacone / dla kogo / po co, np. "doładowanie API Anthropic",
  "obiad z Karoliną", "paliwo do A6", "prezent dla mamy". Zachowaj szczegóły z wiadomości
  użytkownika. Przy paragonie wymień główne pozycje (max ~6), np. "mleko, chleb, masło, karma dla psa".
  Gdy wiadomość nic nie mówi ponad sklep ("biedronka 54,30"), opisz ogólnie: "zakupy spożywcze".
- Gry losowe: użytkownik gra TYLKO w Eurojackpot ("lotto", "wpłata na lotto.pl", "kupon" = Eurojackpot).
  Kupon/wpłata = expense, wygrana = income, obie w kategorii loterii/hazardu ("Lottery, gambling"),
  note np. "kupon Eurojackpot", "wygrana Eurojackpot" — bot liczy z nich bilans (/lotto).
- Gdy wiadomość nie opisuje transakcji, zadania ani książki albo brak kwoty: records = [] i zadaj pytanie
  w question.
- Gdy użytkownik o coś PYTA ("skąd się wzięło…", "co dodałeś?", "ile mam zadań?", "dlaczego…") —
  odpowiedz krótko po polsku w question na podstawie rozmowy i list poniżej; records, tasks i books puste.
  Jeśli coś dodałeś przez pomyłkę, przyznaj to i powiedz, że można to cofnąć przyciskiem ↩️ albo usunąć w /ksiazki.

Jesteś też osobistym asystentem: prowadzisz listę ZADAŃ i listę KSIĄŻEK (obie poniżej, w osobnym bloku).
- Zadanie ("jutro zadzwonić do ubezpieczyciela", "do soboty wymienić opony", "zadanie: oddać książkę",
  "muszę kupić prezent dla mamy") → tasks action=add, records=[]. "Muszę kupić X" bez kwoty to zadanie,
  nie wydatek. due tylko gdy termin wynika z wiadomości ("jutro", "w piątek", "do 20-go").
  time tylko przy konkretnej godzinie ("jutro o 15 zadzwonić do ZUS" → due jutro, time 15:00); sama
  godzina bez dnia ("o 18 trening") → due dziś, a jeśli ta godzina już minęła — jutro.
- "zrobione: …", "zadzwoniłem do ubezpieczyciela", "odhacz …" dla zadania z listy → action=done z task_id.
  "przesuń … na poniedziałek" → action=postpone z task_id i nowym due (i time, jeśli padła godzina).
- Książka ("chcę przeczytać Atomowe nawyki", "książka: Sapiens", "do kupienia: Diuna") → books action=add.
  Status ZAWSZE to_buy, chyba że użytkownik SAM napisze inaczej w tekście wiadomości (podpis pod screenem
  też się liczy): "mam", "kupiona", "kupiłem", "do przeczytania" → to_read; "czytam", "zacząłem" → reading;
  "przeczytałem", "skończyłem" → read. To, co widać na obrazku (półka, biblioteka w Legimi/Kindle/Empik,
  przycisk „Czytaj”, pasek postępu), NIE zmienia statusu — screen bez podpisu to zawsze to_buy.
- Kilka zdjęć naraz to jedna wiadomość: każdą książkę dodaj raz.
- Ze zdjęcia bierz TYLKO książki, które są głównym tematem: okładka z przodu, trzymana w ręce, wskazywana
  palcem albo na środku kadru, z czytelnym tytułem. Pomijaj książki w tle, w stosach, na półkach i same
  grzbiety, chyba że cały screen to lista/półka. Nie zgaduj ani nie dopowiadaj tytułów: tylko to, co da się
  przeczytać; tytuł nieczytelny → pomiń. Kadr z filmiku (TikTok, Instagram, YouTube), na którym ktoś
  pokazuje książkę, to też książka do dodania — pomijasz tylko napisy samego interfejsu (lajki, przyciski).
- Zrzut ekranu lub zdjęcie z książkami (okładka, strona sklepu, lista, półka), które nie jest
  potwierdzeniem zakupu → tylko books (każda widoczna książka osobno, z autorem, jeśli widać), records=[].
  Potwierdzenie zakupu / paragon z książkami → records (wydatek) ORAZ books ze statusem to_read.
- Książka, która już jest na liście KSIĄŻKI → action=status z book_id (np. kupiona → to_read), nie add.
  Zakup książki z listy ("kupiłem Atomowe nawyki 45 zł") → wydatek w records + books status to_read.
- Postęp czytania ("przeczytałem 30 stron Diuny", "jestem na 120 stronie", "Diuna ma 600 stron",
  "dziś 20 stron") → books action=progress z book_id książki z listy: page (strona, na której jest) ALBO
  pages_read (ile stron przybyło), total_pages, gdy poda liczbę stron; status=reading (read, gdy skończył).
  Bez tytułu → książka, którą czyta (status reading na liście KSIĄŻKI); kilka takich → zapytaj w question.
  Książki nie ma na liście → action=add ze status=reading i tym samym page/pages_read.
- Pytanie o czytanie ("ile stron przeczytałem w tym tygodniu?") → odpowiedz w question z bloku CZYTANIE.
- Zadania i książki to nie transakcje: nie twórz dla nich rekordów i nie pytaj o kwotę.

Rozmowa: widzisz kilka ostatnich wiadomości. Używaj ich jako kontekstu:
- Odpowiedź na Twoje pytanie albo dopowiedzenie ("to wydatek", "15$", "wczoraj") łącz z wcześniejszą
  wiadomością w jedną transakcję.
- Poprawka szkicu/zapisanego rekordu ("zmień na 45", "kategoria restauracje", "to było wczoraj",
  "nie Biedronka tylko Lidl") → amends=true i PEŁNA poprawiona lista rekordów tamtej transakcji.
- Nowa transakcja ("i jeszcze parking 12") → amends=false i records zawiera WYŁĄCZNIE transakcje
  z bieżącej wiadomości. Nigdy nie powtarzaj rekordów z wcześniejszych szkiców — one nadal czekają
  osobno na zatwierdzenie i zostałyby zapisane podwójnie.

KONTA (id | nazwa | waluta):
{accounts}

Konto domyślne: {default_account}

- Przelew między WŁASNYMI kontami z listy KONTA ("odłożyłem 500 na poduszkę", "300 na awaryjne",
  "przelałem z awaryjnego 200 na ogólne") → account_id = konto źródłowe (domyślnie konto domyślne),
  transfer_to = konto docelowe, type = expense, category_id = dowolne z listy (zostanie pominięte).
  To nie jest wydatek. Doładowanie konta w zewnętrznej usłudze to nadal zwykły wydatek.
- Spłata długu z listy DŁUGI ("spłata A6 500", "oddałem tacie 200", przelew z tytułem zawierającym
  nazwę długu) → debt_id = id tego długu, type = expense. Inne wydatki → debt_id = null.

DŁUGI (id | nazwa | rata, jeśli jest — przy podobnych nazwach dopasuj też po kwocie):
{debts}

KATEGORIE (id | nazwa | kategoria nadrzędna):
{categories}
"""


class RecordParser:
    def __init__(self, model: str):
        self._client = AsyncAnthropic()
        self._model = model
        # (nazwa wywołania, model, usage, ms) → dziennik zdarzeń; ustawia Core
        self.on_usage: Callable[[str, str, object, int], None] | None = None

    async def _call(self, name: str, **kw):
        started = time.monotonic()
        response = await self._client.messages.parse(model=self._model, **kw)
        if self.on_usage:
            self.on_usage(name, self._model, response.usage, int((time.monotonic() - started) * 1000))
        return response

    async def parse(
        self,
        text: str,
        images: list[tuple[bytes, str]],
        today: date,
        catalog_prompt: str,
        history: list[dict] | None = None,
        memory: str = "",
        lists: str = "",
    ) -> ParseResult:
        """history: wcześniejsze tury [{"role": "user"|"assistant", "content": str}], od najstarszej."""
        content: list[dict] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.standard_b64encode(data).decode(),
                },
            }
            for data, media_type in images
        ]
        content.append(
            {
                "type": "text",
                "text": f"Dzisiaj jest {today.isoformat()} ({_WEEKDAYS[today.weekday()]}).\n\n"
                f"Wiadomość: {text or '(tylko zdjęcie)'}",
            }
        )
        response = await self._call(
            "parse",
            max_tokens=4000,
            system=_system(catalog_prompt, memory, lists),
            messages=[*(history or []), {"role": "user", "content": content}],
            output_format=ParseResult,
        )
        if response.stop_reason == "refusal" or response.parsed_output is None:
            return ParseResult(
                records=[], amends=False, question="Nie udało mi się tego odczytać. Napisz np. „biedronka 54,30”."
            )
        return response.parsed_output

    async def parse_books(self, images: list[tuple[bytes, str]], text: str) -> list[BookAction]:
        """Druga próba, gdy główny parser nic nie znalazł na zdjęciach: samo pytanie o książki."""
        content: list[dict] = [
            {"type": "image", "source": {"type": "base64", "media_type": mt,
                                         "data": base64.standard_b64encode(data).decode()}}
            for data, mt in images
        ]
        content.append({"type": "text", "text": f"{BOOKS_INSTRUCTIONS}\n\nPodpis użytkownika: {text or '(brak)'}"})
        response = await self._call(
            "parse_books", max_tokens=1500, messages=[{"role": "user", "content": content}], output_format=BooksResult,
        )
        if response.stop_reason == "refusal" or response.parsed_output is None:
            return []
        return [BookAction(action="add", book_id=None, title=b.title, author=b.author, status=b.status)
                for b in response.parsed_output.books if b.title.strip()]

    async def parse_statement(self, text: str, today: date, catalog_prompt: str, memory: str = "") -> ParseResult:
        response = await self._call(
            "parse_statement",
            max_tokens=16000,  # wyciąg może mieć kilkadziesiąt operacji
            system=_system(catalog_prompt, memory),
            messages=[
                {
                    "role": "user",
                    "content": f"{STATEMENT_INSTRUCTIONS}\nDzisiaj jest {today.isoformat()}.\n\n"
                    f"<wyciag>\n{text[:60000]}\n</wyciag>",
                }
            ],
            output_format=ParseResult,
        )
        if response.stop_reason == "refusal" or response.parsed_output is None:
            return ParseResult(records=[], amends=False, question="Nie udało się odczytać wyciągu.")
        return response.parsed_output

    async def parse_investment(self, text: str, today: date) -> InvestmentResult:
        response = await self._call(
            "parse_investment",
            max_tokens=1000,
            messages=[{"role": "user", "content": f"{INVESTMENT_INSTRUCTIONS}\nDzisiaj jest {today.isoformat()}.\n\nWiadomość: {text}"}],
            output_format=InvestmentResult,
        )
        if response.stop_reason == "refusal" or response.parsed_output is None:
            return InvestmentResult(investment=None, question="Nie zrozumiałem. Napisz np. „srebro 2 uncje kupione 2024 za 600 zł”.")
        return response.parsed_output

    async def parse_plan(self, text: str, today: date, catalog_prompt: str) -> PlanResult:
        response = await self._call(
            "parse_plan",
            max_tokens=2000,
            # Ten sam prefiks co przy rekordach (cache), instrukcje planu w wiadomości.
            system=[{"type": "text", "text": catalog_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[
                {
                    "role": "user",
                    "content": f"{PLAN_INSTRUCTIONS}\nDzisiaj jest {today.isoformat()} "
                    f"({_WEEKDAYS[today.weekday()]}).\n\nWiadomość: {text}",
                }
            ],
            output_format=PlanResult,
        )
        if response.stop_reason == "refusal" or response.parsed_output is None:
            return PlanResult(plan=None, question="Nie zrozumiałem. Napisz np. `/plan netflix 49 co miesiąc 15-go`.")
        return response.parsed_output


def _system(catalog_prompt: str, memory: str, lists: str = "") -> list[dict]:
    # Katalog kont/kategorii zmienia się rzadko — stabilny prefiks do cache. Pamięć sprzedawców
    # zmienia się po każdym zapisie, więc osobny blok za nim (katalog zostaje w cache).
    # Zadania i książki zmieniają się najczęściej — na końcu, bez cache.
    blocks = [{"type": "text", "text": catalog_prompt, "cache_control": {"type": "ephemeral"}}]
    if memory:
        blocks.append({"type": "text", "text": memory, "cache_control": {"type": "ephemeral"}})
    if lists:
        blocks.append({"type": "text", "text": lists})
    return blocks


_WEEKDAYS = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]
