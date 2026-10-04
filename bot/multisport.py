"""Opłacalność karty Multisport: koszt miesięczny vs wejścia ze Stravy.

Koszt bierzemy z płatności cyklicznej (config/schedule.yaml), aktywności ze Stravy, a cenę
pojedynczego wejścia i typy aktywności liczone jako „wejście na kartę” z config/forecast.yaml.
"""

import calendar
import math
from collections import Counter
from datetime import date
from typing import TYPE_CHECKING

from .forecast import MIES, load_assumptions

if TYPE_CHECKING:
    from .core import Core

DEFAULT_TYPES = ["WeightTraining", "Workout", "Crossfit", "HighIntensityIntervalTraining", "Swim", "Yoga",
                 "Pilates", "RockClimbing", "Squash", "Badminton"]
TYPE_PL = {"WeightTraining": "siłownia", "Workout": "trening", "Crossfit": "crossfit",
           "HighIntensityIntervalTraining": "HIIT", "Swim": "basen", "Yoga": "joga", "Pilates": "pilates",
           "RockClimbing": "ścianka", "Squash": "squash", "Badminton": "badminton"}


def _month_add(d: date, k: int) -> date:
    m = d.month - 1 + k
    return date(d.year + m // 12, m % 12 + 1, 1)


async def analyze(core: "Core") -> dict | None:
    cfg = load_assumptions(core.cfg.forecast_file).get("multisport")
    if not cfg:
        return None
    p = core.planned.schedule.get(cfg.get("payment", "multisport"))
    fee = float(cfg.get("fee") or (p.amount if p else 0))
    ticket = float(cfg.get("ticket", 25))
    types = set(cfg.get("sport_types") or DEFAULT_TYPES)
    n = int(cfg.get("months", 6))
    breakeven = math.ceil(round(fee / ticket, 4)) if ticket else None
    base = {"fee": fee, "ticket": ticket, "breakeven": breakeven, "name": p.name if p else "Multisport"}

    strava = core.strava
    if not strava.configured:
        return {**base, "status": "no_app"}
    if not strava.connected:
        return {**base, "status": "not_connected"}

    today = core.today()
    start = _month_add(today.replace(day=1), -(n - 1))
    acts = await strava.activities(calendar.timegm(start.timetuple()))
    # Wejście na kartę: wybrane typy; pływanie z GPS to otwarta woda — pomijamy. Jeden typ = max 1 wejście dziennie.
    visits = {(a["date"], a["type"]) for a in acts if a["type"] in types and not (a["type"] == "Swim" and a["gps"])}

    rows = []
    for k in range(n):
        m0 = _month_add(start, k)
        key = m0.strftime("%Y-%m")
        mv = [t for d, t in visits if d.startswith(key)]
        count = len(mv)
        rows.append({
            "month": key, "label": f"{MIES[m0.month - 1]} {m0.year}", "visits": count,
            "partial": m0.year == today.year and m0.month == today.month,
            "per_visit": round(fee / count, 2) if count else None,
            "tickets": round(count * ticket, 2), "saving": round(count * ticket - fee, 2),
            "by_type": {TYPE_PL.get(t, t): c for t, c in Counter(mv).most_common()},
        })
    full = [r for r in rows if not r["partial"]]
    avg = round(sum(r["visits"] for r in full) / len(full), 1) if full else None
    avg_saving = round(sum(r["saving"] for r in full) / len(full), 2) if full else None
    cur = rows[-1]
    days_left = calendar.monthrange(today.year, today.month)[1] - today.day
    return {
        **base, "status": "ok", "athlete": strava.tokens.get("athlete", ""), "rows": rows,
        "avg_visits": avg, "avg_saving": avg_saving,
        "avg_per_visit": round(fee / avg, 2) if avg else None,
        "worth_it": (avg is not None and breakeven is not None and avg >= breakeven),
        "current": {"visits": cur["visits"], "to_breakeven": max((breakeven or 0) - cur["visits"], 0), "days_left": days_left},
    }


def text(a: dict | None) -> str:
    from .core import fmt_money

    if not a:
        return "Brak sekcji multisport w config/forecast.yaml."
    head = (f"🏋️ *{a['name']}*: {fmt_money(a['fee'], 'PLN')}/mies. · wejście bez karty ~{fmt_money(a['ticket'], 'PLN')}"
            f" → opłaca się od *{a['breakeven']} wejść* w miesiącu")
    if a["status"] == "no_app":
        return head + "\n\nStrava niepodłączona: dodaj STRAVA_CLIENT_ID i STRAVA_CLIENT_SECRET do .env, potem /strava."
    if a["status"] == "not_connected":
        return head + "\n\nPołącz Stravę: /strava"
    lines = [head, ""]
    for r in a["rows"]:
        mark = "⏳" if r["partial"] else ("✅" if r["saving"] >= 0 else "❌")
        per = f"{fmt_money(r['per_visit'], 'PLN')}/wejście" if r["per_visit"] else "—"
        kinds = ", ".join(f"{k} {v}" for k, v in r["by_type"].items())
        lines.append(f"{mark} {r['label']}: *{r['visits']}* wejść · {per}" + (f" ({kinds})" if kinds else ""))
    if a["avg_visits"] is not None:
        verdict = "opłaca się ✅" if a["worth_it"] else "na razie się nie opłaca ❌"
        lines += ["", f"Średnio {str(a['avg_visits']).replace('.', ',')} wejść/mies. → {fmt_money(a['avg_per_visit'], 'PLN')} za wejście — {verdict}",
                  f"Bilans vs bilety: {'+' if a['avg_saving'] >= 0 else ''}{fmt_money(a['avg_saving'], 'PLN')} miesięcznie"]
    c = a["current"]
    if c["to_breakeven"]:
        lines.append(f"W tym miesiącu: {c['visits']} wejść — brakuje {c['to_breakeven']} do progu (zostało {c['days_left']} dni).")
    else:
        lines.append(f"W tym miesiącu: {c['visits']} wejść — próg opłacalności przekroczony 💪")
    lines.append("_Liczone ze Stravy: siłownia, basen (bez GPS), joga, pilates, ścianka itp._")
    return "\n".join(lines)
