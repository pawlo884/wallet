"""Długi: bez stałych rat (np. „A6 — zostało 9 100 zł”) i kredyty ratalne (np. Credit Agricole).

Każdy dług ma etykietę w Wallet („Dług: A6”). Spłaty to zwykłe wydatki z tą etykietą —
bot przypina ją sam, gdy rozpozna spłatę („spłata A6 500”) albo przy potwierdzeniu raty
z płatności cyklicznej; działa też etykieta dodana ręcznie w aplikacji.

- Dług zwykły: zostało = total − paid_before − suma spłat od daty dodania.
- Kredyt (installment + monthly_rate): kapitał liczony jak w banku (rata annuitetowa) —
  każda wpłata najpierw pokrywa odsetki od pozostałego kapitału, reszta zmniejsza kapitał.
"""

import json
import math
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
    installment: float | None = None  # rata — wtedy pokazujemy też „zostało N rat”
    info: str | None = None  # dowolna notatka
    paid_before: float = 0.0  # spłacone przed dodaniem do bota (kredyt: kapitał)
    monthly_rate: float | None = None  # oprocentowanie miesięczne kredytu, np. 0.007 = 8,4% rocznie


class Debts:
    def __init__(self, core: "Core"):
        self.core = core
        self.path = Path(core.cfg.state_file).with_name("debts.json")
        self._items: dict[str, Debt] = {}
        self._mtime: float | None = None

    @property
    def items(self) -> dict[str, Debt]:
        """Wczytuje plik ponownie, gdy zmienił się z zewnątrz (bez restartu bota)."""
        mtime = self.path.stat().st_mtime if self.path.is_file() else None
        if mtime != self._mtime:
            self._mtime = mtime
            self._items = (
                {d["id"]: Debt(**d) for d in json.loads(self.path.read_text(encoding="utf-8"))} if mtime else {}
            )
        return self._items

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(d) for d in self._items.values()], ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime

    def prompt_block(self) -> str:
        if not self.items:
            return "(brak)"
        # Rata pomaga odróżnić podobne nazwy („Opony” 147 zł vs „Opona” 71,10 zł).
        return "\n".join(
            f"{d.id} | {d.name}" + (f" | rata {d.installment:.2f}" if d.installment else "") for d in self.items.values()
        )

    async def add(
        self,
        name: str,
        total: float,
        category_id: str | None = None,
        installment: float | None = None,
        info: str | None = None,
        paid_before: float = 0.0,
        monthly_rate: float | None = None,
    ) -> Debt:
        did = re.sub(r"[^a-z0-9]+", "-", name.lower().translate(_PL)).strip("-")[:24] or "dlug"
        label_name = f"Dług: {name}"
        existing = next((l for l in await self.core.wallet.labels() if l.get("name") == label_name), None)
        label = existing or await self.core.wallet.create_label(label_name)
        debt = Debt(
            did, name, round(abs(total), 2), self.core.today().isoformat(), label["id"], category_id, installment, info,
            round(paid_before, 2), monthly_rate,
        )
        self.items[did] = debt
        self._save()
        return debt

    def remove(self, did: str) -> Debt | None:
        debt = self.items.pop(did, None)
        if debt:
            self._save()
        return debt

    async def payments(self, debt: Debt) -> list[float]:
        """Kwoty spłat (z etykietą) od daty dodania, w kolejności dat."""
        tomorrow = (self.core.today() + timedelta(days=1)).isoformat()
        records = await self.core.wallet.records(debt.start, tomorrow, labelId=debt.label_id)
        records = sorted(records, key=lambda r: r.get("recordDate", ""))
        return [-r["amount"]["value"] for r in records if (r.get("amount") or {}).get("value", 0) < 0]

    @staticmethod
    def remaining(debt: Debt, payments: list[float]) -> float:
        left = debt.total - debt.paid_before
        for a in payments:
            if debt.monthly_rate:  # rata pokrywa najpierw odsetki od pozostałego kapitału
                left = left * (1 + debt.monthly_rate) - a
            else:
                left -= a
        return round(max(left, 0), 2)

    @staticmethod
    def installments_left(debt: Debt, left: float) -> int | None:
        if not debt.installment or left <= 0:
            return None
        r, a = debt.monthly_rate, debt.installment
        if not r:
            n = left / a
        elif left * r >= a:  # rata nie pokrywa nawet odsetek
            return None
        else:
            n = -math.log(1 - left * r / a) / math.log(1 + r)
        # Oprocentowanie i kwoty są zaokrąglone — 100,02 raty to w praktyce 100 (bank wyrówna ostatnią ratą).
        return round(n) if abs(n - round(n)) < 0.1 else math.ceil(n)

    async def status_line(self, debt: Debt) -> str:
        from .core import fmt_money

        left = self.remaining(debt, await self.payments(debt))
        paid = round(debt.total - left, 2)
        cur = self.core.cfg.base_currency
        if left <= 0:
            return f"🎉 *{debt.name}* spłacone! ({fmt_money(debt.total, cur)})"
        pct = paid / debt.total if debt.total else 1
        bar = "▓" * round(pct * 10) + "░" * (10 - round(pct * 10))
        what = "kapitał do spłaty" if debt.monthly_rate else "zostało"
        line = (
            f"🏦 *{debt.name}*: {what} *{fmt_money(left, cur)}*\n"
            f"   {bar} {pct:.0%} · spłacono {fmt_money(paid, cur)} z {fmt_money(debt.total, cur)}"
        )
        if n := self.installments_left(debt, left):
            line += f"\n   {'' if debt.monthly_rate else '≈ '}{n} rat po {fmt_money(debt.installment, cur)}"
            if debt.monthly_rate:
                line += f" · oprocentowanie {debt.monthly_rate * 1200:.2f}%".replace(".", ",")
        if debt.info:
            line += f"\n   _{debt.info}_"
        return line


def parse_debt_command(text: str) -> tuple[str, float] | None:
    """„A6 9100”, „pożyczka od taty 2 500,50” → (nazwa, kwota)."""
    m = re.fullmatch(r"\s*(.+?)\s+(\d[\d  ]*(?:[.,]\d{1,2})?)\s*(zł|zl|pln)?\s*", text, re.I)
    if not m:
        return None
    return m.group(1).strip(), float(m.group(2).replace(" ", "").replace(" ", "").replace(",", "."))
