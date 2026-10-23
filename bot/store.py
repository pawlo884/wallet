"""Baza asystenta (SQLite na wolumenie, data/assistant.db): zadania, książki, dziennik zdarzeń.

Jedna baza dla wszystkich przyszłych agentów. Dostęp synchroniczny: zapytania trwają ułamki
milisekundy, a bot obsługuje jedną osobę, więc nie ma sensu dokładać wątków.
"""

import json
import logging
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    due TEXT,                      -- YYYY-MM-DD albo NULL (bez terminu)
    status TEXT NOT NULL DEFAULT 'open',  -- open | done
    moves INTEGER NOT NULL DEFAULT 0,      -- ile razy przesunięte (do pilnowania w etapie 2)
    created_at REAL NOT NULL,
    done_at REAL,
    due_time TEXT,                 -- HH:MM albo NULL (cały dzień)
    gcal_id TEXT                   -- ID wydarzenia w Kalendarzu Google (kalendarz „Zadania”)
);
CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    author TEXT,
    status TEXT NOT NULL,          -- to_buy | to_read | reading | read
    note TEXT,
    key TEXT NOT NULL UNIQUE,      -- tytuł bez wielkości liter i ogonków (żeby nie dublować)
    created_at REAL NOT NULL,
    status_at REAL NOT NULL,
    cover_url TEXT,                -- okładka do panelu: NULL = nie szukano, "" = nie znaleziono
    pages INTEGER,                 -- liczba stron (Google Books / Open Library / użytkownik); 0 = nie znaleziono
    page INTEGER NOT NULL DEFAULT 0  -- strona, na której jestem
);
CREATE TABLE IF NOT EXISTS reading (  -- dziennik czytania: ile stron przybyło i kiedy
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    book_id INTEGER NOT NULL,
    pages INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS reading_at ON reading(at);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    trace TEXT,                    -- zdarzenia jednej wiadomości (wiadomość + wywołania modelu)
    kind TEXT NOT NULL,            -- message | button | command | llm | notify | error
    agent TEXT,
    name TEXT,
    input TEXT,
    output TEXT,
    model TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_usd REAL,
    ms INTEGER
);
CREATE INDEX IF NOT EXISTS events_at ON events(at);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""

# kolumny dodane po pierwszym wdrożeniu (CREATE TABLE IF NOT EXISTS nie zmienia istniejącej tabeli)
MIGRATIONS = {"tasks": {"due_time": "TEXT", "gcal_id": "TEXT"}, "books": {"cover_url": "TEXT", "pages": "INTEGER",
                                                                         "page": "INTEGER NOT NULL DEFAULT 0"}}

EVENTS_KEEP_DAYS = 180

BOOK_STATUSES = {
    "to_buy": "🛒 Do kupienia",
    "to_read": "📚 Do przeczytania",
    "reading": "📖 Czytam",
    "read": "✅ Przeczytane",
}


def book_key(title: str) -> str:
    s = unicodedata.normalize("NFKD", title.lower().replace("ł", "l"))
    s = "".join(c for c in s if c.isalnum() or c == " ")
    return " ".join(s.split())


@dataclass
class Task:
    id: int
    title: str
    due: str | None
    status: str
    moves: int
    created_at: float
    done_at: float | None
    due_time: str | None = None
    gcal_id: str | None = None

    @property
    def due_date(self) -> date | None:
        try:
            return date.fromisoformat(self.due) if self.due else None
        except ValueError:
            return None


@dataclass
class Book:
    id: int
    title: str
    author: str | None
    status: str
    note: str | None
    key: str
    created_at: float
    status_at: float
    cover_url: str | None = None
    pages: int | None = None
    page: int = 0

    def label(self) -> str:
        return f"{self.title} — {self.author}" if self.author else self.title

    def progress(self) -> str:
        """„str. 120/600 · 20%” (albo samo „str. 120”, gdy nie znamy liczby stron)."""
        if not self.page:
            return f"{self.pages} str." if self.pages else ""
        if not self.pages:
            return f"str. {self.page}"
        return f"str. {self.page}/{self.pages} · {min(100, round(100 * self.page / self.pages))}%"


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)  # autocommit
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.db.execute("DELETE FROM events WHERE at < ?", (time.time() - EVENTS_KEEP_DAYS * 86400,))

    def close(self) -> None:
        self.db.close()

    # ---------- kv (drobny stan, np. wysłane alerty) ----------

    def get(self, k: str, default=None):
        row = self.db.execute("SELECT v FROM kv WHERE k = ?", (k,)).fetchone()
        return json.loads(row["v"]) if row else default

    def set(self, k: str, v) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (k, json.dumps(v, ensure_ascii=False)))

    # ---------- zadania ----------

    def add_task(self, title: str, due: str | None, due_time: str | None = None) -> Task:
        cur = self.db.execute(
            "INSERT INTO tasks (title, due, due_time, created_at) VALUES (?, ?, ?, ?)",
            (title.strip()[:200], due, due_time if due else None, time.time()),
        )
        return self.task(cur.lastrowid)

    def task(self, tid: int) -> Task | None:
        row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        return Task(**dict(row)) if row else None

    def open_tasks(self) -> list[Task]:
        rows = self.db.execute(
            "SELECT * FROM tasks WHERE status = 'open' ORDER BY due IS NULL, due, id"
        ).fetchall()
        return [Task(**dict(r)) for r in rows]

    def all_tasks(self) -> list[Task]:
        return [Task(**dict(r)) for r in self.db.execute("SELECT * FROM tasks")]

    def done_tasks_since(self, since: float) -> list[Task]:
        rows = self.db.execute(
            "SELECT * FROM tasks WHERE status = 'done' AND done_at >= ? ORDER BY done_at", (since,)
        ).fetchall()
        return [Task(**dict(r)) for r in rows]

    def set_task(self, tid: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), tid))

    def finish_task(self, tid: int) -> None:
        self.set_task(tid, status="done", done_at=time.time())

    def postpone_task(self, tid: int, due: str | None, due_time: str | None = None) -> None:
        """Nowy termin; godzina przepada, chyba że podana (\"przełóż na piątek o 10\")."""
        self.db.execute(
            "UPDATE tasks SET due = ?, due_time = ?, moves = moves + 1 WHERE id = ?",
            (due, due_time if due else None, tid),
        )

    def delete_task(self, tid: int) -> None:
        self.db.execute("DELETE FROM tasks WHERE id = ?", (tid,))

    # ---------- książki ----------

    def add_book(self, title: str, author: str | None, status: str, note: str | None = None) -> tuple[Book, bool]:
        """Zwraca (książka, czy_nowa). Ta sama książka drugi raz nie jest dublowana."""
        key = book_key(title)
        if existing := self.book_by_key(key):
            if author and not existing.author:
                self.db.execute("UPDATE books SET author = ? WHERE id = ?", (author, existing.id))
            return self.book(existing.id), False
        now = time.time()
        cur = self.db.execute(
            "INSERT INTO books (title, author, status, note, key, created_at, status_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (title.strip()[:200], (author or "").strip()[:120] or None, status, note, key, now, now),
        )
        return self.book(cur.lastrowid), True

    def books_without_cover(self, limit: int = 20) -> list[Book]:
        # pages IS NULL: książki sprzed liczenia stron (0 = szukano i nie ma)
        rows = self.db.execute("SELECT * FROM books WHERE cover_url IS NULL OR pages IS NULL ORDER BY id DESC LIMIT ?",
                               (limit,))
        return [Book(**dict(r)) for r in rows]

    def book(self, bid: int) -> Book | None:
        row = self.db.execute("SELECT * FROM books WHERE id = ?", (bid,)).fetchone()
        return Book(**dict(row)) if row else None

    def book_by_key(self, key: str) -> Book | None:
        row = self.db.execute("SELECT * FROM books WHERE key = ?", (key,)).fetchone()
        return Book(**dict(row)) if row else None

    def books(self) -> list[Book]:
        rows = self.db.execute("SELECT * FROM books ORDER BY status_at DESC, id DESC").fetchall()
        return [Book(**dict(r)) for r in rows]

    def set_book_status(self, bid: int, status: str) -> int | None:
        """Przeczytana = do ostatniej strony (reszta stron liczy się do dziennika). Zwraca id wpisu albo None."""
        self.db.execute("UPDATE books SET status = ?, status_at = ? WHERE id = ?", (status, time.time(), bid))
        b = self.book(bid)
        if status == "read" and b and b.pages and b.page < b.pages:
            return self.set_book_page(bid, b.pages)
        return None

    def set_book_cover(self, bid: int, url: str, pages: int | None = None) -> None:
        """Wynik szukania: znaleziona okładka nie jest nadpisywana, liczba stron od użytkownika też."""
        self.db.execute(
            "UPDATE books SET cover_url = CASE WHEN COALESCE(cover_url, '') != '' THEN cover_url ELSE ? END,"
            " pages = COALESCE(pages, ?) WHERE id = ?", (url, pages or 0, bid))

    def set_book_pages(self, bid: int, pages: int | None) -> None:
        self.db.execute("UPDATE books SET pages = ? WHERE id = ?", (pages, bid))

    def set_book_page(self, bid: int, page: int) -> int | None:
        """Ustawia bieżącą stronę; przyrost trafia do dziennika czytania. Zwraca id wpisu (do cofnięcia)."""
        b = self.book(bid)
        if not b:
            return None
        page = max(0, page)
        self.db.execute("UPDATE books SET page = ? WHERE id = ?", (page, bid))
        if page != b.page:  # cofnięcie strony (poprawka) odejmuje
            return self.db.execute("INSERT INTO reading (at, book_id, pages) VALUES (?, ?, ?)",
                                   (time.time(), bid, page - b.page)).lastrowid
        return None

    def delete_reading(self, rid: int) -> None:
        self.db.execute("DELETE FROM reading WHERE id = ?", (rid,))

    def pages_read(self, since: float, until: float | None = None) -> int:
        row = self.db.execute("SELECT COALESCE(SUM(pages), 0) AS n FROM reading WHERE at >= ? AND at < ?",
                              (since, until or time.time() + 1)).fetchone()
        return row["n"]

    def pages_by_day(self, since: float, tz) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.db.execute("SELECT at, pages FROM reading WHERE at >= ?", (since,)):
            d = datetime.fromtimestamp(r["at"], tz).date().isoformat()
            out[d] = out.get(d, 0) + r["pages"]
        return out

    def delete_book(self, bid: int) -> None:
        self.db.execute("DELETE FROM books WHERE id = ?", (bid,))  # dziennik czytania zostaje (statystyki)

    # ---------- dziennik zdarzeń ----------

    def log_event(self, kind: str, **f) -> None:
        try:
            self.db.execute(
                "INSERT INTO events (at, trace, kind, agent, name, input, output, model, tokens_in, tokens_out, cost_usd, ms)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    time.time(), f.get("trace"), kind, f.get("agent"), f.get("name"),
                    _clip(f.get("input")), _clip(f.get("output")), f.get("model"),
                    f.get("tokens_in"), f.get("tokens_out"), f.get("cost_usd"), f.get("ms"),
                ),
            )
        except sqlite3.Error as e:  # dziennik nigdy nie może zepsuć odpowiedzi bota
            log.warning("Dziennik zdarzeń: %r", e)

    def tag_trace(self, trace: str, agent: str | None) -> None:
        if agent:
            self.db.execute("UPDATE events SET agent = ? WHERE trace = ? AND agent IS NULL", (agent, trace))

    def recent_events(self, limit: int = 15) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def events(self, limit: int = 50, before_id: int | None = None, kind: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM events WHERE 1=1", []
        if before_id:
            sql, args = sql + " AND id < ?", [*args, before_id]
        if kind:
            sql, args = sql + " AND kind = ?", [*args, kind]
        rows = self.db.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [dict(r) for r in rows]

    def daily_stats(self, since: float, tz) -> dict[str, dict]:
        """Dzień (YYYY-MM-DD, czas lokalny) → liczba wiadomości i przycisków, wywołań modelu, koszt."""
        out: dict[str, dict] = {}
        for r in self.db.execute("SELECT at, kind, cost_usd FROM events WHERE at >= ?", (since,)):
            day = datetime.fromtimestamp(r["at"], tz).date().isoformat()
            d = out.setdefault(day, {"actions": 0, "llm": 0, "cost_usd": 0.0})
            if r["kind"] == "llm":
                d["llm"] += 1
                d["cost_usd"] += r["cost_usd"] or 0
            elif r["kind"] in ("message", "button", "command"):
                d["actions"] += 1
        return out

    def cost_since(self, since: float) -> tuple[float, int]:
        row = self.db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS c, COUNT(*) AS n FROM events WHERE kind = 'llm' AND at >= ?", (since,)
        ).fetchone()
        return row["c"], row["n"]


def month_start(tz) -> float:
    now = datetime.now(tz)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def day_start(tz, days_ago: int = 0) -> float:
    now = datetime.now(tz) - timedelta(days=days_ago)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _clip(v, n: int = 500):
    if v is None:
        return None
    s = str(v)
    return s if len(s) <= n else s[: n - 1] + "…"
