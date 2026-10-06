"""Stan rozmowy na wolumenie: szkice, „Cofnij”, korekty, czekanie na kwotę (✏️).

Każdy deploy restartuje kontener, więc trzymane tylko w RAM przyciski przestawały działać.
Słowniki z tego modułu zachowują się jak zwykłe dict, ale po każdej zmianie zapisują się
do data/session.json. Wpisy starsze niż TTL danego słownika są przy wczytaniu i zapisie pomijane.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)


def _same(v: Any) -> Any:
    return v


class PersistentDict(dict):
    def __init__(self, session: "Session", ttl: float, encode: Callable, decode: Callable):
        super().__init__()
        self._session = session
        self.ttl = ttl
        self.encode = encode
        self.decode = decode
        self.added: dict[str, float] = {}  # klucz → kiedy dodany (do TTL)

    def __setitem__(self, key, value) -> None:
        super().__setitem__(key, value)
        self.added[key] = time.time()
        self._session.save()

    def __delitem__(self, key) -> None:
        super().__delitem__(key)
        self.added.pop(key, None)
        self._session.save()

    def pop(self, key, *default):
        if key not in self:
            return super().pop(key, *default)
        value = super().pop(key)
        self.added.pop(key, None)
        self._session.save()
        return value

    def prune(self) -> None:
        """Usuwa wygasłe wpisy (bez zapisu — zapisze najbliższa zmiana)."""
        cutoff = time.time() - self.ttl
        for key in [k for k, t in self.added.items() if t < cutoff]:
            dict.pop(self, key, None)
            del self.added[key]


class Session:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._dicts: dict[str, PersistentDict] = {}
        self._raw: dict = {}
        if self.path.exists():
            try:
                self._raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:  # uszkodzony plik nie może zablokować startu bota
                log.warning("Pomijam %s: %r", self.path, e)

    def dict(self, name: str, ttl: float, encode: Callable = _same, decode: Callable = _same) -> PersistentDict:
        d = PersistentDict(self, ttl, encode, decode)
        cutoff = time.time() - ttl
        for key, item in (self._raw.get(name) or {}).items():
            if item.get("t", 0) < cutoff:
                continue
            try:
                dict.__setitem__(d, key, decode(item["v"]))
            except Exception as e:  # np. zmiana formatu po aktualizacji — tracimy tylko ten przycisk
                log.warning("Sesja %s/%s: %r", name, key, e)
                continue
            d.added[key] = item["t"]
        self._dicts[name] = d
        return d

    def save(self) -> None:
        data = {}
        for name, d in self._dicts.items():
            d.prune()
            data[name] = {k: {"t": d.added[k], "v": d.encode(v)} for k, v in d.items()}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:  # brak miejsca / uprawnień — bot działa dalej, tylko bez trwałości
            log.warning("Nie zapisałem sesji: %r", e)
