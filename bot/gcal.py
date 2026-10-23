"""Kalendarz Google: zadania z terminem jako wydarzenia w osobnym kalendarzu „Zadania”
i odczyt Twoich wydarzeń (plan dnia, kolizje).

Autoryzacja (jednorazowo, jak Strava): /google → przycisk → zgoda w Google → przeglądarka przechodzi
na http://localhost/?code=… (strona się nie załaduje — to normalne) → wklejasz ten adres:
/google <adres>. Tokeny i ID kalendarza „Zadania” w data/google.json (wolumen).
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import httpx

if TYPE_CHECKING:
    from .core import Core
    from .store import Task

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/calendar/v3"
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
REDIRECT = "http://localhost"
SCOPE = "https://www.googleapis.com/auth/calendar"
BOT_DESCRIPTION = "Zadanie z bota (Telegram /zadania, panel /panel/)."
CALENDAR_NAME = "Zadania"
TASK_MINUTES = 30  # zadanie z godziną zajmuje w kalendarzu pół godziny
EVENTS_TTL = 300


class GoogleError(Exception):
    pass


class GoogleCalendar:
    def __init__(self, core: "Core"):
        self.core = core
        self.client_id = os.getenv("GOOGLE_CLIENT_ID", "")
        self.client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
        self.path = Path(core.cfg.state_file).with_name("google.json")
        self._http = httpx.AsyncClient(timeout=30)
        self._lock = asyncio.Lock()  # jedna synchronizacja naraz (tworzenie kalendarza, kolejność zmian)
        self._events_cache: dict[str, tuple[float, list[dict]]] = {}  # "start:dni" → (kiedy, wydarzenia)

    # ---------- tokeny ----------

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def state(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}
        except (OSError, ValueError):
            return {}

    @property
    def connected(self) -> bool:
        return self.configured and bool(self.state.get("refresh_token"))

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, self.path)

    def auth_url(self) -> str:
        return f"{AUTH}?" + urlencode({
            "client_id": self.client_id, "redirect_uri": REDIRECT, "response_type": "code",
            "scope": SCOPE, "access_type": "offline", "prompt": "consent",
        })

    async def exchange(self, text: str) -> None:
        """Kod (albo cały adres z kodem) → tokeny."""
        text = text.strip()
        code = (parse_qs(urlparse(text).query).get("code") or [None])[0] if "code=" in text else None
        if not code and re.fullmatch(r"[\w\-/%.]{20,}", text):
            code = unquote(text)
        if not code:
            raise GoogleError("Nie widzę kodu — wklej cały adres z paska przeglądarki (zawiera „code=…”).")
        r = await self._http.post(TOKEN, data={
            "client_id": self.client_id, "client_secret": self.client_secret, "code": code,
            "grant_type": "authorization_code", "redirect_uri": REDIRECT,
        })
        if r.status_code != 200:
            raise GoogleError(f"Google odrzucił kod ({r.status_code}). Kod jest jednorazowy — wygeneruj nowy przez /google.")
        d = r.json()
        if not d.get("refresh_token"):
            raise GoogleError("Google nie dał stałego dostępu (brak refresh_token). Spróbuj jeszcze raz przez /google.")
        if SCOPE not in (d.get("scope") or SCOPE):
            raise GoogleError("Brak zgody na kalendarz — przy autoryzacji zaznacz dostęp do Kalendarza Google.")
        st = self.state
        st.update(refresh_token=d["refresh_token"], access_token=d["access_token"],
                  expires_at=time.time() + d.get("expires_in", 3600))
        self._save(st)
        self._events_cache.clear()

    async def _token(self) -> str:
        st = self.state
        if not st.get("refresh_token"):
            raise GoogleError("Kalendarz Google niepołączony — użyj /google.")
        if st.get("expires_at", 0) - 120 > time.time():
            return st["access_token"]
        r = await self._http.post(TOKEN, data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "grant_type": "refresh_token", "refresh_token": st["refresh_token"],
        })
        if r.status_code != 200:
            raise GoogleError(f"Nie udało się odświeżyć dostępu do Google ({r.status_code}) — połącz ponownie przez /google.")
        d = r.json()
        st.update(access_token=d["access_token"], expires_at=time.time() + d.get("expires_in", 3600))
        self._save(st)
        return st["access_token"]

    async def _req(self, method: str, path: str, **kw) -> httpx.Response:
        token = await self._token()
        r = await self._http.request(method, f"{API}{path}", headers={"Authorization": f"Bearer {token}"}, **kw)
        if r.status_code == 401:
            raise GoogleError("Google odmówił dostępu — połącz ponownie przez /google.")
        return r

    # ---------- kalendarz „Zadania” ----------

    async def _calendar_id(self, recreate: bool = False) -> str:
        st = self.state
        if st.get("calendar_id") and not recreate:
            return st["calendar_id"]
        r = await self._req("POST", "/calendars", json={"summary": CALENDAR_NAME, "timeZone": str(self.core.cfg.tz)})
        r.raise_for_status()
        st = self.state
        st["calendar_id"] = r.json()["id"]
        self._save(st)
        log.info("Kalendarz Google: utworzono kalendarz %s", CALENDAR_NAME)
        return st["calendar_id"]

    def _event_body(self, t: "Task") -> dict:
        tz = str(self.core.cfg.tz)
        d = t.due_date
        if t.due_time:
            start = datetime.combine(d, datetime.strptime(t.due_time, "%H:%M").time())
            end = start + timedelta(minutes=TASK_MINUTES)
            when = {"start": {"dateTime": start.isoformat(), "timeZone": tz},
                    "end": {"dateTime": end.isoformat(), "timeZone": tz}}
        else:
            when = {"start": {"date": d.isoformat()}, "end": {"date": (d + timedelta(days=1)).isoformat()}}
        done = t.status == "done"
        body = {
            "summary": f"✅ {t.title}" if done else t.title,
            "description": BOT_DESCRIPTION,
            "transparency": "transparent" if not t.due_time or done else "opaque",
            **when,
        }
        if done:
            body["colorId"] = "8"  # szary dla zrobionych
        return body

    async def sync_task(self, t: "Task") -> str | None:
        """Tworzy, zmienia albo usuwa wydarzenie zadania. Zwraca aktualne ID wydarzenia (None = brak)."""
        async with self._lock:
            cal = await self._calendar_id()
            if not t.due_date:  # zadanie bez terminu nie ma miejsca w kalendarzu
                if t.gcal_id:
                    await self._delete(cal, t.gcal_id)
                return None
            body = self._event_body(t)
            if t.gcal_id:
                r = await self._req("PUT", f"/calendars/{cal}/events/{t.gcal_id}", json=body)
                if r.status_code not in (404, 410):
                    r.raise_for_status()
                    return t.gcal_id
            r = await self._req("POST", f"/calendars/{cal}/events", json=body)
            if r.status_code == 404:  # kalendarz „Zadania” usunięty ręcznie — zakładamy nowy
                cal = await self._calendar_id(recreate=True)
                r = await self._req("POST", f"/calendars/{cal}/events", json=body)
            r.raise_for_status()
            return r.json()["id"]

    async def delete_event(self, event_id: str) -> None:
        async with self._lock:
            await self._delete(await self._calendar_id(), event_id)

    async def _delete(self, cal: str, event_id: str) -> None:
        r = await self._req("DELETE", f"/calendars/{cal}/events/{event_id}")
        if r.status_code not in (200, 204, 404, 410):
            r.raise_for_status()

    async def task_changes(self) -> list[dict]:
        """Wydarzenia kalendarza „Zadania” zmienione od ostatniego sprawdzenia (pierwszy raz: wszystkie,
        razem z usuniętymi): [{id, deleted, title, due, due_time, done, recurring, from_bot}]."""
        st = self.state
        cal = st.get("calendar_id")
        if not cal:
            return []
        since = st.get("pulled_at")
        now = datetime.now(tz=self.core.cfg.tz).replace(microsecond=0)
        out: list[dict] = []
        params = {"showDeleted": "true", "singleEvents": "true", "maxResults": 250}
        if since:
            params["updatedMin"] = since
        while True:
            r = await self._req("GET", f"/calendars/{cal}/events", params=params)
            if r.status_code == 410:  # za stare updatedMin — zaczynamy od teraz
                break
            r.raise_for_status()
            d = r.json()
            out += [_task_change(e, self.core.cfg.tz) for e in d.get("items", [])]
            if not (token := d.get("nextPageToken")):
                break
            params["pageToken"] = token
        st = self.state
        st["pulled_at"] = (now - timedelta(minutes=2)).isoformat()  # zapas na różnicę zegarów
        self._save(st)
        return out

    # ---------- Twoje wydarzenia ----------

    async def events(self, start: date, days: int = 1) -> list[dict]:
        """Wydarzenia ze wszystkich widocznych kalendarzy (bez „Zadania”), od `start` przez `days` dni."""
        key = f"{start}:{days}"
        if (hit := self._events_cache.get(key)) and time.time() - hit[0] < EVENTS_TTL:
            return hit[1]
        tz = self.core.cfg.tz
        own = self.state.get("calendar_id")
        r = await self._req("GET", "/users/me/calendarList", params={"minAccessRole": "reader"})
        r.raise_for_status()
        calendars = [c for c in r.json().get("items", []) if c.get("selected") and c["id"] != own]
        t0 = datetime.combine(start, datetime.min.time(), tzinfo=tz)
        params = {"timeMin": t0.isoformat(), "timeMax": (t0 + timedelta(days=days)).isoformat(),
                  "singleEvents": "true", "orderBy": "startTime", "maxResults": 100}
        out = []
        for c in calendars[:15]:
            r = await self._req("GET", f"/calendars/{c['id']}/events", params=params)
            if r.status_code != 200:
                continue
            for e in r.json().get("items", []):
                if e.get("status") == "cancelled":
                    continue
                s, en = e.get("start", {}), e.get("end", {})
                all_day = "date" in s
                out.append({
                    "title": e.get("summary") or "(bez tytułu)",
                    "calendar": c.get("summaryOverride") or c.get("summary", ""),
                    "all_day": all_day,
                    "start": s.get("date") if all_day else _local(s.get("dateTime"), tz),
                    "end": en.get("date") if all_day else _local(en.get("dateTime"), tz),
                })
        out.sort(key=lambda e: (e["start"][:10], not e["all_day"], e["start"]))
        self._events_cache = {k: v for k, v in self._events_cache.items() if time.time() - v[0] < EVENTS_TTL}
        self._events_cache[key] = (time.time(), out)
        return out

    def forget_events(self) -> None:
        self._events_cache.clear()

    async def close(self) -> None:
        await self._http.aclose()


def _local(value: str | None, tz) -> str:
    """'2026-10-19T15:00:00+02:00' → '2026-10-19T15:00' w strefie bota."""
    if not value:
        return ""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(tz).strftime("%Y-%m-%dT%H:%M")


def _task_change(e: dict, tz) -> dict:
    s = e.get("start", {})
    if "dateTime" in s:
        local = _local(s["dateTime"], tz)
        due, due_time = local[:10], local[11:16]
    else:
        due, due_time = s.get("date"), None
    summary = (e.get("summary") or "").strip()
    return {"id": e["id"], "deleted": e.get("status") == "cancelled", "title": summary.removeprefix("✅").strip(),
            "due": due, "due_time": due_time, "done": summary.startswith("✅"),
            "recurring": bool(e.get("recurringEventId") or e.get("recurrence")),
            "from_bot": (e.get("description") or "").startswith(BOT_DESCRIPTION)}


def conflicts(events: list[dict], day: date, hhmm: str, minutes: int = TASK_MINUTES) -> list[dict]:
    """Wydarzenia (z godziną) nachodzące na [day hhmm, +minutes)."""
    start = f"{day.isoformat()}T{hhmm}"
    end = (datetime.fromisoformat(start) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M")
    return [e for e in events if not e["all_day"] and e["start"] < end and e["end"] > start]


def event_line(e: dict) -> str:
    when = "cały dzień" if e["all_day"] else f"{e['start'][11:16]}–{e['end'][11:16]}"
    return f"📅 {when} {e['title']}"
