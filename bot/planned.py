"""Płatności cykliczne obsługiwane przez bota (zamiast transakcji zaplanowanych w Wallet).

Harmonogram: plik YAML (patrz schedule.example.yaml). Stan (co zapłacone / pominięte /
kiedy przypomniane) trzymany w JSON na wolumenie, żeby przetrwał restart kontenera.
"""

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from dateutil.rrule import rrulestr

if TYPE_CHECKING:
    from .core import Core, Reply

log = logging.getLogger(__name__)

LOOKBACK_DAYS = 62  # jak daleko wstecz szukać niepotwierdzonych terminów
_ID_RE = re.compile(r"^[a-z0-9_-]{1,24}$")


@dataclass
class Payment:
    id: str
    name: str
    amount: float
    type: str  # expense | income
    category_id: str
    rrule: str
    start: date
    counterparty: str | None = None
    account: str | None = None  # nazwa lub ID; brak = konto domyślne
    active_from: date | None = None  # terminy wcześniejsze ignorowane (płatności dodane przez bota)
    source: str = "file"  # file = config/schedule.yaml, bot = dodane komendą /plan

    def to_json(self) -> dict:
        d = {
            "id": self.id, "name": self.name, "amount": self.amount, "type": self.type,
            "category_id": self.category_id, "rrule": self.rrule, "from": self.start.isoformat(),
            "counterparty": self.counterparty, "account": self.account,
            "active_from": self.active_from.isoformat() if self.active_from else None,
        }
        return {k: v for k, v in d.items() if v is not None}

    @classmethod
    def from_dict(cls, p: dict, source: str) -> "Payment":
        pid = str(p["id"])
        if not _ID_RE.match(pid):
            raise ValueError(f"schedule: id '{pid}' — tylko a-z, 0-9, _ i -, max 24 znaki")
        return cls(
            id=pid,
            name=p["name"],
            amount=float(p["amount"]),
            type=p.get("type", "expense"),
            category_id=p["category_id"],
            rrule=p.get("rrule", "FREQ=MONTHLY"),
            start=_as_date(p["from"]),
            counterparty=p.get("counterparty"),
            account=p.get("account"),
            active_from=_as_date(p["active_from"]) if p.get("active_from") else None,
            source=source,
        )

    def describe_rule(self) -> str:
        r = dict(part.split("=", 1) for part in self.rrule.upper().split(";") if "=" in part)
        n = int(r.get("INTERVAL", 1))
        base = {
            "MONTHLY": f"co miesiąc, {self.start.day}." if n == 1 else f"co {n} mies., {self.start.day}.",
            "YEARLY": f"co rok, {self.start:%d.%m}" if n == 1 else f"co {n} lata, {self.start:%d.%m}",
            "WEEKLY": "co tydzień" if n == 1 else f"co {n} tyg.",
        }.get(r.get("FREQ", ""), self.rrule)
        return base + (f", {r['COUNT']} razy" if "COUNT" in r else "")

    def occurrences(self, after: date, before: date) -> list[date]:
        """Terminy w przedziale [after, before] (włącznie)."""
        rule = rrulestr(self.rrule, dtstart=datetime.combine(self.start, datetime.min.time()))
        lo = datetime.combine(after, datetime.min.time())
        hi = datetime.combine(before, datetime.max.time())
        return [d.date() for d in rule.between(lo, hi, inc=True)]

    def signed(self, amount: float | None = None) -> float:
        a = abs(self.amount if amount is None else amount)
        return -a if self.type == "expense" else a


@dataclass
class Schedule:
    start: date  # terminy sprzed tej daty są ignorowane
    payments: list[Payment]

    @classmethod
    def load(cls, path: str) -> "Schedule":
        if not Path(path).is_file():
            log.info("Brak %s — tylko płatności dodane przez bota", path)
            return cls(date.min, [])
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        payments = [Payment.from_dict(p, "file") for p in raw.get("payments") or []]
        if len({p.id for p in payments}) != len(payments):
            raise ValueError("schedule: id płatności muszą być unikalne")
        log.info("Płatności cykliczne: %d", len(payments))
        return cls(_as_date(raw.get("start", date.today())), payments)

    def get(self, pid: str) -> Payment | None:
        return next((p for p in self.payments if p.id == pid), None)


class State:
    def __init__(self, path: str):
        self.path = Path(path)
        self.data = {"handled": {}, "reminded": {}}
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def handled(self, key: str) -> dict | None:
        return self.data["handled"].get(key)

    def mark(self, key: str, status: str, record_ids: list[str] | None = None, amount: float | None = None) -> None:
        self.data["handled"][key] = {
            "status": status,
            "at": datetime.now().isoformat(timespec="seconds"),
            "record_ids": record_ids or [],
            "amount": amount,
        }
        self._save()

    def unmark(self, key: str) -> None:
        self.data["handled"].pop(key, None)
        self._save()

    def reminded_on(self, key: str) -> str | None:
        return self.data["reminded"].get(key)

    def set_reminded(self, key: str, day: date) -> None:
        self.data["reminded"][key] = day.isoformat()
        self._save()


def occ_key(p: Payment, d: date) -> str:
    return f"{p.id}@{d:%Y%m%d}"


def parse_key(key: str) -> tuple[str, date] | None:
    pid, _, ds = key.partition("@")
    try:
        return pid, datetime.strptime(ds, "%Y%m%d").date()
    except ValueError:
        return None


def parse_amount(text: str) -> float | None:
    m = re.fullmatch(r"\s*([0-9][0-9 ]*(?:[.,][0-9]{1,2})?)\s*(zł|zl|pln)?\s*", text, re.I)
    if not m:
        return None
    return float(m.group(1).replace(" ", "").replace(",", "."))


_PL = str.maketrans("ąćęłńóśźż", "acelnoszz")


def _as_date(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


class Planned:
    """Przypomnienia, potwierdzanie i podgląd płatności cyklicznych."""

    def __init__(self, core: "Core"):
        self.core = core
        self._file = Schedule.load(core.cfg.schedule_file)
        self._mtime = self._file_mtime()
        self.state = State(core.cfg.state_file)
        # Płatności dodane przez bota — obok stanu, na wolumenie (plik YAML jest tylko do odczytu).
        self._extra_path = Path(core.cfg.state_file).with_name("payments.json")
        self._extra = self._load_extra()
        self._awaiting_amount: dict[str, str] = {}  # właściciel → klucz terminu

    @property
    def schedule(self) -> Schedule:
        disabled = set(self.state.data.get("disabled", []))
        payments = [p for p in self._file.payments + self._extra if p.id not in disabled]
        return Schedule(self._file.start, payments)

    def _load_extra(self) -> list[Payment]:
        if not self._extra_path.is_file():
            return []
        return [Payment.from_dict(p, "bot") for p in json.loads(self._extra_path.read_text(encoding="utf-8"))]

    def _save_extra(self) -> None:
        self._extra_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._extra_path.with_suffix(".tmp")
        tmp.write_text(json.dumps([p.to_json() for p in self._extra], ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self._extra_path)

    def new_id(self, name: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", name.lower().translate(_PL)).strip("-")[:20] or "platnosc"
        taken = {p.id for p in self._file.payments + self._extra}
        pid, n = base, 2
        while pid in taken:
            pid, n = f"{base[:20]}-{n}", n + 1
        return pid

    def add(self, p: Payment) -> None:
        p.source = "bot"
        self._extra.append(p)
        self._save_extra()

    def remove(self, pid: str) -> Payment | None:
        p = self.schedule.get(pid)
        if not p:
            return None
        if p.source == "bot":
            self._extra = [x for x in self._extra if x.id != pid]
            self._save_extra()
        else:  # z pliku w repo — tylko wyłączamy, plik zostaje nietknięty
            self.state.data.setdefault("disabled", []).append(pid)
            self.state._save()
        return p

    def reload(self) -> None:
        self._file = Schedule.load(self.core.cfg.schedule_file)
        self._mtime = self._file_mtime()

    def _file_mtime(self) -> float | None:
        try:
            return Path(self.core.cfg.schedule_file).stat().st_mtime
        except OSError:
            return None

    def reload_if_changed(self) -> None:
        """Po deployu (git pull) harmonogram wczytuje się sam — bez restartu i /odswiez."""
        if self._file_mtime() == self._mtime:
            return
        try:
            self.reload()
            log.info("Harmonogram przeładowany po zmianie pliku")
        except Exception:
            log.exception("Błędny schedule.yaml — zostaje poprzednia wersja")
            self._mtime = self._file_mtime()  # nie próbuj co chwilę od nowa

    @property
    def enabled(self) -> bool:
        return bool(self.schedule.payments)

    def pending(self, today: date) -> list[tuple[Payment, date]]:
        """Terminy do dziś włącznie, jeszcze niepotwierdzone i niepominięte."""
        self.reload_if_changed()
        sched = self.schedule
        lo = max(sched.start, today - timedelta(days=LOOKBACK_DAYS))
        out = [
            (p, d)
            for p in sched.payments
            for d in p.occurrences(max(lo, p.active_from or lo), today)
            if not self.state.handled(occ_key(p, d))
        ]
        return sorted(out, key=lambda x: (x[1], x[0].name))

    def reminder(self, p: Payment, d: date, today: date) -> "Reply":
        from .core import Reply, fmt_money

        key = occ_key(p, d)
        when = "Dziś" if d == today else f"Zaległe od {d:%d.%m}" if d < today else f"Termin {d:%d.%m}"
        verb = "Wpłynęło" if p.type == "income" else "Zapłacone"
        return Reply(
            f"📅 *{when}:* {p.name} {fmt_money(p.signed(), self.core.cfg.base_currency)}",
            [(f"✅ {verb}", f"sp:{key}"), ("✏️ Inna kwota", f"sa:{key}"), ("⏭ Pomiń", f"ss:{key}")],
        )

    async def due_reminders(self, today: date) -> list["Reply"]:
        """Przypomnienia do wysłania teraz — każdy termin najwyżej raz dziennie."""
        out = []
        for p, d in self.pending(today):
            key = occ_key(p, d)
            if self.state.reminded_on(key) != today.isoformat():
                self.state.set_reminded(key, today)
                out.append(self.reminder(p, d, today))
        return out

    async def overview(self, today: date) -> list["Reply"]:
        from .core import Reply, fmt_money

        self.reload_if_changed()
        if not self.enabled:
            return [Reply("Brak płatności cyklicznych. Dodaj np. `/plan netflix 49 co miesiąc 15-go`.")]
        cur = self.core.cfg.base_currency
        pending = self.pending(today)
        upcoming = sorted(
            (
                (p, d)
                for p in self.schedule.payments
                for d in p.occurrences(today + timedelta(days=1), today + timedelta(days=30))
            ),
            key=lambda x: (x[1], x[0].name),
        )
        def mark(p: Payment, d: date) -> str:
            done = self.state.handled(occ_key(p, d))
            return {"paid": "✅ ", "skipped": "⏭ "}.get((done or {}).get("status"), "")

        open_upcoming = [(p, d) for p, d in upcoming if not self.state.handled(occ_key(p, d))]
        lines = []
        if pending:
            lines.append("⏳ *Do potwierdzenia:*")
            lines += [f"• {d:%d.%m}  {p.name}  {fmt_money(p.signed(), cur)}" for p, d in pending]
            lines.append("")
        lines.append("📅 *Najbliższe 30 dni:*")
        lines += [f"• {mark(p, d)}{d:%d.%m}  {p.name}  {fmt_money(p.signed(), cur)}" for p, d in upcoming] or ["• nic"]
        total = sum(p.signed() for p, _ in pending + open_upcoming)
        lines.append(f"\nDo rozliczenia: {fmt_money(total, cur)}")
        # Każdy niepotwierdzony termin dostaje osobną wiadomość z przyciskami.
        replies = [Reply("\n".join(lines))] + [self.reminder(p, d, today) for p, d in pending]
        if open_upcoming:
            # Lista wyboru: po kliknięciu przychodzi zwykłe przypomnienie z ✅ / ✏️ / ⏭.
            replies.append(
                Reply(
                    "🗓 Opłacone wcześniej? Wybierz termin:",
                    [(f"{d:%d.%m} · {p.name}", f"pk:{occ_key(p, d)}") for p, d in open_upcoming[:25]],
                    column=True,
                )
            )
        return replies

    async def handle_callback(self, owner: str, action: str, key: str) -> "Reply":
        from .core import Reply

        parsed = parse_key(key)
        p = self.schedule.get(parsed[0]) if parsed else None
        if not p:
            return Reply("Tej płatności nie ma już w harmonogramie.")
        if done := self.state.handled(key):
            return Reply(f"Już obsłużone ({'zapłacone' if done['status'] == 'paid' else 'pominięte'}).")
        if action == "pk":
            return self.reminder(p, parsed[1], self.core.today())
        if action == "ss":
            self.state.mark(key, "skipped")
            return Reply(f"⏭ Pominięto: {p.name} ({parsed[1]:%d.%m})")
        if action == "sa":
            self._awaiting_amount[owner] = key
            return Reply(f"✏️ Podaj kwotę dla *{p.name}* ({parsed[1]:%d.%m}), np. `312,40`")
        return await self.pay(owner, key)

    async def maybe_amount_reply(self, owner: str, text: str) -> "Reply | None":
        """Jeśli bot czekał na kwotę od tej osoby — obsługuje ją. Inaczej None."""
        key = self._awaiting_amount.pop(owner, None)
        if not key:
            return None
        amount = parse_amount(text)
        if amount is None or amount == 0:
            return None  # to nie kwota — traktuj jak zwykłą wiadomość
        return await self.pay(owner, key, amount)

    async def pay(self, owner: str, key: str, amount: float | None = None) -> "Reply":
        from .core import Reply
        from .parser import ParsedRecord

        pid, due = parse_key(key)
        p = self.schedule.get(pid)
        if self.state.handled(key):
            return Reply("Już obsłużone.")
        await self.core.refresh_catalog()
        record = ParsedRecord(
            amount=abs(p.amount if amount is None else amount),
            type=p.type,
            category_id=p.category_id,
            account_id=self.core.resolve_account(p.account),
            date=min(due, self.core.today()).isoformat(),
            counterparty=p.counterparty,
            note=p.name,
        )
        reply = await self.core.save_records(owner, [record], occ_key=key)
        return reply
