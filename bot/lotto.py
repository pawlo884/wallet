"""Eurojackpot: ile poszło na kupony, ile wróciło w wygranych, zwrot i szanse.

Użytkownik gra tylko w Eurojackpot, więc każdy wpis z kategorii „Lottery, gambling” (i jej podkategorii)
to Eurojackpot — także „wpłata na lotto” (saldo lotto.pl). Wydatek = kupon / wpłata, przychód = wygrana.
Liczone tylko konta aktywne (bez wyłączonych ze statystyk, np. archiwum).
Szanse liczone dokładnie z zasad gry (kombinatoryka), nie z danych.
"""

from datetime import date, timedelta
from math import comb
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .core import Core

LOTTERY = "5c5c177c-003c-8000-8000-000000000000"  # Lottery, gambling (Life & Entertainment)
PRICE = 12.5  # zł za zakład — tylko do szacunku „ile zakładów i lat do głównej wygranej”


def _odds() -> tuple[float, float]:
    """(szansa na I stopień, szansa na jakąkolwiek wygraną) — 5 z 50 + 2 z 12, 12 stopni wygranych."""
    tiers = [(5, 2), (5, 1), (5, 0), (4, 2), (4, 1), (3, 2), (4, 0), (2, 2), (3, 1), (3, 0), (1, 2), (2, 1)]
    main, euro = comb(50, 5), comb(12, 2)
    p = lambda k, e: comb(5, k) * comb(45, 5 - k) / main * comb(2, e) * comb(10, 2 - e) / euro  # noqa: E731
    return p(5, 2), sum(p(k, e) for k, e in tiers)


ODDS = _odds()


async def analyze(core: "Core") -> dict:
    await core.refresh_catalog()
    cats = [LOTTERY] + [c["id"] for c in core._categories.values() if c.get("parentId") == LOTTERY]
    # Tylko aktywne konta (jak /miesiac) — archiwum ze starą historią nie wchodzi do bilansu.
    counted = [a for a, acc in core._accounts.items() if not acc.get("excludeFromStats")][:10]
    today = core.today()
    records = await core.wallet.records(
        "2000-01-01", (today + timedelta(days=1)).isoformat(), categoryId=",".join(cats), accountId=",".join(counted)
    ) if counted else []
    spent = won = 0.0
    best, first = None, None
    for r in records:
        value = (r.get("amount") or {}).get("value", 0)
        if value < 0:
            spent += -value
        elif value > 0:
            won += value
            if not best or value > best[0]:
                best = (value, r.get("recordDate", "")[:10])
        d = (r.get("recordDate") or "")[:10]
        if d and (not first or d < first):
            first = d
    months = max(1.0, ((today - date.fromisoformat(first)).days + 1) / 30.44) if first else None
    return {"spent": spent, "won": won, "best": best, "first": first, "months": months, "count": len(records)}


def _pct(x: float) -> str:
    return f"{x:.0%}"


def _ratio(p: float) -> str:
    return f"1 : {round(1 / p):,}".replace(",", " ")


def text(data: dict) -> str:
    from .core import fmt_money

    if not data["count"]:
        return ("🎰 Nie widzę jeszcze wpisów z Eurojackpot.\n"
                "Zapisuj je normalnie, np. „eurojackpot 25” albo „wygrana eurojackpot 24”, a tu policzę bilans.")
    spent, won = data["spent"], data["won"]
    lines = [f"🎰 *Eurojackpot* (od {date.fromisoformat(data['first']):%d.%m.%Y})",
             f"Wydane: {fmt_money(-spent, 'PLN')}",
             f"Wygrane: {fmt_money(won, 'PLN')}",
             f"Bilans: *{fmt_money(won - spent, 'PLN')}*"]
    if spent:
        lines.append(f"Zwrot: {_pct(won / spent)} (z każdych 100 zł wróciło {won / spent * 100:.0f} zł)")
    if data["months"]:
        lines.append(f"Średnio na kupony: {fmt_money(-spent / data['months'], 'PLN')} miesięcznie")
    if data["best"]:
        v, d = data["best"]
        lines.append(f"Największa wygrana: {fmt_money(v, 'PLN')} ({date.fromisoformat(d):%d.%m.%Y})")

    top, anyp = ODDS
    lines.append(f"\n*Szanse na jeden zakład:* główna wygrana {_ratio(top)}, jakakolwiek wygrana {_ratio(anyp)}")
    if spent and data["months"]:
        per_year = spent / data["months"] * 12 / PRICE
        years = 1 / top / per_year
        lines.append(f"Przy Twoim tempie (~{per_year:.0f} zakładów po {PRICE:.2f} zł rocznie) "
                     f"główna wygrana średnio raz na {years:,.0f} lat".replace(",", " ").replace(".", ","))
    lines.append("\n_Do puli wygranych trafia mniej więcej połowa stawek, więc na dłuższą metę "
                 "wraca średnio ok. 50 zł ze 100 zł. Gra to rozrywka, nie inwestycja._")
    return "\n".join(lines)
