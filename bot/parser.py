"""Zamiana wiadomości (tekst i/lub zdjęcie paragonu) na rekordy Wallet przy pomocy Claude."""

import base64
from datetime import date
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
    note: str = Field(
        description="ZAWSZE: krótki opis po polsku, na co konkretnie poszły pieniądze "
        "(np. 'doładowanie API Anthropic', 'kawa i ciastko', 'mleko, chleb, karma dla psa')"
    )


class ParseResult(BaseModel):
    records: list[ParsedRecord]
    amends: bool = Field(
        description="true, gdy wiadomość poprawia/uzupełnia transakcję pokazaną wcześniej w rozmowie "
        "(szkic lub zapisany rekord) — wtedy records to PEŁNA poprawiona wersja tamtej transakcji"
    )
    question: str | None = Field(
        description="Jeśli nie da się ustalić transakcji (np. brak kwoty) — krótkie pytanie po polsku. Inaczej null."
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
- counterparty: nazwa sklepu/firmy/osoby z wiadomości lub paragonu, w naturalnej formie ("Biedronka").
- note: ZAWSZE wypełnij. To ma pozwolić użytkownikowi za kilka miesięcy przypomnieć sobie,
  na co dokładnie poszły pieniądze — kategoria i sklep tego nie mówią. 2–8 słów, po polsku,
  konkretnie: co kupione / za co zapłacone / dla kogo / po co, np. "doładowanie API Anthropic",
  "obiad z Karoliną", "paliwo do A6", "prezent dla mamy". Zachowaj szczegóły z wiadomości
  użytkownika. Przy paragonie wymień główne pozycje (max ~6), np. "mleko, chleb, masło, karma dla psa".
  Gdy wiadomość nic nie mówi ponad sklep ("biedronka 54,30"), opisz ogólnie: "zakupy spożywcze".
- Gdy wiadomość nie opisuje transakcji albo brak kwoty: records = [] i zadaj pytanie w question.

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

KATEGORIE (id | nazwa | kategoria nadrzędna):
{categories}
"""


class RecordParser:
    def __init__(self, model: str):
        self._client = AsyncAnthropic()
        self._model = model

    async def parse(
        self,
        text: str,
        images: list[tuple[bytes, str]],
        today: date,
        catalog_prompt: str,
        history: list[dict] | None = None,
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
        response = await self._client.messages.parse(
            model=self._model,
            max_tokens=4000,
            # Katalog kont/kategorii zmienia się rzadko — stabilny prefiks do cache.
            system=[{"type": "text", "text": catalog_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[*(history or []), {"role": "user", "content": content}],
            output_format=ParseResult,
        )
        if response.stop_reason == "refusal" or response.parsed_output is None:
            return ParseResult(
                records=[], amends=False, question="Nie udało mi się tego odczytać. Napisz np. „biedronka 54,30”."
            )
        return response.parsed_output

    async def parse_plan(self, text: str, today: date, catalog_prompt: str) -> PlanResult:
        response = await self._client.messages.parse(
            model=self._model,
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


_WEEKDAYS = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]
