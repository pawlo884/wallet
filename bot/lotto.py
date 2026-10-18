"""Lotto i Eurojackpot: ile poszło na kupony, ile wróciło w wygranych, zwrot i szanse.

Liczone tylko konta aktywne (bez wyłączonych ze statystyk, np. archiwum). Wpisy to zwykłe rekordy w kategorii „Lottery, gambling” (i jej podkategoriach): wydatek = kupon /
wpłata na lotto.pl, przychód = wygrana. Grę rozpoznajemy po notatce lub odbiorcy („eurojackpot”, „lotto”).
Szanse liczone dokładnie z zasad gry (kombinatoryka), nie z danych.
"""

import re
from collections import defaultdict
from datetime import date, timedelta
from math import comb
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .core import Core

LOTTERY = "5c5c177c-003c-8000-8000-000000000000"  # Lottery, gambling (Life & Entertainment)

# Cena jednego zakładu (zł) — tylko do szacunku „ile zakładów i lat do głównej wygranej”.
PRICE = {"Eurojackpot": 12.5, "Lotto": 3.0}


def _p_eurojackpot() -> tuple[float, float]:
    """(szansa na I stopień, szansa na jakąkolwiek wygraną) — 5 z 50 + 2 z 12, 12 stopni wygranych."""
    tiers = [(5, 2), (5, 1), (5, 0), (4, 2), (4, 1), (3, 2), (4, 0), (2, 2), (3, 1), (3, 0), (1, 2), (2, 1)]
    main, euro = comb(50, 5), comb(12, 2)
    p = lambda k, e: comb(5, k) * comb(45, 5 - k) / main * comb(2, e) * comb(10, 2 - e) / euro  # noqa: E731
    return p(5, 2), sum(p(k, e) for k, e in tiers)


def _p_lotto() -> tuple[float, float]:
    """(szansa na „szóstkę”, szansa na co najmniej „trójkę”) — 6 z 49."""
    total = comb(49, 6)
    return 1 / total, sum(comb(6, k) * comb(43, 6 - k) for k in range(3, 7)) / total


ODDS = {"Eurojackpot": _p_eurojackpot(), "Lotto": _p_lotto()}


def game_of(rec: dict) -> str:
    text = f"{rec.get('note') or ''} {rec.get('counterParty') or ''}".lower()
    if re.search(r"euro\s*-?\s*jack|eurojac|\bej\b", text):
        return "Eurojackpot"
    if re.search(r"lott|lotek|lotto", text):
        return "Lotto"
    return "Inne"


async def analyze(core: "Core") -> dict:
    await core.refresh_catalog()
    cats = [LOTTERY] + [c["id"] for c in core._categories.values() if c.get("parentId") == LOTTERY]
    # Tylko aktywne konta (jak /miesiac) — archiwum ze starą historią nie wchodzi do bilansu.
    counted = [a for a, acc in core._accounts.items() if not acc.get("excludeFromStats")][:10]
    if not counted:
        return {"games": {}, "spent": 0.0, "won": 0.0, "best": None, "first": None, "months": None, "count": 0}
    today = core.today()
    records = await core.wallet.records(
        "2000-01-01", (today + timedelta(days=1)).isoformat(), categoryId=",".join(cats), accountId=",".join(counted)
    )
    games: dict[str, dict] = defaultdict(lambda: {"spent": 0.0, "won": 0.0, "tickets": 0, "wins": 0})
    best, first = None, None
    for r in records:
        value = (r.get("amount") or {}).get("value", 0)
        g = games[game_of(r)]
        if value < 0:
            g["spent"] += -value
            g["tickets"] += 1
        elif value > 0:
            g["won"] += value
            g["wins"] += 1
            if not best or value > best[0]:
                best = (value, r.get("recordDate", "")[:10], game_of(r))
        d = (r.get("recordDate") or "")[:10]
        if d and (not first or d < first):
            first = d
    spent = sum(g["spent"] for g in games.values())
    won = sum(g["won"] for g in games.values())
    months = None
    if first:
        start = date.fromisoformat(first)
        months = max(1.0, ((today - start).days + 1) / 30.44)
    return {"games": dict(games), "spent": spent, "won": won, "best": best, "first": first,
            "months": months, "count": len(records)}


def _pct(x: float) -> str:
    return f"{x:.0%}".replace(".", ",")


def _odds(p: float) -> str:
    return f"1 : {round(1 / p):,}".replace(",", " ")


def text(data: dict) -> str:
    from .core import fmt_money

    if not data["count"]:
        return ("🎰 Nie widzę jeszcze wpisów z Lotto ani Eurojackpot.\n"
                "Zapisuj je normalnie, np. „eurojackpot 25” albo „wygrana lotto 24”, a tu policzę bilans.")
    spent, won = data["spent"], data["won"]
    lines = [f"🎰 *Lotto i Eurojackpot* (od {date.fromisoformat(data['first']):%d.%m.%Y})",
             f"Wydane: {fmt_money(-spent, 'PLN')}",
             f"Wygrane: {fmt_money(won, 'PLN')}",
             f"Bilans: *{fmt_money(won - spent, 'PLN')}*"]
    if spent:
        lines.append(f"Zwrot: {_pct(won / spent)} (z każdych 100 zł wróciło {won / spent * 100:.0f} zł)".replace(".", ","))
    if data["months"]:
        lines.append(f"Średnio na kupony: {fmt_money(-spent / data['months'], 'PLN')} miesięcznie")
    if data["best"]:
        v, d, g = data["best"]
        lines.append(f"Największa wygrana: {fmt_money(v, 'PLN')} ({g}, {date.fromisoformat(d):%d.%m.%Y})")

    games = data["games"]
    if len([g for g in games if games[g]["spent"] or games[g]["won"]]) > 1:
        lines.append("\n*Według gry:*")
        for name in ("Eurojackpot", "Lotto", "Inne"):
            if g := games.get(name):
                ret = f", zwrot {_pct(g['won'] / g['spent'])}" if g["spent"] else ""
                lines.append(f"• {name}: wydane {fmt_money(-g['spent'], 'PLN')}, wygrane {fmt_money(g['won'], 'PLN')}{ret}")

    lines.append("\n*Szanse na jeden zakład:*")
    for name, (top, anyp) in ODDS.items():
        lines.append(f"• {name}: główna wygrana {_odds(top)}, cokolwiek {_odds(anyp)}")
        g = games.get(name)
        if g and g["spent"] and data["months"]:
            per_year = g["spent"] / data["months"] * 12 / PRICE[name]
            years = 1 / top / per_year
            lines.append(f"   przy Twoim tempie (~{per_year:.0f} zakładów po {PRICE[name]:.2f} zł rocznie) "
                         f"główna wygrana średnio raz na {years:,.0f} lat".replace(",", " ").replace(".", ","))
    lines.append("\n_Do puli wygranych trafia mniej więcej połowa stawek, więc na dłuższą metę "
                 "wraca średnio ok. 50 zł ze 100 zł. Gra to rozrywka, nie inwestycja._")
    return "\n".join(lines)
