"""JSON API panelu (React, /panel): zadania, książki, dziennik zdarzeń, podsumowanie.

Jak strona prognozy — bez własnego logowania, dostęp ustawia lista dostępu w Nginx Proxy Managerze.
Zapisy przyjmują tylko application/json: przeglądarka nie wyśle takiego żądania z obcej strony
bez zgody CORS (której nie dajemy), więc obcy formularz nie zmieni listy.
"""

import json
import logging
from dataclasses import asdict
from datetime import date, timedelta
from typing import TYPE_CHECKING

from aiohttp import web

from .lists import fmt_due
from .store import BOOK_STATUSES, day_start, month_start

if TYPE_CHECKING:
    from .core import Core

log = logging.getLogger(__name__)
PREFIX = "/api/v1"


def _json(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str),
                             headers={"Cache-Control": "no-store"})


def _err(msg: str, status: int = 400) -> web.Response:
    return _json({"error": msg}, status)


async def _body(request: web.Request) -> dict:
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(text="Wymagany Content-Type: application/json")
    try:
        data = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="Niepoprawny JSON")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="Oczekiwany obiekt JSON")
    return data


def _due(value) -> str | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise web.HTTPBadRequest(text="Termin w formacie YYYY-MM-DD")


def routes(core: "Core") -> list[web.RouteDef]:
    store = core.store

    def task_json(t) -> dict:
        d = asdict(t)
        d["due_label"] = fmt_due(t.due_date, core.today())
        d["overdue"] = bool(t.due_date and t.due_date < core.today() and t.status == "open")
        return d

    def logged(name: str, detail) -> None:
        store.log_event("panel", agent="panel", name=name, input=json.dumps(detail, ensure_ascii=False))

    # ---------- podsumowanie ----------

    async def overview(_: web.Request) -> web.Response:
        tz, today = core.cfg.tz, core.today()
        tasks = store.open_tasks()
        books = store.books()
        cost_month, calls = store.cost_since(month_start(tz))
        cost_today, _ = store.cost_since(day_start(tz))
        try:
            month = await core.month_data()
        except Exception as e:  # Wallet chwilowo niedostępny — reszta panelu działa
            log.warning("Panel: podsumowanie miesiąca: %r", e)
            month = None
        return _json({
            "today": today.isoformat(),
            "tasks": {
                "open": len(tasks),
                "overdue": sum(1 for t in tasks if t.due_date and t.due_date < today),
                "today": sum(1 for t in tasks if t.due_date == today),
                "done_today": len(store.done_tasks_since(day_start(tz))),
            },
            "books": {s: sum(1 for b in books if b.status == s) for s in BOOK_STATUSES},
            "cost": {"today_usd": cost_today, "month_usd": cost_month, "month_calls": calls,
                     "budget_usd": core.cfg.llm_budget_usd or None},
            "month": month,
            "forecast_url": core.cfg.forecast_url or None,
        })

    # ---------- zadania ----------

    async def tasks_list(_: web.Request) -> web.Response:
        week_ago = day_start(core.cfg.tz, 7)
        return _json({
            "open": [task_json(t) for t in store.open_tasks()],
            "done": [task_json(t) for t in reversed(store.done_tasks_since(week_ago))],
        })

    async def task_create(request: web.Request) -> web.Response:
        data = await _body(request)
        title = str(data.get("title") or "").strip()
        if not title:
            return _err("Podaj tytuł zadania")
        t = store.add_task(title, _due(data.get("due")))
        logged("task_add", {"id": t.id, "title": t.title})
        return _json(task_json(t), 201)

    async def task_update(request: web.Request) -> web.Response:
        t = store.task(int(request.match_info["id"]))
        if not t:
            return _err("Nie ma takiego zadania", 404)
        data = await _body(request)
        if "title" in data and str(data["title"]).strip():
            store.set_task(t.id, title=str(data["title"]).strip()[:200])
        if "due" in data:
            due = _due(data["due"])
            if due != t.due:
                store.postpone_task(t.id, due)
        if data.get("status") == "done" and t.status != "done":
            store.finish_task(t.id)
        elif data.get("status") == "open" and t.status != "open":
            store.set_task(t.id, status="open", done_at=None)
        logged("task_update", {"id": t.id, **data})
        return _json(task_json(store.task(t.id)))

    async def task_delete(request: web.Request) -> web.Response:
        t = store.task(int(request.match_info["id"]))
        if not t:
            return _err("Nie ma takiego zadania", 404)
        store.delete_task(t.id)
        logged("task_delete", {"id": t.id, "title": t.title})
        return _json({"ok": True})

    # ---------- książki ----------

    async def books_list(_: web.Request) -> web.Response:
        return _json({"books": [asdict(b) for b in store.books()], "statuses": BOOK_STATUSES})

    async def book_create(request: web.Request) -> web.Response:
        data = await _body(request)
        title = str(data.get("title") or "").strip()
        status = data.get("status") or "to_buy"
        if not title or status not in BOOK_STATUSES:
            return _err("Podaj tytuł i poprawny status")
        b, created = store.add_book(title, (data.get("author") or None), status)
        logged("book_add", {"id": b.id, "title": b.title, "created": created})
        return _json({**asdict(b), "created": created}, 201 if created else 200)

    async def book_update(request: web.Request) -> web.Response:
        b = store.book(int(request.match_info["id"]))
        if not b:
            return _err("Nie ma takiej książki", 404)
        data = await _body(request)
        if (status := data.get("status")) and status != b.status:
            if status not in BOOK_STATUSES:
                return _err("Niepoprawny status")
            store.set_book_status(b.id, status)
        logged("book_update", {"id": b.id, **data})
        return _json(asdict(store.book(b.id)))

    async def book_delete(request: web.Request) -> web.Response:
        b = store.book(int(request.match_info["id"]))
        if not b:
            return _err("Nie ma takiej książki", 404)
        store.delete_book(b.id)
        logged("book_delete", {"id": b.id, "title": b.title})
        return _json({"ok": True})

    # ---------- dziennik ----------

    async def events_list(request: web.Request) -> web.Response:
        q = request.query
        limit = min(int(q.get("limit", 50)), 200)
        before = int(q["before"]) if q.get("before", "").isdigit() else None
        return _json({"events": store.events(limit, before, q.get("kind") or None)})

    async def daily(request: web.Request) -> web.Response:
        days = min(int(request.query.get("days", 30)), 400)
        stats = store.daily_stats(day_start(core.cfg.tz, days - 1), core.cfg.tz)
        today = core.today()
        series = []
        for i in range(days - 1, -1, -1):
            d = (today - timedelta(days=i)).isoformat()
            series.append({"date": d, **stats.get(d, {"actions": 0, "llm": 0, "cost_usd": 0.0})})
        return _json({"days": series})

    p = PREFIX
    return [
        web.get(f"{p}/overview", overview),
        web.get(f"{p}/tasks", tasks_list),
        web.post(f"{p}/tasks", task_create),
        web.patch(fr"{p}/tasks/{{id:\d+}}", task_update),
        web.delete(fr"{p}/tasks/{{id:\d+}}", task_delete),
        web.get(f"{p}/books", books_list),
        web.post(f"{p}/books", book_create),
        web.patch(fr"{p}/books/{{id:\d+}}", book_update),
        web.delete(fr"{p}/books/{{id:\d+}}", book_delete),
        web.get(f"{p}/events", events_list),
        web.get(f"{p}/stats/daily", daily),
    ]
