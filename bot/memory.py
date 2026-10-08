"""Pamięć sprzedawców: jak użytkownik wcześniej zapisywał wpisy z danego sklepu/firmy.

Po każdym zapisie (✅) bot zapamiętuje dla sprzedawcy: nazwę, typ, kategorię, konto i notatkę.
Przy następnej wiadomości lub paragonie lista trafia do promptu, więc „Piekarnia Julka” z paragonu
dostaje tę samą nazwę, kategorię i konto co ostatnio. Najnowszy zapis wygrywa — poprawka
(„kategoria restauracje” → ✅ Zapisz poprawkę) od razu uczy bota.

Na start (gdy pliku jeszcze nie ma) pamięć wypełnia się historią z Wallet z ostatniego roku.
Plik: data/learned.json na wolumenie.
"""

import hashlib
import json
import logging
import os
import re
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .core import Core
    from .parser import ParsedRecord

log = logging.getLogger(__name__)

PROMPT_LIMIT = 150  # tyle sprzedawców (najświeższych) trafia do promptu
BOOTSTRAP_DAYS = 365

_PL = str.maketrans("ąćęłńóśźż", "acelnoszz")
_LEGAL = re.compile(r"\b(sp(olka)?\s*z\s*o\s*o|s\s*a|sp\s*j|sp\s*k|s\s*c|ltd|gmbh|inc)\b")


def merchant_key(name: str | None) -> str:
    """„Piekarnia Julka Sp. z o.o.” i „PIEKARNIA  JULKA” → „piekarnia julka”."""
    s = (name or "").lower().translate(_PL)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = _LEGAL.sub(" ", s)
    return " ".join(s.split())


def short_id(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:10]


class Memory:
    def __init__(self, core: "Core"):
        self.core = core
        self.path = Path(core.cfg.state_file).with_name("learned.json")
        self.items: dict[str, dict] = {}
        if self.path.is_file():
            try:
                self.items = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:  # uszkodzony plik nie blokuje bota — uczy się od nowa
                log.warning("Pomijam %s: %r", self.path, e)

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("Nie zapisałem pamięci: %r", e)

    # ---------- uczenie ----------

    def learn(self, records: list["ParsedRecord"]) -> None:
        changed = False
        for r in records:
            key = merchant_key(r.counterparty)
            if not key or r.transfer_to:
                continue
            prev = self.items.get(key, {})
            self.items[key] = {
                "name": r.counterparty.strip(),
                "type": r.type,
                "category_id": r.category_id,
                "account_id": r.account_id,
                "note": (r.note or "").strip()[:80],
                "amount": round(r.amount, 2),
                "count": prev.get("count", 0) + 1,
                "last": time.time(),
            }
            changed = True
        if changed:
            self._save()

    async def bootstrap(self) -> None:
        """Jednorazowo: sprzedawcy z historii Wallet (najczęstsza kategoria i konto)."""
        if self.path.exists():
            return
        today = self.core.today()
        try:
            records = await self.core.wallet.records(
                (today - timedelta(days=BOOTSTRAP_DAYS)).isoformat(),
                (today + timedelta(days=1)).isoformat(),
                isTransfer="false",
            )
        except Exception as e:  # spróbuje przy następnym starcie
            log.warning("Pamięć: nie pobrałem historii z Wallet: %r", e)
            return
        groups: dict[str, list[dict]] = {}
        for rec in records:
            if key := merchant_key(rec.get("counterParty")):
                groups.setdefault(key, []).append(rec)
        for key, recs in groups.items():
            if key in self.items:  # zapis przez bota w trakcie pobierania jest świeższy
                continue
            recs.sort(key=lambda x: x.get("recordDate", ""))
            latest = recs[-1]
            value = (latest.get("amount") or {}).get("value", 0)
            cat = Counter((x.get("category") or {}).get("id") for x in recs if x.get("category")).most_common(1)
            acc = Counter(x.get("accountId") for x in recs).most_common(1)
            try:
                last = time.mktime(date.fromisoformat(latest["recordDate"][:10]).timetuple())
            except (KeyError, ValueError):
                last = 0.0
            self.items[key] = {
                "name": latest["counterParty"].strip(),
                "type": "income" if value > 0 else "expense",
                "category_id": cat[0][0] if cat else None,
                "account_id": acc[0][0] if acc else None,
                "note": (latest.get("note") or "").strip()[:80],
                "amount": abs(round(value, 2)),
                "count": len(recs),
                "last": last,
            }
        self._save()
        log.info("Pamięć: %d sprzedawców z %d rekordów Wallet", len(groups), len(records))

    # ---------- korzystanie ----------

    def find(self, name: str | None) -> dict | None:
        key = merchant_key(name)
        if not key:
            return None
        if key in self.items:
            return self.items[key]
        # „Julka” ↔ „Piekarnia Julka” — tylko gdy jednoznaczne
        hits = [v for k, v in self.items.items() if len(key) >= 4 and len(k) >= 4 and (key in k or k in key)]
        return hits[0] if len(hits) == 1 else None

    def forget(self, sid: str) -> dict | None:
        key = next((k for k in self.items if short_id(k) == sid), None)
        if key is None:
            return None
        item = self.items.pop(key)
        self._save()
        return item

    def top(self, n: int) -> list[tuple[str, dict]]:
        return sorted(self.items.items(), key=lambda kv: (-kv[1].get("count", 0), -kv[1].get("last", 0)))[:n]

    def prompt_block(self) -> str:
        if not self.items:
            return ""
        cats, accs = self.core._categories, self.core._accounts
        recent = sorted(self.items.values(), key=lambda v: -v.get("last", 0))[:PROMPT_LIMIT]
        lines = []
        for v in recent:
            cat = v.get("category_id")
            acc = v.get("account_id")
            lines.append(
                f"{v['name']} | {v['type']} | "
                f"{cat if cat in cats else '-'} ({cats.get(cat, {}).get('name', '?')}) | "
                f"{acc if acc in accs else '-'} ({accs.get(acc, {}).get('name', '?')}) | "
                f"{v.get('note') or '-'} | {v.get('count', 1)}×"
            )
        return MEMORY_TEMPLATE.format(items="\n".join(lines))


MEMORY_TEMPLATE = """ZAPAMIĘTANE — jak użytkownik zapisywał wcześniej transakcje u danego sprzedawcy
(nazwa | typ | id kategorii (nazwa) | id konta (nazwa) | ostatnia notatka | ile razy):
{items}

Gdy transakcja dotyczy sprzedawcy z tej listy (także z paragonu, w innej odmianie, pisowni czy skrócie,
np. "julka" = "Piekarnia Julka"), użyj DOKŁADNIE tej samej nazwy w counterparty oraz tej samej kategorii,
konta i typu, a notatkę napisz w podobnym stylu — chyba że bieżąca wiadomość wyraźnie mówi inaczej
(inna kategoria, inne konto, przychód zamiast wydatku). Kwotę i datę zawsze bierz z bieżącej wiadomości.
"""
