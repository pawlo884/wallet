"""Logika niezależna od platformy: szkic → potwierdzenie → zapis → cofnięcie, oraz podsumowania."""

import asyncio
import logging
import os
import re
import secrets
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

import httpx

from . import events
from .config import Config
from .debts import Debts, parse_debt_command
from .fx import FX, FXError, resolve_code
from .investments import Holding, Investments
from .lists import Lists
from .memory import Memory, short_id
from .gcal import GoogleCalendar, GoogleError, conflicts, event_line
from .strava import Strava, StravaError
from .statement import Reconciler, email_to_text, looks_like_statement
from .stt import STT
from .parser import SYSTEM_TEMPLATE, ParsedRecord, RecordParser
from .planned import Payment, Planned
from .session import Session
from .store import Store, day_start, month_start
from .wallet_api import UNKNOWN_EXPENSE, UNKNOWN_INCOME, WalletAPI, WalletError

log = logging.getLogger(__name__)

CATALOG_TTL = 3600
DRAFT_TTL = 24 * 3600
SAVED_TTL = 7 * 24 * 3600  # „Cofnij” i „zmień na…” po zapisie
HISTORY_TTL = 30 * 60  # pamięć rozmowy: ostatnie 30 min
HISTORY_TURNS = 8  # i najwyżej tyle wiadomości (user + bot)
LIST_ACTIONS = {"iu", "tk", "td", "tp", "tn", "tx", "txy", "bk", "bs", "bx", "bxy"}


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
    replaces: str | None = None  # klucz zapisanych rekordów, które ta poprawka zastąpi


_FX_NOTE = re.compile(r"\s*·?\s*−?[\d  ]+,\d{2} [A-Z]{3} po [\d,.]+ \(NBP [\d.]+\)")


def fmt_rate(rate: float) -> str:
    return f"{rate:.4f}".replace(".", ",")


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
        # Szkice i przyciski przeżywają restart (deploy) — zapis w data/session.json na wolumenie.
        self.session = Session(Path(cfg.state_file).with_name("session.json"))
        self._drafts: dict[str, _Draft] = self.session.dict(
            "drafts", DRAFT_TTL,
            lambda d: {"owner": d.owner, "records": [r.model_dump() for r in d.records],
                       "created": d.created, "replaces": d.replaces},
            lambda v: _Draft(v["owner"], [ParsedRecord(**r) for r in v["records"]], v["created"], v["replaces"]),
        )
        # klucz → (właściciel, id rekordów, klucz terminu płatności cyklicznej lub None)
        self._saved: dict[str, tuple[str, list[str], str | None]] = self.session.dict(
            "saved", SAVED_TTL, list, tuple
        )
        # klucz → (właściciel, szkic płatności)
        self._plan_drafts: dict[str, tuple[str, Payment]] = self.session.dict(
            "plan_drafts", DRAFT_TTL,
            lambda v: [v[0], v[1].to_json(), v[1].source],
            lambda v: (v[0], Payment.from_dict(v[1], v[2])),
        )
        self._inv_drafts: dict[str, tuple[str, Holding]] = self.session.dict(
            "inv_drafts", DRAFT_TTL, lambda v: [v[0], asdict(v[1])], lambda v: (v[0], Holding(**v[1]))
        )
        # korekty salda czekające na wybór (wpis / saldo początkowe)
        self._fix_drafts: dict[str, tuple] = self.session.dict("fix_drafts", DRAFT_TTL, list, tuple)
        self.fx = FX()
        self.stt = STT(cfg.stt_model, cfg.stt_threads) if cfg.stt_enabled else None
        self.reconciler = Reconciler(self)
        self.debts = Debts(self)
        self.investments = Investments(self)
        self.strava = Strava(self)
        self.gcal = GoogleCalendar(self)  # zadania ↔ Kalendarz Google (data/google.json)
        self._gcal_lock = asyncio.Lock()  # zmiany wysyłane po kolei, żeby nie zdublować wydarzenia
        self._bg: set[asyncio.Task] = set()
        self._covers_http = httpx.AsyncClient(timeout=15, headers={"User-Agent": "wallet-bot/1.0 (book covers)"})
        self._covers_running = False
        self.memory = Memory(self)  # sprzedawca → jak ostatnio zapisany (data/learned.json)
        # Krótka pamięć rozmowy (w RAM): właściciel → [(czas, rola, tekst)]
        self._history: dict[str, list[tuple[float, str, str]]] = {}
        self._last_draft: dict[str, str] = self.session.dict("last_draft", DRAFT_TTL)  # właściciel → klucz ostatniego szkicu
        self._last_saved: dict[str, str] = self.session.dict("last_saved", SAVED_TTL)  # właściciel → klucz ostatnio zapisanych rekordów
        self.planned = Planned(self)
        # Baza asystenta: zadania, książki, dziennik zdarzeń (data/assistant.db)
        self.store = Store(Path(cfg.state_file).with_name("assistant.db"))
        self.lists = Lists(self, self.store)
        self.parser.on_usage = self._log_usage

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
                debts=self.debts.prompt_block(),
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

    def now(self) -> datetime:
        return datetime.now(self.cfg.tz)

    def _cleanup(self) -> None:
        self._drafts.prune()

    # ---------- pamięć rozmowy ----------

    def remember(self, owner: str, role: str, text: str) -> None:
        now = time.time()
        h = [x for x in self._history.get(owner, []) if x[0] > now - HISTORY_TTL]
        if h and h[-1][1] == role:  # kolejne wiadomości tej samej strony sklejamy (API wymaga naprzemienności)
            h[-1] = (now, role, f"{h[-1][2]}\n{text}")
        else:
            h.append((now, role, text))
        self._history[owner] = h[-HISTORY_TURNS:]

    def _history_messages(self, owner: str) -> list[dict]:
        h = [x for x in self._history.get(owner, []) if x[0] > time.time() - HISTORY_TTL]
        while h and h[0][1] != "user":
            h = h[1:]
        if h and h[-1][1] == "user":  # bieżąca wiadomość dojdzie jako ostatnia tura użytkownika
            h = h[:-1]
        return [{"role": role, "content": text} for _, role, text in h]

    async def handle_voice(self, owner: str, audio: bytes) -> Reply:
        """Głosówka → tekst (lokalny Whisper) → dalej jak zwykła wiadomość."""
        if not self.stt:
            return Reply("🎤 Rozpoznawanie mowy jest wyłączone (STT_ENABLED=false).")
        try:
            text = await self.stt.transcribe(audio)
        except Exception:
            log.exception("Błąd rozpoznawania mowy")
            return Reply("⚠️ Nie udało się odczytać nagrania. Spróbuj jeszcze raz albo napisz.")
        if not text:
            return Reply("🎤 Nic nie usłyszałem — nagraj jeszcze raz.")
        reply = await self.handle_message(owner, text, [])
        first = reply[0] if isinstance(reply, list) else reply
        first.text = f"🎤 „{text}”\n\n{first.text}"
        return reply

    async def handle_message(
        self, owner: str, text: str, images: list[tuple[bytes, str]]
    ) -> Reply | list[Reply]:
        trace = events.new_trace()
        started = time.monotonic()
        shown = ("[zdjęcie] " if images else "") + (text or "")
        try:
            if not images and looks_like_statement(text):  # wklejony/udostępniony wyciąg z banku
                events.agent_var.set("wydatki")
                reply = await self.statement(owner, text)
            else:
                reply = await self._handle_message(owner, text, images)
                self.remember(owner, "user", shown)
                self.remember(owner, "assistant", reply.text)
        except Exception as e:
            self.store.log_event("error", trace=trace, agent=events.agent_var.get(), name="wiadomość",
                                 input=shown, output=repr(e), ms=_ms(started))
            raise
        first = reply[0] if isinstance(reply, list) else reply
        self.store.log_event("message", trace=trace, agent=events.agent_var.get(), input=shown,
                             output=first.text, ms=_ms(started))
        self.store.tag_trace(trace, events.agent_var.get())  # wywołania modelu sprzed decyzji, kto obsłużył
        return reply

    async def statement(self, owner: str, text: str, source: str = "wyciągu") -> list[Reply]:
        if not text.strip():
            return [Reply("📄 Wklej treść wyciągu po `/wyciag` albo przekaż maila z wyciągiem na adres bota.")]
        try:
            return await self.reconciler.reconcile(owner, text, source)
        except WalletError as e:
            return [Reply(f"⚠️ Nie mogę połączyć się z Wallet: {e}")]

    async def statement_file(self, owner: str, raw: bytes) -> list[Reply]:
        """Plik .eml (udostępniony mail) albo .txt z wyciągiem."""
        head = raw[:4000].lower()
        if b"from:" in head and (b"subject:" in head or b"mime-version:" in head):
            _, subject, text = email_to_text(raw)
            return await self.statement(owner, text, f"maila „{subject[:40]}”")
        return await self.statement(owner, raw.decode("utf-8", errors="replace"))

    async def _handle_message(self, owner: str, text: str, images: list[tuple[bytes, str]]) -> Reply:
        if not images and (amount_reply := await self.planned.maybe_amount_reply(owner, text)):
            return amount_reply
        try:
            await self.refresh_catalog()
        except WalletError as e:
            return Reply(f"⚠️ Nie mogę połączyć się z Wallet: {e}")
        result = await self.parser.parse(
            text, images, self.today(), self._catalog_prompt, self._history_messages(owner),
            memory=self.memory.prompt_block(), lists=self.lists.prompt_block(),
        )
        if images and not (result.records or result.tasks or result.books):
            # Na zdjęciu nic nie znalazł — najczęściej to książka, którą przeoczył przy szukaniu paragonu.
            try:
                result.books = await self.parser.parse_books(images, text)
            except Exception as e:
                log.warning("Druga próba (książki): %r", e)
            if result.books:
                result.question = None
        agents = (["wydatki"] if result.records else []) + (["zadania"] if result.tasks else []) + (
            ["ksiazki"] if result.books else [])
        events.agent_var.set("+".join(agents) or "wydatki")
        # Zadania i książki od razu na listę (z ↩️ Cofnij); transakcje jak dotąd jako szkic do ✅.
        item_lines, undo_key = self.lists.apply(result.tasks, result.books)
        if result.books:
            self.find_covers()
        item_lines += await self._calendar_warnings(self.lists.changed)
        if not result.records:
            if not item_lines:
                return Reply(result.question or "Nie widzę tu transakcji, zadania ani książki. Napisz np. „biedronka 54,30”.")
            if result.question:
                item_lines.append(f"\n❓ {result.question}")
            return Reply("\n".join(item_lines), [("↩️ Cofnij", f"iu:{undo_key}")] if undo_key else [])
        reply = await self._records_reply(owner, text, result)
        if item_lines:
            reply.text = "\n".join(item_lines) + "\n\n" + reply.text
            if undo_key:
                reply.buttons.append(("↩️ Cofnij listę", f"iu:{undo_key}"))
        return reply

    async def _records_reply(self, owner: str, text: str, result) -> Reply:
        records = [self._sanitize(r) for r in result.records]
        self._apply_memory(records)
        for r in records:  # notatka zawsze — żeby po miesiącach było wiadomo, na co to poszło
            if not (r.note or "").strip():
                r.note = (text.strip() or "z paragonu")[:120]
        try:
            for r in records:
                await self._convert(r)
        except Exception as e:  # brak kursu (FXError) albo NBP niedostępne
            log.warning("Przeliczenie waluty: %s", e)
            return Reply(f"⚠️ Nie mogę przeliczyć waluty: {e}")
        if not result.amends:
            # Bezpiecznik: model czasem powtarza rekordy z otwartego szkicu — zapisałyby się dwa razy.
            # Gdy odpadłoby wszystko (np. „jeszcze jedna kawa 14”), zostawiamy jak jest.
            last = self._drafts.get(self._last_draft.get(owner, ""))
            if last and last.owner == owner:
                seen = {self._fingerprint(r) for r in last.records}
                records = [r for r in records if self._fingerprint(r) not in seen] or records
        self._cleanup()
        remembered = list(dict.fromkeys(
            r.counterparty for r in records if not r.transfer_to and self.memory.find(r.counterparty)
        ))

        # Poprawka: zastępuje otwarty szkic albo (przez „Zapisz poprawkę”) ostatnio zapisane rekordy.
        replaces = None
        if result.amends:
            last = self._last_draft.get(owner)
            if last in self._drafts and self._drafts[last].owner == owner:
                replaces = self._drafts.pop(last).replaces
            elif (saved := self._last_saved.get(owner)) in self._saved and self._saved[saved][0] == owner:
                replaces = saved

        if not self.cfg.require_confirmation and not replaces:
            return await self.save_records(owner, records)

        key = secrets.token_urlsafe(6)
        self._drafts[key] = _Draft(owner, records, replaces=replaces)
        self._last_draft[owner] = key
        header = "✏️ *Poprawka (zastąpi zapisany rekord):*" if replaces else "📝 *Do zapisania:*"
        lines = [header] + [self._describe(r) for r in records]
        if remembered and not result.amends:
            lines.append(f"🧠 _Jak ostatnio: {', '.join(remembered)}_")
        if result.question:
            lines.append(f"\n❓ {result.question}")
        ok_label = "✅ Zapisz poprawkę" if replaces else "✅ Zapisz"
        return Reply("\n".join(lines), [(ok_label, f"ok:{key}"), ("❌ Anuluj", f"no:{key}")])

    async def handle_callback(self, owner: str, data: str) -> Reply:
        trace = events.new_trace()
        started = time.monotonic()
        reply = await self._handle_callback(owner, data)
        self.remember(owner, "assistant", reply.text)  # żeby „zmień na 45” wiedziało, co zapisano
        action = data.partition(":")[0]
        agent = "wydatki" if action not in LIST_ACTIONS else "listy" if action == "iu" else (
            "ksiazki" if action.startswith("b") else "zadania")
        self.store.log_event("button", trace=trace, agent=agent, input=data, output=reply.text, ms=_ms(started))
        return reply

    async def _handle_callback(self, owner: str, data: str) -> Reply:
        action, _, key = data.partition(":")
        if action in LIST_ACTIONS:
            return self.lists.handle_callback(action, key)
        if action in ("sp", "sa", "ss", "pk"):
            return await self.planned.handle_callback(owner, action, key)
        if action in ("ia", "in", "ir", "iry"):
            return await self._inv_callback(owner, action, key)
        if action in ("kr", "ki", "kn"):
            return await self._fix_callback(owner, action, key)
        if action in ("mf", "mfy"):
            return self._memory_callback(action, key)
        if action == "dl":
            debt = self.debts.items.get(key)
            if not debt:
                return Reply("Tego długu już nie śledzę.")
            return Reply(
                f"Przestać śledzić *{debt.name}*? (wpisy i etykieta w Wallet zostają)",
                [("🗑 Tak", f"dly:{key}"), ("Zostaw", "keep:")],
            )
        if action == "dly":
            debt = self.debts.remove(key)
            self._catalog_at = 0.0
            return Reply(f"🗑 Nie śledzę już: {debt.name}" if debt else "Tego długu już nie śledzę.")
        if action in ("pa", "pn", "rm", "rmy", "keep"):
            return await self._plan_callback(owner, action, key)
        if action in ("ok", "no"):
            draft = self._drafts.get(key)
            if not draft or draft.owner not in (owner, "*"):  # "*" = szkic z maila, dla każdej dozwolonej osoby
                return Reply("Ten szkic wygasł albo został już obsłużony.")
            del self._drafts[key]
            if action == "no":
                return Reply("❌ Anulowano.")
            prefix = ""
            if draft.replaces:
                if err := await self._undo(owner, draft.replaces):
                    return Reply(f"⚠️ Nie udało się usunąć starej wersji: {err}")
                prefix = "✏️ Stara wersja usunięta.\n"
            reply = await self.save_records(owner, draft.records)
            reply.text = prefix + reply.text
            return reply
        if action == "undo":
            if (err := await self._undo(owner, key)) is not None:
                return Reply(err if err.startswith("Nie ma") else f"⚠️ Nie udało się cofnąć: {err}")
            return Reply("↩️ Usunięto z Wallet.")
        return Reply("Nieznana akcja.")

    async def _undo(self, owner: str, key: str) -> str | None:
        """Usuwa zapisane rekordy spod klucza. Zwraca opis błędu albo None."""
        saved = self._saved.pop(key, None)
        if not saved or saved[0] != owner:
            return "Nie ma już czego cofać."
        try:
            await self.wallet.delete_records(saved[1])
        except WalletError as e:
            self._saved[key] = saved
            return str(e)
        if saved[2]:
            self.planned.state.unmark(saved[2])  # termin wraca na listę do potwierdzenia
        return None

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
            self.memory.learn([rec for rec, res in zip(records, results) if res.get("success")])
            if occ_key:
                self.planned.state.mark(occ_key, "paid", ok_ids, records[0].amount)
            key = secrets.token_urlsafe(6)
            self._saved[key] = (owner, ok_ids, occ_key)
            if not occ_key:  # poprawki „zmień na…” dotyczą zwykłych wpisów, nie płatności cyklicznych
                self._last_saved[owner] = key
            buttons.append(("↩️ Cofnij", f"undo:{key}"))
            # Po spłacie długu — od razu ile zostało.
            for did in dict.fromkeys(r.debt_id for r in records if r.debt_id):
                if debt := self.debts.items.get(did):
                    try:
                        lines.append(await self.debts.status_line(debt))
                    except WalletError as e:
                        log.warning("Stan długu %s: %s", did, e)
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
        if r.transfer_to not in self._accounts or r.transfer_to == r.account_id:
            r.transfer_to = None
        if r.transfer_to:
            r.type, r.debt_id = "expense", None  # przelew wychodzi z konta źródłowego
        debt = self.debts.items.get(r.debt_id or "")
        r.debt_id = debt.id if debt and r.type == "expense" else None
        if debt and debt.category_id:
            r.category_id = debt.category_id
        return r

    def _apply_memory(self, records: list[ParsedRecord]) -> None:
        """Sprzedawca znany z wcześniejszych wpisów: ta sama nazwa, a gdy model nie dobrał kategorii —
        kategoria z pamięci. Resztę (konto, typ, notatka) podpowiada model z bloku ZAPAMIĘTANE."""
        for r in records:
            if r.transfer_to or not (m := self.memory.find(r.counterparty)):
                continue
            r.counterparty = m["name"]
            if r.category_id in (UNKNOWN_EXPENSE, UNKNOWN_INCOME) and m.get("type") == r.type \
                    and m.get("category_id") in self._categories:
                r.category_id = m["category_id"]

    @staticmethod
    def _fingerprint(r: ParsedRecord) -> tuple:
        return (r.type, round(r.amount, 2), r.category_id, (r.counterparty or "").lower(), r.date)

    async def _convert(self, r: ParsedRecord) -> None:
        """Kwota w obcej walucie → waluta konta po kursie NBP z dnia transakcji (info w notatce)."""
        src = (r.currency or "").upper()
        dst = self._accounts.get(r.account_id, {}).get("currencyCode", self.cfg.base_currency)
        r.currency = None
        if not src or src == dst:
            return
        value, ratio, table_day = await self.fx.convert(r.amount, src, dst, date.fromisoformat(r.date))
        info = f"{fmt_money(r.amount, src)} po {fmt_rate(ratio)} (NBP {table_day:%d.%m})"
        # Przy poprawkach model powtarza notatkę z poprzedniej wersji — usuń stare przeliczenie.
        note = _FX_NOTE.sub("", r.note or "").strip(" ·")
        r.note = f"{note} · {info}" if note else info
        r.amount = value

    def _to_wallet(self, r: ParsedRecord) -> dict:
        d = date.fromisoformat(r.date)
        now = datetime.now(self.cfg.tz)
        if d == now.date():  # dzisiejszy wpis: bieżąca godzina; inny dzień: południe tego dnia
            when = now.astimezone(timezone.utc)
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
        if r.debt_id and (debt := self.debts.items.get(r.debt_id)):
            rec["labelIds"] = [debt.label_id]
        if r.transfer_to:  # Wallet tworzy parę rekordów (wychodzący + przychodzący) z kategorią Przelew
            rec.pop("categoryId", None)
            rec["transfer"] = {"pairingMode": "new", "accountId": r.transfer_to}
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
        if r.debt_id and (debt := self.debts.items.get(r.debt_id)):
            parts.append(f"🏦 spłata: {debt.name}")
        if r.transfer_to:
            parts[1] = f"↔ przelew na: {self._accounts.get(r.transfer_to, {}).get('name', '?')}"
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

    async def month_data(self) -> dict:
        """Bieżący miesiąc: przychody, wydatki, kategorie (bez przelewów i kont archiwalnych)."""
        today = self.today()
        start = today.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)
        await self.refresh_catalog()
        # Tylko aktywne konta liczone do statystyk — archiwum (stara historia) nie może dublować wydatków.
        counted = [a for a, acc in self._accounts.items() if not acc.get("excludeFromStats")][:10]
        records = await self.wallet.records(
            start.isoformat(), end.isoformat(), isTransfer="false", convertTo=self.cfg.base_currency,
            accountId=",".join(counted),
        ) if counted else []

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
        return {
            "month": start.strftime("%m.%Y"), "day": days, "currency": self.cfg.base_currency,
            "income": round(income, 2), "expense": round(expense, 2),
            "categories": [{"name": n, "value": round(v, 2)} for n, v in sorted(by_cat.items(), key=lambda x: -x[1])[:10]],
        }

    async def month_summary(self) -> Reply:
        try:
            m = await self.month_data()
        except WalletError as e:
            return Reply(f"⚠️ {e}")
        cur, income, expense, days = m["currency"], m["income"], m["expense"], m["day"]
        lines = [
            f"📊 *{m['month']}* (dzień {days})",
            f"Przychody: {fmt_money(income, cur)}",
            f"Wydatki: {fmt_money(-expense, cur)}",
            f"Bilans: {fmt_money(income - expense, cur)}",
            f"Średnio dziennie: {fmt_money(-expense / days, cur)}",
        ]
        if m["categories"]:
            lines.append("\n*Największe kategorie:*")
            for c in m["categories"]:
                lines.append(f"• {c['name']}: {fmt_money(-c['value'], cur)} ({c['value'] / expense:.0%})")
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

    # ---------- długi (/dlug, /dlugi) ----------

    async def debt_add(self, text: str) -> Reply:
        parsed = parse_debt_command(text)
        if not parsed:
            return Reply(
                "🏦 Dodaj dług: `/dlug <nazwa> <ile zostało>`, np. `/dlug A6 9100` albo `/dlug pożyczka od taty 2000`.\n"
                "Spłaty wpisuj normalnie: „spłata A6 500” — sam odejmę od długu."
            )
        name, total = parsed
        try:
            await self.refresh_catalog()
            # Kategoria ze starych wpisów o tej nazwie nie jest znana — Leasing dla aut, inaczej Loan.
            cat = "5c5c1f46-0032-8000-8000-000000000000" if re.search(r"\b(a\d|auto|samoch)", name.lower()) else None
            debt = await self.debts.add(name, total, cat if cat in self._categories else None)
            self._catalog_at = 0.0  # prompt dostanie nową listę długów
            return Reply(
                f"✅ Dodano dług *{debt.name}*: {fmt_money(debt.total, self.cfg.base_currency)}.\n"
                f"W Wallet utworzyłem etykietę „Dług: {debt.name}”.\n"
                f"Spłaty wpisuj np. „spłata {debt.name} 500” — pokażę, ile zostało. Podgląd: `/dlugi`."
            )
        except WalletError as e:
            return Reply(f"⚠️ {e}")

    async def debts_list(self) -> Reply:
        if not self.debts.items:
            return await self.debt_add("")
        try:
            lines = [await self.debts.status_line(d) for d in self.debts.items.values()]
        except WalletError as e:
            return Reply(f"⚠️ {e}")
        return Reply(
            "\n\n".join(lines) + "\n\nNowy: `/dlug <nazwa> <kwota>`",
            [(f"🗑 Przestań śledzić: {d.name}", f"dl:{d.id}") for d in self.debts.items.values()],
            column=True,
        )

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

    # ---------- pamięć sprzedawców (/pamiec) ----------

    async def memory_list(self) -> Reply:
        if not self.memory.items:
            return Reply("🧠 Jeszcze nic nie pamiętam. Po każdym zapisie zapamiętuję sprzedawcę, "
                         "kategorię i konto — następnym razem podpowiem to samo.")
        try:
            await self.refresh_catalog()
        except WalletError:
            pass
        top = self.memory.top(30)
        lines = [f"🧠 *Pamiętam {len(self.memory.items)} sprzedawców.* Najczęstsi:"]
        for _, v in top:
            cat = self._categories.get(v.get("category_id"), {}).get("name", "?")
            line = f"• {v['name']} → {cat}"
            if v.get("account_id") and v["account_id"] != self._default_account_id:
                line += f", konto {self._accounts.get(v['account_id'], {}).get('name', '?')}"
            lines.append(f"{line} ({v.get('count', 1)}×)")
        lines.append("\nPoprawka wpisu (np. „kategoria restauracje”) od razu zmienia pamięć. "
                     "Zapomnij sprzedawcę: przycisk poniżej.")
        return Reply("\n".join(lines), [(f"🗑 {v['name']}", f"mf:{short_id(k)}") for k, v in top], column=True)

    def _memory_callback(self, action: str, sid: str) -> Reply:
        item = next((v for k, v in self.memory.items.items() if short_id(k) == sid), None)
        if not item:
            return Reply("Tego sprzedawcy już nie pamiętam.")
        if action == "mf":
            return Reply(f"Zapomnieć *{item['name']}*?", [("🗑 Tak", f"mfy:{sid}"), ("Zostaw", "keep:")])
        self.memory.forget(sid)
        return Reply(f"🗑 Zapomniałem: {item['name']}")

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

    async def fx_quote(self, text: str) -> Reply:
        """/kurs [kwota] [waluta] [na walutę] — np. „100 eur”, „50 usd eur”, „1000 pln usd”."""
        today = self.today()
        tokens = re.findall(r"\d+(?:[.,]\d+)?|[^\s\d]+", text.lower())
        nums = [float(t.replace(",", ".")) for t in tokens if t[0].isdigit()]
        codes = [c for t in tokens if not t[0].isdigit() and (c := resolve_code(t))]
        try:
            if not codes:
                lines = ["💱 *Kursy NBP (średnie):*"]
                for code in ("EUR", "USD", "CHF", "GBP"):
                    rate, day = await self.fx.rate(code, today)
                    lines.append(f"• 1 {code} = {fmt_rate(rate)} PLN")
                lines.append(f"\nTabela z {day:%d.%m.%Y}. Przelicz: `/kurs 100 eur`")
                return Reply("\n".join(lines))
            src = codes[0]
            dst = codes[1] if len(codes) > 1 else ("PLN" if src != "PLN" else "EUR")
            amount = nums[0] if nums else 1.0
            value, ratio, day = await self.fx.convert(amount, src, dst, today)
        except Exception as e:
            return Reply(f"⚠️ Nie mogę pobrać kursu: {e}")
        return Reply(
            f"💱 {fmt_money(amount, src)} = *{fmt_money(value, dst)}*\n"
            f"Kurs NBP {fmt_rate(ratio)} z {day:%d.%m.%Y}"
        )

    # ---------- inwestycje (/inwestycja, /inwestycje) ----------

    async def investment_add(self, owner: str, text: str) -> Reply:
        if not text.strip():
            return Reply(
                "📈 Dodaj inwestycję, np.:\n• `/inwestycja srebro 2 uncje kupione 2024 za 600 zł`\n"
                "• `/inwestycja złoto 10 g`\n• `/inwestycja VWCE 3 sztuki za 1500 zł`\nPodgląd: `/inwestycje`"
            )
        result = await self.parser.parse_investment(text, self.today())
        d = result.investment
        if not d or d.quantity <= 0:
            return Reply(result.question or "Nie zrozumiałem — podaj co i ile, np. „srebro 2 uncje”.")
        h = Holding(id="", name=d.name.strip()[:40], kind=d.kind, symbol=d.symbol.strip().upper() if d.kind == "metal" else d.symbol.strip(),
                    quantity=round(d.quantity, 6), unit=d.unit or ("oz" if d.kind == "metal" else "szt."),
                    cost=round(d.cost_pln, 2) if d.cost_pln else None, bought=d.bought)
        key = secrets.token_urlsafe(6)
        self._inv_drafts[key] = (owner, h)
        lines = [f"📈 *Nowa inwestycja:* {h.name}", f"{h.quantity:g} {h.unit} · notowanie: {h.symbol}"]
        if h.kind == "metal" and h.unit == "oz":
            lines[-1] += f" (= {h.quantity * 31.1035:.1f} g)".replace(".", ",")
        lines.append(f"Koszt zakupu: {fmt_money(h.cost, 'PLN')}" if h.cost else "Koszt zakupu: nie podano (bez zysku/straty)")
        if h.bought:
            lines.append(f"Kupione: {h.bought}")
        return Reply("\n".join(lines), [("✅ Dodaj", f"ia:{key}"), ("❌ Anuluj", f"in:{key}")])

    async def investments_list(self) -> Reply:
        rows = await self.investments.valuate()
        if not rows:
            return await self.investment_add("", "")
        lines, total, cost_known, cost_sum = ["📈 *Inwestycje:*"], 0.0, 0.0, 0.0
        for r in rows:
            head = f"• *{r['name']}* {r['quantity']:g} {r['unit']}"
            if r["value"] is None:
                lines.append(f"{head} — {r['error']}")
                continue
            total += r["value"]
            line = f"{head}: *{fmt_money(r['value'], 'PLN')}* ({fmt_money(r['unit_pln'], 'PLN')}/{r['unit']})"
            if r.get("gain") is not None:
                cost_known += r["value"]
                cost_sum += r["cost"]
                line += f"\n   zakup {fmt_money(r['cost'], 'PLN')} → {'+' if r['gain'] >= 0 else ''}{fmt_money(r['gain'], 'PLN')} ({f"{r['gain_pct']:+.1f}".replace(".", ",")}%)"
            elif r.get("bought"):
                line += f"\n   kupione {r['bought']} — podaj koszt zakupu, żeby liczyć zysk"
            if r.get("account"):
                line += f"\n   🔄 konto „{r['account']}” w Wallet — wycena codziennie rano"
            lines.append(line)
        lines.append(f"\nRazem: *{fmt_money(total, 'PLN')}*")
        if cost_sum:
            g = cost_known - cost_sum
            lines.append(f"Zysk na pozycjach z ceną zakupu: {'+' if g >= 0 else ''}{fmt_money(g, 'PLN')}")
        lines.append("_Ceny: gold-api.com (metale), Yahoo Finance (ETF/akcje), kurs NBP._")
        return Reply("\n".join(lines), [(f"🗑 {r['name']}", f"ir:{r['id']}") for r in rows], column=True)

    async def _inv_callback(self, owner: str, action: str, key: str) -> Reply:
        if action in ("ia", "in"):
            draft = self._inv_drafts.pop(key, None)
            if not draft or draft[0] != owner:
                return Reply("Ten szkic wygasł albo został już obsłużony.")
            if action == "in":
                return Reply("❌ Anulowano.")
            h = self.investments.add(draft[1])
            rows = [r for r in await self.investments.valuate() if r["id"] == h.id]
            value = f" — dziś warte *{fmt_money(rows[0]['value'], 'PLN')}*" if rows and rows[0]["value"] else ""
            return Reply(f"✅ Dodano: {h.name} {h.quantity:g} {h.unit}{value}. Podgląd: `/inwestycje`.")
        h = self.investments.items.get(key)
        if not h:
            return Reply("Tej inwestycji już nie ma.")
        if action == "ir":
            return Reply(f"Usunąć *{h.name}* ({h.quantity:g} {h.unit}) z listy?", [("🗑 Tak", f"iry:{key}"), ("Zostaw", "keep:")])
        self.investments.remove(key)
        return Reply(f"🗑 Usunięto: {h.name}")

    # ---------- Strava i Multisport (/strava, /multisport) ----------

    async def strava_connect(self, text: str) -> Reply:
        s = self.strava
        if not s.configured:
            return Reply(
                "🔗 *Strava — konfiguracja (jednorazowo):*\n"
                "1. https://www.strava.com/settings/api → utwórz aplikację, *Authorization Callback Domain*: `localhost`.\n"
                "2. Do `.env` na serwerze dopisz `STRAVA_CLIENT_ID` i `STRAVA_CLIENT_SECRET`, potem `docker compose up -d`.\n"
                "3. Wróć tutaj i napisz /strava."
            )
        if text.strip():
            try:
                who = await s.exchange(text)
            except StravaError as e:
                return Reply(f"⚠️ {e}")
            return Reply(f"✅ Strava połączona{': ' + who if who else ''}. Sprawdź: /multisport")
        status = f"Połączona: {s.tokens.get('athlete') or 'tak'}. Żeby połączyć ponownie:\n" if s.connected else ""
        return Reply(
            f"🔗 {status}1. Kliknij przycisk poniżej i *Authorize* (zostaw zgodę na odczyt aktywności).\n"
            "2. Przeglądarka przejdzie na adres `http://localhost/exchange_token?...` — strona się nie otworzy, to normalne.\n"
            "3. Skopiuj *cały adres* z paska i wyślij: `/strava <adres>`",
            # Link jako przycisk: w treści Markdown zjadał podkreślenia (client_id → clientid).
            [("🔗 Autoryzuj w Stravie", s.auth_url())],
        )

    async def lotto_report(self) -> Reply:
        from . import lotto

        try:
            return Reply(lotto.text(await lotto.analyze(self)))
        except WalletError as e:
            return Reply(f"⚠️ {e}")

    async def multisport_report(self) -> Reply:
        from . import multisport

        try:
            return Reply(multisport.text(await multisport.analyze(self)))
        except StravaError as e:
            return Reply(f"⚠️ {e}")
        except Exception as e:
            log.exception("Multisport")
            return Reply(f"⚠️ Nie udało się pobrać aktywności: {e}")

    # ---------- korekta salda (/korekta) ----------

    async def balance_fix(self, owner: str, text: str) -> Reply:
        """„/korekta 2345,67” (konto domyślne) albo „/korekta awaryjne 500” — ile naprawdę jest na koncie."""
        m = re.fullmatch(r"\s*(.*?)\s*(-?\d[\d  ]*(?:[.,]\d{1,2})?)\s*(zł|zl|pln|eur|€)?\s*", text or "", re.I)
        if not m:
            return Reply(
                "⚖️ Podaj, ile *naprawdę* masz na koncie (z aplikacji banku):\n"
                "• `/korekta 2345,67` — konto domyślne\n• `/korekta awaryjne 500` — inne konto"
            )
        name, actual = m.group(1).strip(), float(m.group(2).replace(" ", "").replace(" ", "").replace(",", "."))
        try:
            accounts = {a["id"]: a for a in await self.wallet.accounts()}  # świeże salda, bez cache
        except WalletError as e:
            return Reply(f"⚠️ {e}")
        acc = None
        if name:
            low = name.lower()
            acc = next((a for a in accounts.values() if a["name"].lower() == low), None) or next(
                (a for a in accounts.values() if a["name"].lower().startswith(low[:4])), None)
            if not acc:
                return Reply(f"Nie znam konta „{name}”. Konta: " + ", ".join(a["name"] for a in accounts.values()))
        acc = acc or accounts.get(self._default_account_id) or next(iter(accounts.values()))
        bal = acc.get("balance") or {}
        current, cur = float(bal.get("currentBalance", 0)), acc.get("currencyCode", "")
        diff = round(actual - current, 2)
        if abs(diff) < 0.01:
            return Reply(f"✅ *{acc['name']}*: {fmt_money(current, cur)} — zgadza się z bankiem.")
        key = secrets.token_urlsafe(6)
        self._fix_drafts[key] = (owner, acc["id"], diff, actual, float(bal.get("initial", 0)), cur, acc["name"])
        return Reply(
            f"⚖️ *{acc['name']}*\nW Wallet: {fmt_money(current, cur)}\nW banku: {fmt_money(actual, cur)}\n"
            f"Różnica: *{'+' if diff > 0 else ''}{fmt_money(diff, cur)}*\n\n"
            "📝 *Wpis* — drobny rozjazd (zapomniany paragon, opłata); liczy się w statystykach.\n"
            "⚖️ *Saldo początkowe* — punkt startowy konta (np. pierwsze ustawienie); statystyki bez zmian.",
            [("📝 Zapisz jako wpis", f"kr:{key}"), ("⚖️ Saldo początkowe", f"ki:{key}"), ("❌ Anuluj", f"kn:{key}")],
        )

    async def _fix_callback(self, owner: str, action: str, key: str) -> Reply:
        draft = self._fix_drafts.pop(key, None)
        if not draft or draft[0] != owner:
            return Reply("Ta korekta wygasła albo została już obsłużona.")
        _, acc_id, diff, actual, initial, cur, name = draft
        if action == "kn":
            return Reply("❌ Anulowano.")
        if action == "ki":
            try:
                await self.wallet.set_initial_balance(acc_id, initial + diff)
            except WalletError as e:
                return Reply(f"⚠️ {e}")
            return Reply(f"⚖️ *{name}*: saldo początkowe {fmt_money(initial, cur)} → {fmt_money(initial + diff, cur)}. "
                         f"Saldo teraz {fmt_money(actual, cur)}.")
        await self.refresh_catalog()
        r = ParsedRecord(
            amount=abs(diff), currency=None, type="income" if diff > 0 else "expense",
            category_id=UNKNOWN_INCOME if diff > 0 else UNKNOWN_EXPENSE, account_id=acc_id,
            date=self.today().isoformat(), counterparty=None,
            note=f"Korekta salda (bank: {fmt_money(actual, cur)})",
        )
        return await self.save_records(owner, [r])

    async def forecast(self) -> Reply:
        from . import forecast

        try:
            data = await forecast.build(self)
        except WalletError as e:
            return Reply(f"⚠️ {e}")
        except Exception as e:  # np. błąd w config/forecast.yaml
            log.exception("Prognoza")
            return Reply(f"⚠️ Nie udało się policzyć prognozy: {e}")
        return Reply(forecast.summary_text(data, self.cfg.forecast_url))

    # ---------- zadania, książki, dziennik ----------

    def _log_usage(self, name: str, model: str, usage, ms: int) -> None:
        self.store.log_event(
            "llm", trace=events.trace_var.get(), agent=events.agent_var.get(), name=name, model=model,
            tokens_in=events.tokens_in(usage), tokens_out=getattr(usage, "output_tokens", 0),
            cost_usd=events.cost_usd(model, usage), ms=ms,
        )

    async def tasks_list(self) -> Reply:
        await self.calendar_pull()
        today = self.today().isoformat()
        evs = await self.calendar_events(self.today(), 1)
        return self.lists.tasks_view([e for e in evs if e["start"][:10] <= today])  # wielodniowe też, gdy trwają

    # ---------- Kalendarz Google (/google, /kalendarz) ----------

    def calendar_sync(self, task_id: int) -> None:
        """Po zmianie zadania: w tle zaktualizuj jego wydarzenie (bot nie czeka na Google)."""
        if self.gcal.connected:
            self._spawn(self._calendar_sync(task_id))

    def calendar_delete(self, event_id: str) -> None:
        if self.gcal.connected:
            self._spawn(self._calendar_delete(event_id))

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    def find_covers(self) -> None:
        """W tle: okładki dla książek, które jeszcze ich nie mają (po dodaniu i co obrót pętli)."""
        if os.getenv("BOOK_COVERS", "on") == "off":  # testy offline: bez sieci
            return
        if not self._covers_running and self.store.books_without_cover(1):
            self._covers_running = True
            self._spawn(self._find_covers())

    async def _find_covers(self) -> None:
        from .covers import find_cover

        try:
            for b in self.store.books_without_cover():
                try:
                    url, pages = await find_cover(self._covers_http, b.title, b.author)
                except Exception as e:  # sieć — spróbujemy przy następnym obrocie pętli
                    log.warning("Okładka „%s”: %r", b.title, e)
                    return
                if self.store.book(b.id):
                    self.store.set_book_cover(b.id, url, pages)  # liczba stron tylko, gdy jeszcze jej nie ma
        finally:
            self._covers_running = False

    async def calendar_idle(self) -> None:
        """Czeka, aż skończą się synchronizacje w tle (testy, zamykanie)."""
        while self._bg:
            await asyncio.gather(*list(self._bg), return_exceptions=True)

    async def _calendar_sync(self, task_id: int) -> None:
        async with self._gcal_lock:
            t = self.store.task(task_id)  # stan z chwili wysyłki, nie z chwili zmiany
            if not t:
                return
            try:
                gid = await self.gcal.sync_task(t)
                if not self.store.task(task_id):  # usunięte, zanim Google odpowiedział
                    if gid:
                        await self.gcal.delete_event(gid)
                elif gid != t.gcal_id:
                    self.store.set_task(task_id, gcal_id=gid)
            except Exception as e:
                log.warning("Kalendarz Google: zadanie %s: %r", task_id, e)
                self.store.log_event("error", agent="kalendarz", name="gcal_sync", input=t.title, output=str(e)[:300])

    async def _calendar_delete(self, event_id: str) -> None:
        async with self._gcal_lock:
            try:
                await self.gcal.delete_event(event_id)
            except Exception as e:
                log.warning("Kalendarz Google: usuwanie %s: %r", event_id, e)
                self.store.log_event("error", agent="kalendarz", name="gcal_delete", output=str(e)[:300])

    async def calendar_pull(self) -> list[str]:
        """Zmiany zrobione w samym Kalendarzu Google wracają do listy: usunięte wydarzenie = usunięte
        zadanie, przeniesione = nowy termin, inna nazwa = nowy tytuł. Zwraca opis zmian."""
        if not self.gcal.connected:
            return []
        lines = []
        async with self._gcal_lock:
            try:
                changes = await asyncio.wait_for(self.gcal.task_changes(), 10)
            except Exception as e:
                log.warning("Kalendarz Google: zmiany z kalendarza: %r", e)
                return []
            by_event = {t.gcal_id: t for t in self.store.all_tasks() if t.gcal_id}
            for c in changes:
                t = by_event.get(c["id"])
                if not t:
                    continue
                if c["deleted"]:
                    self.store.delete_task(t.id)
                    lines.append(f"🗑 Usunięte w kalendarzu: {t.title}")
                    continue
                fields = {}
                if c["title"] and c["title"] != t.title:
                    fields["title"] = c["title"][:200]
                if c["due"] and (c["due"], c["due_time"]) != (t.due, t.due_time):
                    fields.update(due=c["due"], due_time=c["due_time"])
                if fields:
                    self.store.set_task(t.id, **fields)
                    lines.append(f"📅 Zmienione w kalendarzu: {fields.get('title', t.title)}")
        if lines:
            self.store.log_event("calendar", agent="kalendarz", name="gcal_pull", output="\n".join(lines))
        return lines

    async def calendar_events(self, start: date, days: int) -> list[dict]:
        """Twoje wydarzenia z Kalendarza Google; pusto, gdy niepołączony albo Google nie odpowiada."""
        if not self.gcal.connected:
            return []
        try:
            return await asyncio.wait_for(self.gcal.events(start, days), 10)  # wolny Google nie blokuje listy
        except Exception as e:
            log.warning("Kalendarz Google: odczyt wydarzeń: %r", e)
            return []

    async def _calendar_warnings(self, task_ids: list[int]) -> list[str]:
        out = []
        for tid in task_ids:
            t = self.store.task(tid)
            if not t or not t.due_time or not t.due_date:
                continue
            clash = conflicts(await self.calendar_events(t.due_date, 1), t.due_date, t.due_time)
            if clash:
                out.append(f"⚠️ O {t.due_time} masz już: " + ", ".join(_md_safe(e["title"]) for e in clash))
        return out

    async def calendar_report(self) -> Reply:
        """/kalendarz: dziś i jutro z Kalendarza Google oraz zadania z terminem."""
        if not self.gcal.connected:
            return await self.google_connect("")
        await self.calendar_pull()
        today = self.today()
        try:
            evs = await self.gcal.events(today, 2)
        except Exception as e:
            return Reply(f"⚠️ Nie mogę odczytać kalendarza: {e}")
        tasks = self.store.open_tasks()
        lines = []
        for label, d in (("Dziś", today), ("Jutro", today + timedelta(days=1))):
            day_ev = [e for e in evs if e["start"][:10] == d.isoformat()]
            day_t = [t for t in tasks if t.due_date == d]
            lines.append(f"*{label}, {d:%d.%m}*")
            lines += [_md_safe(event_line(e)) for e in day_ev]
            lines += [f"🗒 {t.due_time or 'cały dzień'} {_md_safe(t.title)}" for t in day_t]
            if not day_ev and not day_t:
                lines.append("— wolne")
            lines.append("")
        return Reply("\n".join(lines).strip())

    async def google_connect(self, text: str) -> Reply:
        g = self.gcal
        if not g.configured:
            return Reply(
                "🔗 *Kalendarz Google — konfiguracja (jednorazowo):*\n"
                "1. https://console.cloud.google.com → nowy projekt → włącz *Google Calendar API*.\n"
                "2. *OAuth consent screen*: External, dodaj siebie, potem *Publish app* (w trybie Testing dostęp wygasa po 7 dniach).\n"
                "3. *Credentials → OAuth client ID*, typ *Desktop app*.\n"
                "4. Do `.env` na serwerze dopisz `GOOGLE_CLIENT_ID` i `GOOGLE_CLIENT_SECRET`, potem `docker compose up -d`.\n"
                "5. Wróć tutaj i napisz /google."
            )
        if text.strip():
            try:
                await g.exchange(text)
            except GoogleError as e:
                return Reply(f"⚠️ {e}")
            dated = [t for t in self.store.open_tasks() if t.due_date]
            for t in dated:  # zadania sprzed połączenia też trafiają do kalendarza
                self.calendar_sync(t.id)
            moved = f" Wysyłam do niego zadania z terminem: {len(dated)}." if dated else ""
            return Reply(f"✅ Kalendarz Google połączony. Zadania z terminem będą w kalendarzu „Zadania”.{moved}\n"
                         "Twoje spotkania zobaczysz w /zadania i /kalendarz.")
        status = "Połączony. Żeby połączyć ponownie:\n" if g.connected else ""
        return Reply(
            f"🔗 {status}1. Kliknij przycisk poniżej, wybierz konto i zezwól na dostęp do kalendarza.\n"
            "2. Google może ostrzec, że aplikacja nie jest zweryfikowana — to Twoja aplikacja: *Zaawansowane → Przejdź*.\n"
            "3. Przeglądarka przejdzie na adres `http://localhost/?code=...` — strona się nie otworzy, to normalne.\n"
            "4. Skopiuj *cały adres* z paska i wyślij: `/google <adres>`",
            [("🔗 Połącz z Google", g.auth_url())],
        )

    async def books_list(self) -> Reply:
        return self.lists.books_view()

    async def add_item(self, owner: str, kind: str, text: str) -> Reply | list[Reply]:
        """/zadanie i /ksiazka: to samo co zwykła wiadomość, z jasną intencją dla modelu."""
        from .lists import BOOKS_HELP, TASKS_HELP

        if not text.strip():
            return Reply(TASKS_HELP if kind == "task" else BOOKS_HELP)
        return await self.handle_message(owner, f"{'Dodaj zadanie' if kind == 'task' else 'Dodaj książkę'}: {text}", [])

    async def event_log(self) -> Reply:
        tz = self.cfg.tz
        month, calls = self.store.cost_since(month_start(tz))
        today, _ = self.store.cost_since(day_start(tz))
        rows = self.store.recent_events(15)
        lines = [f"🔎 *Dziennik* (ostatnie {len(rows)})"]
        icons = {"message": "💬", "button": "🔘", "command": "⌨️", "llm": "🧠", "notify": "🔔", "error": "⚠️"}
        for e in rows:
            at = datetime.fromtimestamp(e["at"], tz).strftime("%d.%m %H:%M")
            who = f"[{e['agent']}] " if e["agent"] else ""
            if e["kind"] == "llm":
                what = f"{e['name']} · {e['tokens_in']}→{e['tokens_out']} tok · ${e['cost_usd']:.4f}"
            else:
                what = (e["input"] or e["name"] or "").replace("\n", " ")[:50]
            took = f" · {e['ms'] / 1000:.1f}s" if e["ms"] else ""
            lines.append(f"{icons.get(e['kind'], '•')} `{at}` {who}{_md_safe(what)}{took}")
        limit = f" z limitu ${self.cfg.llm_budget_usd:.2f}" if self.cfg.llm_budget_usd else ""
        lines.append(f"\n💵 Model: dziś {_usd(today)}, w tym miesiącu *{_usd(month)}*{limit} (wywołania: {calls})")
        return Reply("\n".join(lines))

    def budget_alert(self) -> Reply | None:
        """Raz w miesiącu: koszt modelu przekroczył LLM_BUDGET_USD."""
        limit = self.cfg.llm_budget_usd
        if not limit:
            return None
        spent, _ = self.store.cost_since(month_start(self.cfg.tz))
        month = self.today().strftime("%Y-%m")
        if spent < limit or self.store.get("budget_alert") == month:
            return None
        self.store.set("budget_alert", month)
        return Reply(f"💵 Koszt modelu w tym miesiącu: *${spent:.2f}*, limit ${limit:.2f}. "
                     "Sprawdź `/log`, co zużywa najwięcej, albo podnieś `LLM_BUDGET_USD`.")

    async def close(self) -> None:
        self.store.close()
        await self.wallet.close()
        await self.fx.close()
        await self.investments.close()
        await self.strava.close()
        await self.calendar_idle()
        await self.gcal.close()
        await self._covers_http.aclose()


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _md_safe(s: str) -> str:
    return s.replace("*", "").replace("_", " ").replace("`", "'").replace("[", "(").replace("]", ")")


def _usd(v: float) -> str:
    return f"${v:.3f}" if v < 1 else f"${v:.2f}"


HELP = """👋 Zapisuję wydatki i przychody do Wallet, a do tego pilnuję zadań i książek.

Po prostu napisz, np.:
• `biedronka 54,30`
• `paliwo 250 orlen wczoraj`
• `kawa 14 i ciastko 9`
• `wypłata 6200`
• `anthropic 15$`  → przeliczę na PLN po kursie NBP
albo wyślij *zdjęcie paragonu*.

Pamiętam ostatnie ~30 min rozmowy, więc możesz poprawiać:
`zmień na 45` · `to było wczoraj` · `kategoria restauracje` · `i jeszcze parking 12`
🧠 Uczę się z zapisów: następny paragon z tego samego sklepu dostanie tę samą kategorię i konto. Lista: `/pamiec`

Płatności cykliczne: przypominam w dniu terminu — ✅ / ✏️ / ⏭.
Nowa: `/plan netflix 49 co miesiąc 15-go` · lista i usuwanie: `/plany`
Kursy walut: `/kurs` · `/kurs 100 eur` · `/kurs 50 usd eur`
Inwestycje: `/inwestycja srebro 2 uncje` · wycena: `/inwestycje`
Multisport: opłacalność ze Stravy — `/multisport`
Eurojackpot: wydane, wygrane, zwrot i szanse — `/lotto`
Długi: `/dlug A6 9100` · spłata: „spłata A6 500” · stan: `/dlugi`
Wyciąg z banku: wklej treść maila albo wyślij plik .eml — porównam z Wallet i pokażę, czego brakuje.
🎤 Możesz też nagrać głosówkę.

🗒 Zadania: napisz `jutro zadzwonić do ubezpieczyciela` · lista: `/zadania` · odhaczanie: „zadzwoniłem…” albo przycisk
📚 Książki: wyślij *screena* okładki, sklepu albo półki, albo napisz `chcę przeczytać Sapiensa` · lista: `/ksiazki`
🔎 Co robiłem i ile kosztował model: `/log`

Saldo się nie zgadza z bankiem? `/korekta 2345,67` (albo `/korekta awaryjne 500`).

Komendy: zadania · zadanie · ksiazki · ksiazka · saldo · miesiac · pamiec · prognoza · zaplanowane · plan · plany · dlug · dlugi · inwestycje · multisport · lotto · kurs · wyciag · korekta · odswiez · log · pomoc"""

PLAN_HELP = """🗓 Dodawanie płatności cyklicznej — opisz ją po ludzku, np.:
• `/plan netflix 49 co miesiąc 15-go`
• `/plan czynsz 1800 10-tego`
• `/plan OC auta 1200 co rok 20 marca`
• `/plan rata telefonu 89 co miesiąc 5-go, 12 rat`
• `/plan pensja 6200 10-go`

Lista i usuwanie: `/plany`"""
