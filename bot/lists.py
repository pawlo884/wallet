"""Zadania i książki: zastosowanie akcji z modelu, widoki /zadania i /ksiazki, przyciski."""

import secrets
from datetime import date, timedelta
from typing import TYPE_CHECKING

from .parser import BookAction, TaskAction
from .store import BOOK_STATUSES, Book, Store, Task

if TYPE_CHECKING:
    from .core import Core, Reply

UNDO_TTL = 7 * 24 * 3600
_NEXT_STATUS = {"to_buy": "to_read", "to_read": "reading", "reading": "read"}
_WEEKDAYS = ["pon", "wt", "śr", "czw", "pt", "sob", "nd"]


def fmt_due(d: date | None, today: date) -> str:
    if not d:
        return ""
    if d == today:
        return "dziś"
    if d == today + timedelta(days=1):
        return "jutro"
    if d < today:
        return f"zaległe od {d:%d.%m}"
    if d < today + timedelta(days=7):
        return f"{_WEEKDAYS[d.weekday()]} {d:%d.%m}"
    return f"{d:%d.%m.%Y}"


class Lists:
    def __init__(self, core: "Core", store: Store):
        self.core = core
        self.store = store
        # klucz → lista operacji do cofnięcia: ["task_add", id] | ["book_add", id] | ["book_status", id, stary]
        #   | ["task_done", id] | ["task_due", id, stary_termin]
        self._undo = core.session.dict("lists_undo", UNDO_TTL)

    # ---------- blok dla modelu ----------

    def prompt_block(self) -> str:
        today = self.core.today()
        tasks = self.store.open_tasks()[:60]
        books = [b for b in self.store.books() if b.status != "read"][:80]
        lines = ["ZADANIA (otwarte; id | tytuł | termin):"]
        lines += [f"{t.id} | {t.title} | {t.due or '-'}" for t in tasks] or ["(brak)"]
        lines.append("\nKSIĄŻKI (id | tytuł | autor | status):")
        lines += [f"{b.id} | {b.title} | {b.author or '-'} | {b.status}" for b in books] or ["(brak)"]
        lines.append(f"\n(dziś {today.isoformat()})")
        return "\n".join(lines)

    # ---------- akcje z modelu ----------

    def apply(self, tasks: list[TaskAction], books: list[BookAction]) -> tuple[list[str], str | None]:
        """Wykonuje akcje od razu. Zwraca (linie opisu, klucz cofnięcia albo None)."""
        today = self.core.today()
        lines: list[str] = []
        undo: list[list] = []
        for a in tasks:
            due = _valid_date(a.due)
            if a.action == "add":
                if not a.title.strip():
                    continue
                t = self.store.add_task(a.title, due.isoformat() if due else None)
                undo.append(["task_add", t.id])
                when = fmt_due(t.due_date, today)
                lines.append(f"✅ Nowe zadanie: *{t.title}*" + (f" ({when})" if when else ""))
                continue
            t = self.store.task(a.task_id or 0)
            if not t or t.status != "open":
                lines.append(f"❓ Nie znalazłem zadania „{a.title}” na liście.")
                continue
            if a.action == "done":
                self.store.finish_task(t.id)
                undo.append(["task_done", t.id])
                lines.append(f"☑️ Zrobione: ~{t.title}~")
            else:  # postpone
                self.store.postpone_task(t.id, due.isoformat() if due else None)
                undo.append(["task_due", t.id, t.due])
                lines.append(f"⏭ Przesunięte: *{t.title}* → {fmt_due(due, today) or 'bez terminu'}")
        for a in books:
            if a.action == "add" or not (b := self.store.book(a.book_id or 0)):
                if not a.title.strip():
                    continue
                b, created = self.store.add_book(a.title, a.author, a.status)
                if created:
                    undo.append(["book_add", b.id])
                    lines.append(f"📚 Dodana: *{b.label()}* · {BOOK_STATUSES[b.status]}")
                    continue
                if b.status == a.status:
                    lines.append(f"📚 Już jest na liście: *{b.label()}* · {BOOK_STATUSES[b.status]}")
                    continue
            if b.status != a.status:
                undo.append(["book_status", b.id, b.status])
                self.store.set_book_status(b.id, a.status)
                lines.append(f"📚 *{b.label()}* → {BOOK_STATUSES[a.status]}")
        if not undo:
            return lines, None
        key = secrets.token_urlsafe(6)
        self._undo[key] = undo
        return lines, key

    def undo(self, key: str) -> str:
        ops = self._undo.pop(key, None)
        if not ops:
            return "Nie ma już czego cofać."
        for op in reversed(ops):
            kind, oid = op[0], op[1]
            if kind == "task_add":
                self.store.delete_task(oid)
            elif kind == "task_done":
                self.store.set_task(oid, status="open", done_at=None)
            elif kind == "task_due":
                self.store.set_task(oid, due=op[2])
            elif kind == "book_add":
                self.store.delete_book(oid)
            elif kind == "book_status":
                self.store.set_book_status(oid, op[2])
        return "↩️ Cofnięte."

    # ---------- /zadania ----------

    def tasks_view(self) -> "Reply":
        from .core import Reply

        today = self.core.today()
        tasks = self.store.open_tasks()
        if not tasks:
            return Reply(TASKS_HELP)
        overdue = [t for t in tasks if t.due_date and t.due_date < today]
        now = [t for t in tasks if t.due_date == today]
        soon = [t for t in tasks if t.due_date and t.due_date > today]
        later = [t for t in tasks if not t.due_date]
        lines = [f"🗒 *Zadania ({len(tasks)})*"]
        for title, group in (("⚠️ Zaległe", overdue), ("📌 Dziś", now), ("🗓 Najbliższe", soon), ("💭 Bez terminu", later)):
            if group:
                lines.append(f"\n*{title}*")
                lines += [self._task_line(t, today) for t in group]
        done_today = self.store.done_tasks_since(_day_start_ts(self.core))
        if done_today:
            lines.append(f"\n☑️ Dziś zrobione: {len(done_today)}")
        lines.append("\nDotknij zadania, żeby je odhaczyć albo przesunąć.")
        shown = (overdue + now + soon + later)[:30]
        return Reply("\n".join(lines), [(f"{t.title[:40]}", f"tk:{t.id}") for t in shown], column=True)

    @staticmethod
    def _task_line(t: Task, today: date) -> str:
        when = fmt_due(t.due_date, today)
        moved = f" · przesuwane {t.moves}×" if t.moves >= 2 else ""
        return f"• {t.title}" + (f" — {when}" if when else "") + moved

    # ---------- /ksiazki ----------

    def books_view(self) -> "Reply":
        from .core import Reply

        books = self.store.books()
        if not books:
            return Reply(BOOKS_HELP)
        by_status: dict[str, list[Book]] = {s: [] for s in ("reading", "to_read", "to_buy", "read")}
        for b in books:
            by_status.setdefault(b.status, []).append(b)
        lines = [f"📚 *Książki ({len(books)})*"]
        for status in ("reading", "to_read", "to_buy"):
            group = by_status[status]
            if group:
                lines.append(f"\n*{BOOK_STATUSES[status]} ({len(group)})*")
                lines += [f"• {b.label()}" for b in group]
        if read := by_status["read"]:
            lines.append(f"\n*{BOOK_STATUSES['read']} ({len(read)})*: " + ", ".join(b.title for b in read[:10])
                         + ("…" if len(read) > 10 else ""))
        lines.append("\nDotknij książki, żeby zmienić status. Nowe: napisz tytuł albo wyślij screena.")
        active = by_status["reading"] + by_status["to_read"] + by_status["to_buy"]
        icon = {"reading": "📖", "to_read": "📚", "to_buy": "🛒"}
        return Reply("\n".join(lines), [(f"{icon[b.status]} {b.title[:40]}", f"bk:{b.id}") for b in active[:30]],
                     column=True)

    # ---------- przyciski ----------

    def handle_callback(self, action: str, key: str) -> "Reply":
        from .core import Reply

        today = self.core.today()
        if action == "iu":
            return Reply(self.undo(key))
        if action in ("tk", "td", "tp", "tn", "tx", "txy"):
            t = self.store.task(int(key) if key.isdigit() else 0)
            if not t or (t.status != "open" and action != "txy"):
                return Reply("Tego zadania już nie ma na liście.")
            if action == "tk":
                when = fmt_due(t.due_date, today)
                return Reply(
                    f"🗒 *{t.title}*" + (f"\nTermin: {when}" if when else ""),
                    [("☑️ Zrobione", f"td:{t.id}"), ("⏭ Jutro", f"tp:{t.id}"), ("📅 Za tydzień", f"tn:{t.id}"),
                     ("🗑 Usuń", f"tx:{t.id}")],
                )
            if action == "td":
                self.store.finish_task(t.id)
                left = len(self.store.open_tasks())
                return Reply(f"☑️ Zrobione: ~{t.title}~\nZostało: {left}")
            if action in ("tp", "tn"):
                due = today + timedelta(days=1 if action == "tp" else 7)
                self.store.postpone_task(t.id, due.isoformat())
                note = "\n_To już trzecie przesunięcie. Może rozbić je na mniejsze kroki albo skreślić?_" if t.moves + 1 >= 3 else ""
                return Reply(f"⏭ *{t.title}* → {fmt_due(due, today)}{note}")
            if action == "tx":
                return Reply(f"Usunąć zadanie *{t.title}*?", [("🗑 Tak", f"txy:{t.id}"), ("Zostaw", "keep:")])
            self.store.delete_task(t.id)
            return Reply(f"🗑 Usunięto: {t.title}")
        # książki
        bid, _, status = key.partition(".")
        b = self.store.book(int(bid) if bid.isdigit() else 0)
        if not b:
            return Reply("Tej książki już nie ma na liście.")
        if action == "bk":
            buttons = [(label, f"bs:{b.id}.{s}") for s, label in BOOK_STATUSES.items() if s != b.status]
            nxt = _NEXT_STATUS.get(b.status)
            buttons.sort(key=lambda x: x[1] != f"bs:{b.id}.{nxt}")  # najbardziej prawdopodobny pierwszy
            buttons.append(("🗑 Usuń", f"bx:{b.id}"))
            return Reply(f"📚 *{b.label()}*\nTeraz: {BOOK_STATUSES[b.status]}", buttons, column=True)
        if action == "bs" and status in BOOK_STATUSES:
            self.store.set_book_status(b.id, status)
            extra = "\n🎉 Gratulacje! Kolejna przeczytana." if status == "read" else ""
            return Reply(f"📚 *{b.label()}* → {BOOK_STATUSES[status]}{extra}")
        if action == "bx":
            return Reply(f"Usunąć *{b.label()}* z listy?", [("🗑 Tak", f"bxy:{b.id}"), ("Zostaw", "keep:")])
        if action == "bxy":
            self.store.delete_book(b.id)
            return Reply(f"🗑 Usunięto: {b.title}")
        return Reply("Nieznana akcja.")


def _valid_date(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def _day_start_ts(core: "Core") -> float:
    from datetime import datetime, time as dtime

    return datetime.combine(core.today(), dtime.min, tzinfo=core.cfg.tz).timestamp()


TASKS_HELP = """🗒 Lista zadań jest pusta. Napisz po prostu, np.:
• `jutro zadzwonić do ubezpieczyciela`
• `do soboty wymienić opony`
• `zadanie: oddać książkę do biblioteki`
albo `/zadanie kupić prezent dla mamy`.

Odhaczanie: „zadzwoniłem do ubezpieczyciela” albo przycisk w `/zadania`."""

BOOKS_HELP = """📚 Lista książek jest pusta. Dodaj je:
• wyślij *screena* okładki, strony sklepu albo swojej półki (Legimi, Empik, Kindle),
• napisz `chcę przeczytać Atomowe nawyki` albo `mam już Sapiensa`,
• `/ksiazka Diuna Frank Herbert`.

Statusy: 🛒 do kupienia → 📚 do przeczytania → 📖 czytam → ✅ przeczytane."""
