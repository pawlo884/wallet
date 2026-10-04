"""Kursy walut NBP (tabela A, kurs średni) — bez klucza API: https://api.nbp.pl"""

import logging
import re
from datetime import date, timedelta

import httpx

log = logging.getLogger(__name__)

NBP = "https://api.nbp.pl/api/exchangerates/rates"

# Potoczne nazwy → kody ISO (dla /kurs; w rekordach walutę rozpoznaje Claude).
ALIASES = {
    "$": "USD", "usd": "USD", "dolar": "USD", "dolary": "USD", "dolarów": "USD", "dolarow": "USD",
    "€": "EUR", "eur": "EUR", "euro": "EUR",
    "£": "GBP", "gbp": "GBP", "funt": "GBP", "funty": "GBP", "funtów": "GBP", "funtow": "GBP",
    "chf": "CHF", "frank": "CHF", "franki": "CHF", "franków": "CHF", "frankow": "CHF",
    "zł": "PLN", "zl": "PLN", "pln": "PLN", "złotych": "PLN", "zlotych": "PLN",
    "czk": "CZK", "korony": "CZK", "koron": "CZK",
}


class FXError(Exception):
    pass


def resolve_code(word: str) -> str | None:
    w = word.strip().lower()
    if w in ALIASES:
        return ALIASES[w]
    return w.upper() if re.fullmatch(r"[a-z]{3}", w) else None


class FX:
    def __init__(self):
        self._http = httpx.AsyncClient(timeout=15, headers={"Accept": "application/json"})
        self._cache: dict[tuple[str, date], tuple[float, date]] = {}

    async def close(self) -> None:
        await self._http.aclose()

    async def rate(self, code: str, on: date) -> tuple[float, date]:
        """Kurs średni 1 {code} w PLN obowiązujący w dniu `on` (ostatnia tabela ≤ on)."""
        code = code.upper()
        if code == "PLN":
            return 1.0, on
        if (code, on) in self._cache:
            return self._cache[(code, on)]
        frm = on - timedelta(days=10)  # weekendy i święta — bierzemy ostatni dostępny dzień
        for table in ("a", "b"):  # A: główne waluty, B: egzotyczne
            r = await self._http.get(f"{NBP}/{table}/{code.lower()}/{frm}/{on}/?format=json")
            if r.status_code == 404:
                continue
            r.raise_for_status()
            last = r.json()["rates"][-1]
            result = (float(last["mid"]), date.fromisoformat(last["effectiveDate"]))
            self._cache[(code, on)] = result
            return result
        raise FXError(f"NBP nie publikuje kursu {code}")

    async def convert(self, amount: float, src: str, dst: str, on: date) -> tuple[float, float, date]:
        """Zwraca (kwota w dst, kurs src→dst, data tabeli NBP). Krzyżowo przez PLN."""
        (r_src, d1), (r_dst, d2) = await self.rate(src, on), await self.rate(dst, on)
        ratio = r_src / r_dst
        return round(amount * ratio, 2), ratio, max(d1, d2)
