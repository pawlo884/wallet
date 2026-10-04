"""Strava: aktywności do oceny opłacalności karty Multisport.

Autoryzacja (jednorazowo): /strava → link → zgoda w Stravie → przeglądarka przechodzi na
http://localhost/exchange_token?...&code=XYZ (strona się nie załaduje — to normalne) →
wklejasz ten adres albo sam kod: /strava <adres lub kod>. Tokeny w data/strava.json (wolumen).
"""

import calendar
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import httpx

if TYPE_CHECKING:
    from .core import Core

log = logging.getLogger(__name__)

API = "https://www.strava.com/api/v3"
OAUTH = "https://www.strava.com/oauth"
REDIRECT = "http://localhost/exchange_token"
CACHE_TTL = 3600


class StravaError(Exception):
    pass


class Strava:
    def __init__(self, core: "Core"):
        self.client_id = os.getenv("STRAVA_CLIENT_ID", "")
        self.client_secret = os.getenv("STRAVA_CLIENT_SECRET", "")
        self.path = Path(core.cfg.state_file).with_name("strava.json")
        self._http = httpx.AsyncClient(timeout=30)
        self._cache: tuple[float, int, list[dict]] | None = None  # (kiedy, after, aktywności)

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def tokens(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}

    @property
    def connected(self) -> bool:
        return bool(self.tokens.get("refresh_token"))

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, self.path)

    def auth_url(self) -> str:
        return f"{OAUTH}/authorize?" + urlencode({
            "client_id": self.client_id, "response_type": "code", "redirect_uri": REDIRECT,
            "approval_prompt": "force", "scope": "activity:read_all",
        })

    async def exchange(self, text: str) -> str:
        """Kod (albo cały adres z kodem) → tokeny. Zwraca imię sportowca."""
        m = re.search(r"code=([0-9a-f]+)", text) or re.fullmatch(r"\s*([0-9a-f]{20,})\s*", text)
        if not m:
            raise StravaError("Nie widzę kodu — wklej cały adres z paska przeglądarki (zawiera „code=…”).")
        if "activity:read" not in text and "scope=" in text:
            raise StravaError("Brak zgody na odczyt aktywności — przy autoryzacji zostaw zaznaczone „View data about your activities”.")
        r = await self._http.post(f"{OAUTH}/token", data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "code": m.group(1), "grant_type": "authorization_code",
        })
        if r.status_code != 200:
            raise StravaError(f"Strava odrzuciła kod ({r.status_code}). Kod jest jednorazowy — wygeneruj nowy przez /strava.")
        d = r.json()
        athlete = d.get("athlete") or {}
        self._save({"refresh_token": d["refresh_token"], "access_token": d["access_token"],
                    "expires_at": d["expires_at"], "athlete": f"{athlete.get('firstname', '')} {athlete.get('lastname', '')}".strip()})
        self._cache = None
        return self.tokens["athlete"]

    async def _access_token(self) -> str:
        t = self.tokens
        if not t.get("refresh_token"):
            raise StravaError("Strava niepołączona — użyj /strava.")
        if t.get("expires_at", 0) - 120 > time.time():
            return t["access_token"]
        r = await self._http.post(f"{OAUTH}/token", data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "grant_type": "refresh_token", "refresh_token": t["refresh_token"],
        })
        if r.status_code != 200:
            raise StravaError(f"Nie udało się odświeżyć dostępu do Stravy ({r.status_code}) — połącz ponownie przez /strava.")
        d = r.json()
        t.update(access_token=d["access_token"], refresh_token=d["refresh_token"], expires_at=d["expires_at"])
        self._save(t)
        return t["access_token"]

    async def activities(self, after_ts: int) -> list[dict]:
        if self._cache and self._cache[1] <= after_ts and time.time() - self._cache[0] < CACHE_TTL:
            return [a for a in self._cache[2] if a["ts"] >= after_ts]
        token = await self._access_token()
        out, page = [], 1
        while True:
            r = await self._http.get(f"{API}/athlete/activities", headers={"Authorization": f"Bearer {token}"},
                                     params={"after": after_ts, "per_page": 200, "page": page})
            if r.status_code == 429:
                raise StravaError("Limit zapytań Stravy — spróbuj za kwadrans.")
            r.raise_for_status()
            batch = r.json()
            for a in batch:
                out.append({
                    "id": a["id"], "name": a.get("name", ""), "type": a.get("sport_type") or a.get("type", ""),
                    "date": a["start_date_local"][:10], "ts": calendar.timegm(time.strptime(a["start_date"][:19], "%Y-%m-%dT%H:%M:%S")),
                    "gps": bool(a.get("start_latlng")), "minutes": round((a.get("moving_time") or 0) / 60),
                })
            if len(batch) < 200:
                break
            page += 1
        self._cache = (time.time(), after_ts, out)
        return out

    async def close(self) -> None:
        await self._http.aclose()
