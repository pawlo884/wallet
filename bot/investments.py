"""Inwestycje spoza Wallet (metale szlachetne, ETF-y/akcje) — wycena na żywo w PLN.

Ceny: metale z gold-api.com (USD/uncja, bez klucza), tickery z Yahoo Finance (waluta z notowania);
przeliczenie na PLN kursem NBP. Portfel w data/investments.json (wolumen, poza repo).
"""

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .core import Core

log = logging.getLogger(__name__)

METALS = {"XAG": "srebro", "XAU": "złoto", "XPT": "platyna", "XPD": "pallad"}
PRICE_TTL = 30 * 60
_PL = str.maketrans("ąćęłńóśźż", "acelnoszz")


@dataclass
class Holding:
    id: str
    name: str
    kind: str  # metal | ticker
    symbol: str  # XAG / VWCE.DE
    quantity: float  # metal: uncje trojańskie; ticker: sztuki
    unit: str = "oz"
    cost: float | None = None  # łączny koszt zakupu w PLN
    bought: str | None = None  # kiedy kupione (rok lub data, tekst)


class Investments:
    def __init__(self, core: "Core"):
        self.core = core
        self.path = Path(core.cfg.state_file).with_name("investments.json")
        self._http = httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0 wallet-bot"})
        self._prices: dict[str, tuple[float, str, float]] = {}  # symbol → (cena, waluta, kiedy)

    @property
    def items(self) -> dict[str, Holding]:
        if not self.path.is_file():
            return {}
        return {h["id"]: Holding(**h) for h in json.loads(self.path.read_text(encoding="utf-8"))}

    def _write(self, items: dict[str, Holding]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(h) for h in items.values()], ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, h: Holding) -> Holding:
        items = self.items
        base = re.sub(r"[^a-z0-9]+", "-", h.name.lower().translate(_PL)).strip("-")[:20] or "inwestycja"
        h.id, n = base, 2
        while h.id in items:
            h.id, n = f"{base}-{n}", n + 1
        items[h.id] = h
        self._write(items)
        return h

    def remove(self, hid: str) -> Holding | None:
        items = self.items
        h = items.pop(hid, None)
        if h:
            self._write(items)
        return h

    async def _quote(self, h: Holding) -> tuple[float, str]:
        """Cena jednostki w walucie notowania: (cena, waluta)."""
        cached = self._prices.get(h.symbol)
        if cached and time.time() - cached[2] < PRICE_TTL:
            return cached[0], cached[1]
        if h.kind == "metal":
            r = await self._http.get(f"https://api.gold-api.com/price/{h.symbol.upper()}")
            r.raise_for_status()
            d = r.json()
            price, cur = float(d["price"]), d.get("currency", "USD")
        else:
            r = await self._http.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{h.symbol}", params={"range": "1d", "interval": "1d"}
            )
            r.raise_for_status()
            meta = r.json()["chart"]["result"][0]["meta"]
            price, cur = float(meta["regularMarketPrice"]), meta.get("currency", "USD")
            if cur == "GBp":  # Londyn notuje w pensach
                price, cur = price / 100, "GBP"
        self._prices[h.symbol] = (price, cur, time.time())
        return price, cur

    async def valuate(self) -> list[dict]:
        out = []
        today = self.core.today()
        for h in self.items.values():
            row = {"id": h.id, "name": h.name, "symbol": h.symbol, "quantity": h.quantity, "unit": h.unit,
                   "cost": h.cost, "bought": h.bought, "value": None, "unit_pln": None, "error": None}
            try:
                price, cur = await self._quote(h)
                unit_pln, _, _ = await self.core.fx.convert(price, cur, "PLN", today)
                row.update(unit_pln=unit_pln, value=round(unit_pln * h.quantity, 2), quote=price, currency=cur)
                if h.cost:
                    row["gain"] = round(row["value"] - h.cost, 2)
                    row["gain_pct"] = round(row["gain"] / h.cost * 100, 1)
            except Exception as e:  # brak notowania / sieć — pokazujemy resztę portfela
                log.warning("Wycena %s: %r", h.symbol, e)
                row["error"] = "brak aktualnej ceny"
            out.append(row)
        return out

    async def close(self) -> None:
        await self._http.aclose()
