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
            log.info("Brak %s — płatności cykliczne wyłączone", path)
            return cls(date.max, [])
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        payments = []
        for p in raw.get("payments") or []:
            pid = str(p["id"])
            if not _ID_RE.match(pid):
                raise ValueError(f"schedule: id '{pid}' — tylko a-z, 0-9, _ i -, max 24 znaki")
            payments.append(
                Payment(
                    id=pid,
                    name=p["name"],
                    amount=float(p["amount"]),
                    type=p.get("type", "expense"),
                    category_id=p["category_id"],
                    rrule=p.get("rrule", "FREQ=MONTHLY"),
                    start=_as_date(p["from"]),
                    counterparty=p.get("counterparty"),
                    account=p.get("account"),
                )
            )
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


def _as_date(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


class Planned:
    """Przypomnienia, potwierdzanie i podgląd płatności cyklicznych."""

    def __init__(self, core: "Core"):
        self.core = core
        self.schedule = Schedule.load(core.cfg.schedule_file)
        self._mtime = self._file_mtime()
        self.state = State(core.cfg.state_file)
        self._awaiting_amount: dict[str, str] = {}  # właściciel → klucz terminu

    def reload(self) -> None:
        self.schedule = Schedule.load(self.core.cfg.schedule_file)
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
        lo = max(self.schedule.start, today - timedelta(days=LOOKBACK_DAYS))
        out = [
            (p, d)
            for p in self.schedule.payments
            for d in p.occurrences(lo, today)
            if not self.state.handled(occ_key(p, d))
        ]
        return sorted(out, key=lambda x: (x[1], x[0].name))

    def reminder(self, p: Payment, d: date, today: date) -> "Reply":
        from .core import Reply, fmt_money

        key = occ_key(p, d)
        when = "Dziś" if d == today else f"Zaległe od {d:%d.%m}"
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
            return [Reply("Brak płatności cyklicznych (plik schedule.yaml).")]
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
        lines = []
        if pending:
            lines.append("⏳ *Do potwierdzenia:*")
            lines += [f"• {d:%d.%m}  {p.name}  {fmt_money(p.signed(), cur)}" for p, d in pending]
        lines.append("\n📅 *Najbliższe 30 dni:*" if pending else "📅 *Najbliższe 30 dni:*")
        lines += [f"• {d:%d.%m}  {p.name}  {fmt_money(p.signed(), cur)}" for p, d in upcoming] or ["• nic"]
        total = sum(p.signed() for p, _ in pending + upcoming)
        lines.append(f"\nRazem: {fmt_money(total, cur)}")
        # Każdy niepotwierdzony termin dostaje osobną wiadomość z przyciskami.
        return [Reply("\n".join(lines))] + [self.reminder(p, d, today) for p, d in pending]

    async def handle_callback(self, owner: str, action: str, key: str) -> "Reply":
        from .core import Reply

        parsed = parse_key(key)
        p = self.schedule.get(parsed[0]) if parsed else None
        if not p:
            return Reply("Tej płatności nie ma już w harmonogramie.")
        if done := self.state.handled(key):
            return Reply(f"Już obsłużone ({'zapłacone' if done['status'] == 'paid' else 'pominięte'}).")
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
