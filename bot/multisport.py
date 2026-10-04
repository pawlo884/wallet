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
        "coach": bool(cfg.get("coach", True)),
        # Wejścia bieżącego miesiąca jako klucze „data|typ” (do powiadomień o nowych treningach).
        "current_visits": sorted(f"{d}|{t}" for d, t in visits if d.startswith(cur["month"])),
        "avg_visits": avg, "avg_saving": avg_saving,
        "avg_per_visit": round(fee / avg, 2) if avg else None,
        "worth_it": (avg is not None and breakeven is not None and avg >= breakeven),
        "current": {"visits": cur["visits"], "to_breakeven": max((breakeven or 0) - cur["visits"], 0), "days_left": days_left},
    }


async def coach(core: "Core") -> list:
    """Motywacja: gratulacje po nowym wejściu, status w poniedziałki, ostrzeżenie na tydzień przed
    końcem miesiąca, podsumowanie poprzedniego miesiąca. Stan w state.json — nic się nie dubluje."""
    from .core import Reply

    a = await analyze(core)
    if not a or a.get("status") != "ok" or not a.get("coach"):
        return []
    state = core.planned.state
    st = state.data.setdefault("multisport", {})
    today, B, fee = core.today(), a["breakeven"], a["fee"]
    ym, cur = today.strftime("%Y-%m"), a["rows"][-1]
    visits = a["current_visits"]
    out = []

    if st.get("month") != ym:
        if st.get("month") and len(a["rows"]) >= 2 and a["rows"][-2]["month"] == st["month"]:
            p = a["rows"][-2]
            per = f"{p['per_visit']:.0f} zł za wejście" if p["per_visit"] else "żadnego wejścia"
            verdict = "karta się zwróciła 🎉" if p["visits"] >= B else f"zabrakło {B - p['visits']} do progu"
            out.append(Reply(f"📊 *Multisport — {p['label']}:* {p['visits']}/{B} wejść, {per} — {verdict}.\n"
                             f"Nowy miesiąc, nowy licznik. Cel: {B} wejść, czyli ~{math.ceil(B / 4.3)} w tygodniu 💪"))
        first_run = "month" not in st
        st.update(month=ym, seen=visits if first_run else [], weekly=None, late=None)

    new = [v for v in visits if v not in set(st["seen"])]
    if new:
        k = len(visits)
        kinds = ", ".join(TYPE_PL.get(v.split("|")[1], v.split("|")[1]) for v in new)
        before = f"{fee / (k - len(new)):.0f} zł → " if k - len(new) else ""
        msg = f"💪 *Wejście {k}/{B}* ({kinds}) — koszt wejścia w tym miesiącu: {before}*{fee / k:.0f} zł*"
        if k >= B and k - len(new) < B:
            msg += "\n🎉 Karta zwróciła się w tym miesiącu! Każde kolejne wejście to czysty zysk."
        elif k < B:
            msg += f"\nDo progu opłacalności: {B - k}."
        out.append(Reply(msg))
        st["seen"] = visits

    left = max(B - cur["visits"], 0)
    days_left = a["current"]["days_left"]
    if today.weekday() == 0 and st.get("weekly") != today.isoformat() and today.day > 1:
        weeks = max(days_left / 7, 1 / 7)
        pace = f"~{math.ceil(left / weeks)} w tygodniu" if left else "próg już zaliczony ✅"
        cost = f"{fee / cur['visits']:.0f} zł" if cur["visits"] else f"{fee:.0f} zł (jeszcze ani jednego)"
        out.append(Reply(f"📅 *Multisport — tydzień:* {cur['visits']}/{B} wejść, zostało {days_left} dni → {pace}.\n"
                         f"Teraz jedno wejście kosztuje Cię {cost}."))
        st["weekly"] = today.isoformat()
    if 0 < days_left <= 7 and left and st.get("late") != ym:
        out.append(Reply(f"⏰ *Tydzień do końca miesiąca:* {cur['visits']}/{B} wejść. Brakuje {left} — "
                         f"{'da się, ' if left <= days_left else ''}to {left} treningów w {days_left} dni."))
        st["late"] = ym
    state._save()
    return out


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
