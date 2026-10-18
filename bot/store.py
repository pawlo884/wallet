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
    done_at REAL
);
CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    author TEXT,
    status TEXT NOT NULL,          -- to_buy | to_read | reading | read
    note TEXT,
    key TEXT NOT NULL UNIQUE,      -- tytuł bez wielkości liter i ogonków (żeby nie dublować)
    created_at REAL NOT NULL,
    status_at REAL NOT NULL
);
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

    def label(self) -> str:
        return f"{self.title} — {self.author}" if self.author else self.title


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)  # autocommit
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
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

    def add_task(self, title: str, due: str | None) -> Task:
        cur = self.db.execute(
            "INSERT INTO tasks (title, due, created_at) VALUES (?, ?, ?)", (title.strip()[:200], due, time.time())
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

    def postpone_task(self, tid: int, due: str | None) -> None:
        self.db.execute("UPDATE tasks SET due = ?, moves = moves + 1 WHERE id = ?", (due, tid))

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

    def book(self, bid: int) -> Book | None:
        row = self.db.execute("SELECT * FROM books WHERE id = ?", (bid,)).fetchone()
        return Book(**dict(row)) if row else None

    def book_by_key(self, key: str) -> Book | None:
        row = self.db.execute("SELECT * FROM books WHERE key = ?", (key,)).fetchone()
        return Book(**dict(row)) if row else None

    def books(self) -> list[Book]:
        rows = self.db.execute("SELECT * FROM books ORDER BY status_at DESC, id DESC").fetchall()
        return [Book(**dict(r)) for r in rows]

    def set_book_status(self, bid: int, status: str) -> None:
        self.db.execute("UPDATE books SET status = ?, status_at = ? WHERE id = ?", (status, time.time(), bid))

    def delete_book(self, bid: int) -> None:
        self.db.execute("DELETE FROM books WHERE id = ?", (bid,))

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
