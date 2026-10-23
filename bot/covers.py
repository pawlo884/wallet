"""Okładki książek do panelu (i przy okazji liczba stron): najpierw Google Books, potem Open Library.

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


async def find_cover(http: httpx.AsyncClient, title: str, author: str | None) -> tuple[str, int | None]:
    """(adres okładki albo "", liczba stron albo None). Błąd sieci → wyjątek (spróbujemy później)."""
    url, pages = "", None
    for source in (_google, _openlibrary):
        try:
            u, p = await source(http, title, author)
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 429:  # limit zapytań — próbujemy drugiego źródła
                raise
            log.info("Okładki: limit w %s", source.__name__)
            continue
        url, pages = url or u, pages or p
        if url and pages:
            break
    return url, pages


async def _google(http: httpx.AsyncClient, title: str, author: str | None) -> tuple[str, int | None]:
    q = f"intitle:{title}" + (f" inauthor:{author.split(',')[0]}" if author else "")
    r = await http.get(GOOGLE, params={"q": q, "maxResults": 5, "printType": "books",
                                       "fields": "items(volumeInfo(title,imageLinks,pageCount))"})
    r.raise_for_status()
    url, pages = "", None
    for item in r.json().get("items", []):
        info = item.get("volumeInfo", {})
        pages = pages or (info.get("pageCount") or None)
        links = info.get("imageLinks") or {}
        if not url and (u := links.get("thumbnail") or links.get("smallThumbnail")):
            # http → https (panel jest po https) i bez „zawiniętego rogu”
            url = re.sub(r"&edge=curl", "", u.replace("http://", "https://", 1))
        if url and pages:
            break
    return url, pages


async def _openlibrary(http: httpx.AsyncClient, title: str, author: str | None) -> tuple[str, int | None]:
    params = {"title": title, "limit": 5, "fields": "cover_i,number_of_pages_median"}
    if author:
        params["author"] = author.split(",")[0]
    r = await http.get(OPENLIBRARY, params=params)
    r.raise_for_status()
    url, pages = "", None
    for doc in r.json().get("docs", []):
        pages = pages or (doc.get("number_of_pages_median") or None)
        if not url and doc.get("cover_i"):
            url = OL_COVER.format(quote(str(doc["cover_i"])))
        if url and pages:
            break
    return url, pages
