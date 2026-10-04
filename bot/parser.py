"""Zamiana wiadomości (tekst i/lub zdjęcie paragonu) na rekordy Wallet przy pomocy Claude."""

import base64
from datetime import date
from typing import Literal

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field


class ParsedRecord(BaseModel):
    amount: float = Field(description="Kwota dodatnia, w walucie konta")
    type: Literal["expense", "income"]
    category_id: str = Field(description="ID kategorii dokładnie z listy")
    account_id: str = Field(description="ID konta dokładnie z listy")
    date: str = Field(description="Data transakcji YYYY-MM-DD")
    counterparty: str | None = Field(description="Sklep / płatnik / odbiorca, jeśli znany")
    note: str | None = Field(description="Krótka notatka, jeśli wnosi coś ponad kategorię")


class ParseResult(BaseModel):
    records: list[ParsedRecord]
    question: str | None = Field(
        description="Jeśli nie da się ustalić transakcji (np. brak kwoty) — krótkie pytanie po polsku. Inaczej null."
    )


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
  (np. "20 euro", "€") wskazuje konto w tej walucie, jeśli takie istnieje.
- Daty względne ("wczoraj", "w piątek") licz od dzisiejszej daty podanej w wiadomości.
  Brak daty = dzisiaj. Nigdy data w przyszłości.
- counterparty: nazwa sklepu/firmy/osoby z wiadomości lub paragonu, w naturalnej formie ("Biedronka").
- note: tylko jeśli dodaje informację (np. "prezent dla mamy"); inaczej null.
- Gdy wiadomość nie opisuje transakcji albo brak kwoty: records = [] i zadaj pytanie w question.

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
    ) -> ParseResult:
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
            messages=[{"role": "user", "content": content}],
            output_format=ParseResult,
        )
        if response.stop_reason == "refusal" or response.parsed_output is None:
            return ParseResult(records=[], question="Nie udało mi się tego odczytać. Napisz np. „biedronka 54,30”.")
        return response.parsed_output


_WEEKDAYS = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]
