"""Długi spłacane bez stałych rat (np. „A6 — zostało 9 100 zł”).

Każdy dług ma etykietę w Wallet („Dług: A6”). Spłaty to zwykłe wydatki z tą etykietą —
bot przypina ją sam, gdy rozpozna spłatę („spłata A6 500”), ale działa też etykieta dodana
ręcznie w aplikacji. Ile zostało = kwota początkowa − suma wydatków z etykietą od daty dodania.
"""

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .core import Core

_PL = str.maketrans("ąćęłńóśźż", "acelnoszz")


@dataclass
class Debt:
    id: str
    name: str
    total: float
    start: str  # YYYY-MM-DD — spłaty liczone od tej daty
    label_id: str
    category_id: str | None = None


class Debts:
    def __init__(self, core: "Core"):
        self.core = core
        self.path = Path(core.cfg.state_file).with_name("debts.json")
        self.items: dict[str, Debt] = {}
        if self.path.is_file():
            self.items = {d["id"]: Debt(**d) for d in json.loads(self.path.read_text(encoding="utf-8"))}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(d) for d in self.items.values()], ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def prompt_block(self) -> str:
        if not self.items:
            return "(brak)"
        return "\n".join(f"{d.id} | {d.name}" for d in self.items.values())

    async def add(self, name: str, total: float, category_id: str | None = None) -> Debt:
        did = re.sub(r"[^a-z0-9]+", "-", name.lower().translate(_PL)).strip("-")[:24] or "dlug"
        label_name = f"Dług: {name}"
        existing = next((l for l in await self.core.wallet.labels() if l.get("name") == label_name), None)
        label = existing or await self.core.wallet.create_label(label_name)
        debt = Debt(did, name, round(abs(total), 2), self.core.today().isoformat(), label["id"], category_id)
        self.items[did] = debt
        self._save()
        return debt

    def remove(self, did: str) -> Debt | None:
        debt = self.items.pop(did, None)
        if debt:
            self._save()
        return debt

    async def paid(self, debt: Debt) -> float:
        tomorrow = (self.core.today() + timedelta(days=1)).isoformat()
        records = await self.core.wallet.records(debt.start, tomorrow, labelId=debt.label_id)
        return round(sum(-r["amount"]["value"] for r in records if (r.get("amount") or {}).get("value", 0) < 0), 2)

    async def status_line(self, debt: Debt) -> str:
        from .core import fmt_money

        paid = await self.paid(debt)
        left = max(debt.total - paid, 0)
        cur = self.core.cfg.base_currency
        if left <= 0:
            return f"🎉 *{debt.name}* spłacone! ({fmt_money(debt.total, cur)})"
        pct = paid / debt.total if debt.total else 1
        bar = "▓" * round(pct * 10) + "░" * (10 - round(pct * 10))
        return (
            f"🏦 *{debt.name}*: zostało *{fmt_money(left, cur)}*\n"
            f"   {bar} {pct:.0%} · spłacono {fmt_money(paid, cur)} z {fmt_money(debt.total, cur)}"
        )


def parse_debt_command(text: str) -> tuple[str, float] | None:
    """„A6 9100”, „pożyczka od taty 2 500,50” → (nazwa, kwota)."""
    m = re.fullmatch(r"\s*(.+?)\s+(\d[\d  ]*(?:[.,]\d{1,2})?)\s*(zł|zl|pln)?\s*", text, re.I)
    if not m:
        return None
    return m.group(1).strip(), float(m.group(2).replace(" ", "").replace(" ", "").replace(",", "."))
