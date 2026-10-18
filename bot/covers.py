"""Okładki książek do panelu: najpierw Google Books, potem Open Library (oba bez klucza).

Zapisujemy sam adres obrazka (przeglądarka pobiera go wprost ze źródła). cover_url w bazie:
NULL = jeszcze nie szukano, "" = szukano i nie ma.
"""

import logging
import re
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

GOOGLE = "https://www.googleapis.com/books/v1/volumes"
OPENLIBRARY = "https://openlibrary.org/search.json"
OL_COVER = "https://covers.openlibrary.org/b/id/{}-M.jpg"


async def find_cover(http: httpx.AsyncClient, title: str, author: str | None) -> str:
    """Adres okładki albo "" (nie znaleziono). Błąd sieci → wyjątek (spróbujemy później)."""
    for source in (_google, _openlibrary):
        try:
            if url := await source(http, title, author):
                return url
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 429:  # limit zapytań — próbujemy drugiego źródła
                raise
            log.info("Okładki: limit w %s", source.__name__)
    return ""


async def _google(http: httpx.AsyncClient, title: str, author: str | None) -> str:
    q = f"intitle:{title}" + (f" inauthor:{author.split(',')[0]}" if author else "")
    r = await http.get(GOOGLE, params={"q": q, "maxResults": 5, "printType": "books",
                                       "fields": "items(volumeInfo(title,imageLinks))"})
    r.raise_for_status()
    for item in r.json().get("items", []):
        links = item.get("volumeInfo", {}).get("imageLinks") or {}
        if url := links.get("thumbnail") or links.get("smallThumbnail"):
            # http → https (panel jest po https) i bez „zawiniętego rogu”
            return re.sub(r"&edge=curl", "", url.replace("http://", "https://", 1))
    return ""


async def _openlibrary(http: httpx.AsyncClient, title: str, author: str | None) -> str:
    params = {"title": title, "limit": 5, "fields": "cover_i"}
    if author:
        params["author"] = author.split(",")[0]
    r = await http.get(OPENLIBRARY, params=params)
    r.raise_for_status()
    for doc in r.json().get("docs", []):
        if doc.get("cover_i"):
            return OL_COVER.format(quote(str(doc["cover_i"])))
    return ""
