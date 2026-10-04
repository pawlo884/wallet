"""Logika niezależna od platformy: szkic → potwierdzenie → zapis → cofnięcie, oraz podsumowania."""

import asyncio
import logging
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .config import Config
from .parser import SYSTEM_TEMPLATE, ParsedRecord, RecordParser
from .planned import Payment, Planned
from .wallet_api import UNKNOWN_EXPENSE, UNKNOWN_INCOME, WalletAPI, WalletError

log = logging.getLogger(__name__)

CATALOG_TTL = 3600
DRAFT_TTL = 24 * 3600


@dataclass
class Reply:
    text: str
    # (etykieta, callback_data) — adapter platformy zamienia je na przyciski.
    buttons: list[tuple[str, str]] = field(default_factory=list)
    # True = każdy przycisk w osobnym wierszu (listy wyboru); False = wszystkie obok siebie.
    column: bool = False


@dataclass
class _Draft:
    owner: str
    records: list[ParsedRecord]
    created: float = field(default_factory=time.time)


def fmt_money(value: float, currency: str) -> str:
    s = f"{abs(value):,.2f}".replace(",", " ").replace(".", ",")
    return f"{'−' if value < 0 else ''}{s} {currency}"


class Core:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.wallet = WalletAPI(cfg.wallet_token, cfg.wallet_base_url)
        self.parser = RecordParser(cfg.claude_model)
        self._accounts: dict[str, dict] = {}
        self._categories: dict[str, dict] = {}
        self._default_account_id = ""
        self._catalog_prompt = ""
        self._catalog_at = 0.0
        self._catalog_lock = asyncio.Lock()
        self._drafts: dict[str, _Draft] = {}
        # klucz → (właściciel, id rekordów, klucz terminu płatności cyklicznej lub None)
        self._saved: dict[str, tuple[str, list[str], str | None]] = {}
        self._plan_drafts: dict[str, tuple[str, Payment]] = {}  # klucz → (właściciel, szkic płatności)
        self.planned = Planned(self)

    # ---------- katalog kont i kategorii ----------

    async def refresh_catalog(self, force: bool = False) -> None:
        async with self._catalog_lock:
            if not force and time.time() - self._catalog_at < CATALOG_TTL:
                return
            accounts, categories = await asyncio.gather(
                self.wallet.accounts(), self.wallet.categories()
            )
            self._accounts = {a["id"]: a for a in accounts}
            self._categories = {c["id"]: c for c in categories if c.get("enabled", True)}
            self._default_account_id = self._resolve_default_account()
            self._catalog_prompt = SYSTEM_TEMPLATE.format(
                accounts="\n".join(
                    f"{a['id']} | {a['name']} | {a.get('currencyCode', '')}" for a in accounts
                ),
                default_account=self._default_account_id,
                categories="\n".join(
                    f"{c['id']} | {c['name']} | {c.get('parentName') or (c.get('group') or {}).get('name', '')}"
                    for c in sorted(self._categories.values(), key=lambda c: c["name"])
                ),
            )
            self._catalog_at = time.time()
            log.info("Katalog: %d kont, %d kategorii", len(self._accounts), len(self._categories))

    def _find_account(self, want: str | None) -> str | None:
        want = (want or "").strip()
        if want in self._accounts:
            return want
        for a in self._accounts.values():
            if want and a["name"].lower() == want.lower():
                return a["id"]
        return None

    def resolve_account(self, want: str | None) -> str:
        return self._find_account(want) or self._default_account_id

    def _resolve_default_account(self) -> str:
        if found := self._find_account(self.cfg.default_account):
            return found
        # Bez konfiguracji: konto w walucie bazowej z największą liczbą rekordów.
        candidates = sorted(
            self._accounts.values(),
            key=lambda a: (
                a.get("currencyCode") == self.cfg.base_currency,
                (a.get("recordStats") or {}).get("recordCount", 0),
            ),
            reverse=True,
        )
        return candidates[0]["id"] if candidates else ""

    # ---------- wiadomości ----------

    def today(self) -> date:
        return datetime.now(self.cfg.tz).date()

    def _cleanup(self) -> None:
        cutoff = time.time() - DRAFT_TTL
        self._drafts = {k: d for k, d in self._drafts.items() if d.created > cutoff}

    async def handle_message(self, owner: str, text: str, images: list[tuple[bytes, str]]) -> Reply:
        if not images and (amount_reply := await self.planned.maybe_amount_reply(owner, text)):
            return amount_reply
        try:
            await self.refresh_catalog()
        except WalletError as e:
            return Reply(f"⚠️ Nie mogę połączyć się z Wallet: {e}")
        result = await self.parser.parse(text, images, self.today(), self._catalog_prompt)
        if not result.records:
            return Reply(result.question or "Nie widzę tu transakcji. Napisz np. „biedronka 54,30”.")

        records = [self._sanitize(r) for r in result.records]
        self._cleanup()
        if not self.cfg.require_confirmation:
            return await self.save_records(owner, records)

        key = secrets.token_urlsafe(6)
        self._drafts[key] = _Draft(owner, records)
        lines = ["📝 *Do zapisania:*"] + [self._describe(r) for r in records]
        if result.question:
            lines.append(f"\n❓ {result.question}")
        return Reply("\n".join(lines), [("✅ Zapisz", f"ok:{key}"), ("❌ Anuluj", f"no:{key}")])

    async def handle_callback(self, owner: str, data: str) -> Reply:
        action, _, key = data.partition(":")
        if action in ("sp", "sa", "ss", "pk"):
            return await self.planned.handle_callback(owner, action, key)
        if action in ("pa", "pn", "rm", "rmy", "keep"):
            return await self._plan_callback(owner, action, key)
        if action in ("ok", "no"):
            draft = self._drafts.get(key)
            if not draft or draft.owner != owner:
                return Reply("Ten szkic wygasł albo został już obsłużony.")
            del self._drafts[key]
            if action == "no":
                return Reply("❌ Anulowano.")
            return await self.save_records(owner, draft.records)
        if action == "undo":
            saved = self._saved.pop(key, None)
            if not saved or saved[0] != owner:
                return Reply("Nie ma już czego cofać.")
            try:
                await self.wallet.delete_records(saved[1])
            except WalletError as e:
                self._saved[key] = saved
                return Reply(f"⚠️ Nie udało się cofnąć: {e}")
            if saved[2]:
                self.planned.state.unmark(saved[2])  # termin wraca na listę do potwierdzenia
            return Reply(f"↩️ Usunięto {len(saved[1])} rekord(y) z Wallet.")
        return Reply("Nieznana akcja.")

    async def save_records(self, owner: str, records: list[ParsedRecord], occ_key: str | None = None) -> Reply:
        payload = [self._to_wallet(r) for r in records]
        try:
            results = await self.wallet.create_records(payload)
        except WalletError as e:
            return Reply(f"⚠️ Wallet odrzucił zapis: {e}")
        ok_ids = [r["id"] for r in results if r.get("success")]
        errors = [r.get("error", "?") for r in results if not r.get("success")]
        lines = []
        if ok_ids:
            lines.append(f"✅ Zapisano {len(ok_ids)} z {len(records)}:")
            lines += [self._describe(rec) for rec, res in zip(records, results) if res.get("success")]
        if errors:
            lines.append("⚠️ Błędy: " + "; ".join(errors))
        buttons = []
        if ok_ids:
            if occ_key:
                self.planned.state.mark(occ_key, "paid", ok_ids, records[0].amount)
            key = secrets.token_urlsafe(6)
            self._saved[key] = (owner, ok_ids, occ_key)
            buttons.append(("↩️ Cofnij", f"undo:{key}"))
        return Reply("\n".join(lines) or "⚠️ Nic nie zapisano.", buttons)

    # ---------- konwersje ----------

    def _sanitize(self, r: ParsedRecord) -> ParsedRecord:
        if r.account_id not in self._accounts:
            r.account_id = self._default_account_id
        if r.category_id not in self._categories:
            r.category_id = UNKNOWN_INCOME if r.type == "income" else UNKNOWN_EXPENSE
        today = self.today()
        try:
            d = date.fromisoformat(r.date)
        except ValueError:
            d = today
        r.date = min(d, today).isoformat()
        r.amount = abs(r.amount)
        return r

    def _to_wallet(self, r: ParsedRecord) -> dict:
        d = date.fromisoformat(r.date)
        if d == self.today():
            when = datetime.now(timezone.utc)
        else:
            when = datetime(d.year, d.month, d.day, 12, tzinfo=self.cfg.tz).astimezone(timezone.utc)
        rec = {
            "accountId": r.account_id,
            "amount": {"value": round(-r.amount if r.type == "expense" else r.amount, 2)},
            "categoryId": r.category_id,
            "recordDate": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if r.counterparty:
            rec["counterParty"] = r.counterparty[:255]
        if r.note:
            rec["note"] = r.note[:255]
        return rec

    def _describe(self, r: ParsedRecord) -> str:
        acc = self._accounts.get(r.account_id, {})
        cat = self._categories.get(r.category_id, {}).get("name", "Nieznana")
        value = -r.amount if r.type == "expense" else r.amount
        parts = [fmt_money(value, acc.get("currencyCode", "")), cat]
        if r.counterparty:
            parts.append(r.counterparty)
        parts.append(date.fromisoformat(r.date).strftime("%d.%m"))
        if r.account_id != self._default_account_id:
            parts.append(f"konto {acc.get('name', '?')}")
        line = "• " + " · ".join(parts)
        if r.note:
            line += f"\n   _{r.note}_"
        return line

    # ---------- podsumowania ----------

    async def balances(self) -> Reply:
        try:
            accounts = await self.wallet.accounts()
        except WalletError as e:
            return Reply(f"⚠️ {e}")
        lines = ["💰 *Salda kont:*"]
        for a in sorted(accounts, key=lambda a: a["name"].lower()):
            bal = a.get("balance") or {}
            lines.append(f"• {a['name']}: {fmt_money(bal.get('currentBalance', 0), a.get('currencyCode', ''))}")
        return Reply("\n".join(lines))

    async def month_summary(self) -> Reply:
        today = self.today()
        start = today.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)
        try:
            await self.refresh_catalog()
            records = await self.wallet.records(
                start.isoformat(), end.isoformat(), isTransfer="false", convertTo=self.cfg.base_currency
            )
        except WalletError as e:
            return Reply(f"⚠️ {e}")

        cur = self.cfg.base_currency
        by_cat: dict[str, float] = defaultdict(float)
        income = expense = 0.0
        for r in records:
            conv = r.get("convertedAmount") or {}
            value = conv.get("value", (r.get("amount") or {}).get("value", 0))
            if value < 0:
                expense += -value
                by_cat[(r.get("category") or {}).get("name", "Nieznana")] += -value
            else:
                income += value

        days = (min(today, end - timedelta(days=1)) - start).days + 1
        lines = [
            f"📊 *{start.strftime('%m.%Y')}* (dzień {days})",
            f"Przychody: {fmt_money(income, cur)}",
            f"Wydatki: {fmt_money(-expense, cur)}",
            f"Bilans: {fmt_money(income - expense, cur)}",
            f"Średnio dziennie: {fmt_money(-expense / days, cur)}",
        ]
        if by_cat:
            lines.append("\n*Największe kategorie:*")
            for name, v in sorted(by_cat.items(), key=lambda x: -x[1])[:10]:
                lines.append(f"• {name}: {fmt_money(-v, cur)} ({v / expense:.0%})")
        return Reply("\n".join(lines))

    async def planned_overview(self) -> list[Reply]:
        return await self.planned.overview(self.today())

    async def due_reminders(self) -> list[Reply]:
        return await self.planned.due_reminders(self.today())

    # ---------- definiowanie płatności cyklicznych (/plan, /plany) ----------

    async def plan_add(self, owner: str, text: str) -> Reply:
        if not text.strip():
            return Reply(PLAN_HELP)
        try:
            await self.refresh_catalog()
        except WalletError as e:
            return Reply(f"⚠️ Nie mogę połączyć się z Wallet: {e}")
        today = self.today()
        result = await self.parser.parse_plan(text, today, self._catalog_prompt)
        d = result.plan
        if not d or not d.amount:
            return Reply(result.question or PLAN_HELP)
        try:
            first = date.fromisoformat(d.first_date)
        except ValueError:
            first = today
        rule = f"FREQ={d.freq}"
        if d.interval > 1:
            rule += f";INTERVAL={d.interval}"
        if d.count:
            rule += f";COUNT={d.count}"
        p = Payment(
            id=self.planned.new_id(d.name),
            name=d.name.strip()[:60],
            amount=abs(d.amount),
            type=d.type,
            category_id=d.category_id if d.category_id in self._categories
            else (UNKNOWN_INCOME if d.type == "income" else UNKNOWN_EXPENSE),
            rrule=rule,
            start=first,
            counterparty=d.counterparty,
            active_from=today,  # bez zaległości sprzed dodania
            source="bot",
        )
        upcoming = p.occurrences(today, today + timedelta(days=800))[:3]
        if not upcoming:
            return Reply("Ta płatność nie ma żadnego terminu w przyszłości. Sprawdź datę lub liczbę rat.")

        key = secrets.token_urlsafe(6)
        self._plan_drafts[key] = (owner, p)
        cat = self._categories.get(p.category_id, {}).get("name", "Nieznana")
        return Reply(
            f"🗓 *Nowa płatność cykliczna:*\n"
            f"{p.name} {fmt_money(p.signed(), self.cfg.base_currency)} · {cat}\n"
            f"{p.describe_rule()}\n"
            f"Najbliższe: {', '.join(f'{x:%d.%m.%Y}' for x in upcoming)}",
            [("✅ Dodaj", f"pa:{key}"), ("❌ Anuluj", f"pn:{key}")],
        )

    async def plans_list(self) -> Reply:
        payments = sorted(self.planned.schedule.payments, key=lambda p: (p.type != "income", p.name.lower()))
        if not payments:
            return Reply(PLAN_HELP)
        cur = self.cfg.base_currency
        lines = ["🗂 *Płatności cykliczne:*"] + [
            f"• {p.name} {fmt_money(p.signed(), cur)} — {p.describe_rule()}" for p in payments
        ]
        lines.append("\nDodaj: `/plan netflix 49 co miesiąc 15-go`. Usuń: przycisk poniżej.")
        return Reply("\n".join(lines), [(f"🗑 {p.name}", f"rm:{p.id}") for p in payments[:25]], column=True)

    async def _plan_callback(self, owner: str, action: str, key: str) -> Reply:
        if action in ("pa", "pn"):
            draft = self._plan_drafts.pop(key, None)
            if not draft or draft[0] != owner:
                return Reply("Ten szkic wygasł albo został już obsłużony.")
            if action == "pn":
                return Reply("❌ Anulowano.")
            p = draft[1]
            p.id = self.planned.new_id(p.name)  # na wypadek, gdyby w międzyczasie ktoś dodał tę samą nazwę
            self.planned.add(p)
            return Reply(f"✅ Dodano: *{p.name}* ({p.describe_rule()}). Przypomnę w dniu terminu.")
        if action == "keep":
            return Reply("OK, zostaje.")
        p = self.planned.schedule.get(key)
        if not p:
            return Reply("Tej płatności już nie ma.")
        if action == "rm":
            note = "\n_(pochodzi z pliku w repo — zostanie wyłączona w bocie)_" if p.source == "file" else ""
            return Reply(
                f"Usunąć *{p.name}* ({p.describe_rule()})?{note}",
                [("🗑 Tak, usuń", f"rmy:{p.id}"), ("Zostaw", "keep:")],
            )
        self.planned.remove(p.id)
        return Reply(f"🗑 Usunięto: {p.name}")

    async def refresh(self) -> Reply:
        try:
            await self.refresh_catalog(force=True)
            self.planned.reload()
        except WalletError as e:
            return Reply(f"⚠️ {e}")
        except Exception as e:  # np. błąd w schedule.yaml
            return Reply(f"⚠️ Błąd harmonogramu: {e}")
        return Reply(
            f"🔄 Odświeżono: {len(self._accounts)} kont, {len(self._categories)} kategorii, "
            f"{len(self.planned.schedule.payments)} płatności cyklicznych."
        )

    async def close(self) -> None:
        await self.wallet.close()


HELP = """👋 Zapisuję wydatki i przychody do Wallet.

Po prostu napisz, np.:
• `biedronka 54,30`
• `paliwo 250 orlen wczoraj`
• `kawa 14 i ciastko 9`
• `wypłata 6200`
• `obiad 20 euro`  → konto w EUR
albo wyślij *zdjęcie paragonu*.

Płatności cykliczne: przypominam w dniu terminu — ✅ / ✏️ / ⏭.
Nowa: `/plan netflix 49 co miesiąc 15-go` · lista i usuwanie: `/plany`

Komendy: saldo · miesiac · zaplanowane · plan · plany · odswiez · pomoc"""

PLAN_HELP = """🗓 Dodawanie płatności cyklicznej — opisz ją po ludzku, np.:
• `/plan netflix 49 co miesiąc 15-go`
• `/plan czynsz 1800 10-tego`
• `/plan OC auta 1200 co rok 20 marca`
• `/plan rata telefonu 89 co miesiąc 5-go, 12 rat`
• `/plan pensja 6200 10-go`

Lista i usuwanie: `/plany`"""
