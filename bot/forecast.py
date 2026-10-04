"""Prognoza na N miesięcy z żywych danych bota: płatności cykliczne, raty, długi, salda oszczędności.

Założenia, których bot nie zna (kwota „na życie”, cele oszczędności), są w config/forecast.yaml.
Wynik: słownik gotowy do JSON (strona WWW) i krótki tekst do /prognoza.
"""

import calendar
import json
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from .core import Core

MIES = ["styczeń", "luty", "marzec", "kwiecień", "maj", "czerwiec", "lipiec", "sierpień", "wrzesień",
        "październik", "listopad", "grudzień"]


def _month_add(d: date, k: int) -> date:
    m = d.month - 1 + k
    return date(d.year + m // 12, m % 12 + 1, 1)


def _month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def _z(v: float) -> str:
    """Kwota do tekstu: „2 181 zł”, „−976 zł”."""
    return f"{v:,.0f}".replace(",", " ").replace("-", "−") + " zł"


def _m(ym: str) -> str:
    y, m = map(int, ym.split("-"))
    return f"{MIES[m - 1]} {y}"


def _is_yearly(rrule: str) -> bool:
    r = rrule.upper()
    return "FREQ=YEARLY" in r or "INTERVAL=12" in r


def load_assumptions(path: str) -> dict:
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) if p.is_file() else {}
    raw = raw or {}
    living = raw.get("living") or {"bazowy": 1400}
    return {
        "months": int(raw.get("months", 12)),
        "living": {str(k): float(v) for k, v in living.items()},
        "savings": raw.get("savings") or [],
    }


async def build(core: "Core") -> dict:
    await core.refresh_catalog()
    cfg = load_assumptions(core.cfg.forecast_file)
    today = core.today()
    start = today.replace(day=1)
    months = [_month_add(start, k) for k in range(cfg["months"])]
    sched = core.planned.schedule
    living = cfg["living"]
    base_key = "bazowy" if "bazowy" in living else next(iter(living))

    # --- przepływy miesięczne z harmonogramu ---
    rows = []
    for m0 in months:
        m1 = _month_end(m0)
        income = planned = debt_part = 0.0
        yearly = []
        for p in sched.payments:
            for d in p.occurrences(m0, m1):
                v = p.signed()
                if v > 0:
                    income += v
                else:
                    planned += -v
                    if p.debt:
                        debt_part += -v
                    if _is_yearly(p.rrule):
                        yearly.append({"name": p.name, "amount": -v, "date": d.isoformat()})
        rows.append({"month": m0.strftime("%Y-%m"), "income": round(income, 2), "planned": round(planned, 2),
                     "debt": round(debt_part, 2), "yearly": yearly})

    # --- oszczędności: salda z Wallet + planowane wpłaty ---
    savings = []
    for s in cfg["savings"]:
        acc_id = core._find_account(s.get("account"))
        acc = core._accounts.get(acc_id or "", {})
        balance = float((acc.get("balance") or {}).get("currentBalance", 0)) if acc else 0.0
        target, monthly = float(s.get("target") or 0), float(s.get("monthly") or 0)
        since = str(s.get("from") or "")[:7]  # opcjonalnie: wpłaty od miesiąca RRRR-MM
        path, b = [], balance
        for m0 in months:
            add = min(monthly, max(target - b, 0)) if target else monthly
            if since and m0.strftime("%Y-%m") < since:
                add = 0
            b += add
            path.append({"balance": round(b, 2), "deposit": round(add, 2)})
        reach = next((months[i].strftime("%Y-%m") for i, x in enumerate(path) if target and x["balance"] >= target), None)
        savings.append({"name": s.get("account"), "found": bool(acc), "balance": round(balance, 2), "target": target,
                        "monthly": monthly, "since": since or None, "path": path, "reach": reach})

    for i, r in enumerate(rows):
        r["savings"] = round(sum(s["path"][i]["deposit"] for s in savings), 2)
        for name, amount in living.items():
            r[f"net_{name}"] = round(r["income"] - r["planned"] - amount, 2)
        r["free"] = round(r[f"net_{base_key}"] - r["savings"], 2)  # po odłożeniu na oszczędności
    for name in list(living) + ["free"]:
        cum = 0.0
        for r in rows:
            cum += r[f"net_{name}"] if name != "free" else r["free"]
            r[f"cum_{name}"] = round(cum, 2)

    # --- długi: stan dziś (z Wallet) i trajektoria wg rat z harmonogramu ---
    debts = []
    linked = {p.debt: p for p in sched.payments if p.debt}
    for d in core.debts.items.values():
        left = core.debts.remaining(d, await core.debts.payments(d))
        p = linked.get(d.id)
        path, end, interest, cur = [], None, 0.0, left
        for m0 in months:
            m1 = _month_end(m0)
            if p and cur > 0:
                for _ in p.occurrences(max(m0, today + timedelta(days=1)), m1):
                    if d.monthly_rate:
                        i = cur * d.monthly_rate
                        interest += i
                        cur = cur + i - p.amount
                    else:
                        cur -= p.amount
                    cur = max(round(cur, 2), 0)
                    if cur == 0 and not end:
                        end = m0.strftime("%Y-%m")
            path.append(round(cur, 2))
        if p and not end:  # koniec poza horyzontem — ostatni termin raty
            occ = p.occurrences(today, today + timedelta(days=365 * 30))
            end = occ[-1].strftime("%Y-%m") if occ else None
        debts.append({"id": d.id, "name": d.name, "now": left, "path": path, "installment": d.installment,
                      "rate": d.monthly_rate, "end": end, "planned": bool(p), "interest": round(interest, 2),
                      "freed_from": end if p and end else None, "freed": p.amount if p else 0})

    # --- wnioski ---
    base = f"net_{base_key}"
    insights = []
    worst = min(rows, key=lambda r: r[base])
    if worst[base] < 0:
        y, m = map(int, worst["month"].split("-"))
        what = ", ".join(f"{x['name']} {_z(x['amount'])}" for x in worst["yearly"]) or "wysokie płatności"
        insights.append({"kind": "warn", "title": f"{MIES[m - 1].capitalize()} {y} na minusie",
                         "text": f"Wynik {_z(worst[base])} ({what}). Zostaw nadwyżkę z wcześniejszych miesięcy na ten moment."})
    yearly_total = sum(x["amount"] for r in rows for x in r["yearly"])
    if yearly_total:
        insights.append({"kind": "info", "title": "Opłaty roczne",
                         "text": f"W horyzoncie {_z(yearly_total)} opłat rocznych — ok. {_z(yearly_total / len(rows))} miesięcznie do odkładania."})
    for d in debts:
        if not d["planned"] and d["now"] > 0:
            insights.append({"kind": "neg", "title": f"{d['name']}: brak planu spłaty",
                             "text": f"Zostało {_z(d['now'])} i bez stałej wpłaty dług się nie zmniejsza. Dodaj płatność cykliczną z debt: {d['id']}."})
    in_horizon = [d for d in debts if d["planned"] and d["end"] and d["end"] <= rows[-1]["month"]]
    if in_horizon:
        freed = sum(d["freed"] for d in in_horizon)
        last = max(d["end"] for d in in_horizon)
        insights.append({"kind": "ok", "title": "Koniec małych kredytów",
                         "text": f"{', '.join(d['name'] for d in in_horizon)} spłacone do: {_m(last)}. Zwalnia się {_z(freed)} miesięcznie."})
    for d in debts:
        if d["interest"]:
            yearly_rate = f"{d['rate'] * 1200:.2f}".replace(".", ",")
            insights.append({"kind": "info", "title": f"Odsetki: {d['name']}",
                             "text": f"Ok. {_z(d['interest'])} w najbliższych {len(rows)} miesiącach ({yearly_rate}% rocznie)."})
    neg_free = [r["month"] for r in rows if r["cum_free"] < 0]
    if neg_free:
        insights.append({"kind": "warn", "title": "Za duże wpłaty na oszczędności",
                         "text": f"Po odłożeniu na konta oszczędnościowe brakuje pieniędzy w: {_m(neg_free[0])}. Zmniejsz wpłaty w config/forecast.yaml albo ustaw im późniejszy start (from)."})
    for s in savings:
        if s["target"] and not s["monthly"]:  # wpłaty tylko z dodatkowych dochodów
            missing = max(s["target"] - s["balance"], 0)
            insights.append({"kind": "ok" if not missing else "info", "title": s["name"],
                             "text": f"Wpłaty z dodatkowych dochodów. Jest {_z(s['balance'])} z {_z(s['target'])}"
                                     + (f", brakuje {_z(missing)}." if missing else " — cel osiągnięty.")})
        elif s["target"]:
            insights.append({"kind": "ok" if s["reach"] else "info", "title": s["name"],
                             "text": (f"Cel {_z(s['target'])} osiągnięty: {_m(s['reach'])}, przy {_z(s['monthly'])}/mies."
                                      if s["reach"] else f"Przy {_z(s['monthly'])}/mies. cel {_z(s['target'])} poza horyzontem.")
                             + (f" Wpłaty od: {_m(s['since'])}." if s["since"] else "")})

    # --- majątek: salda kont (EUR po kursie NBP) + inwestycje − długi ---
    accounts_pln = 0.0
    for acc in core._accounts.values():
        if acc.get("excludeFromStats"):
            continue
        bal = float((acc.get("balance") or {}).get("currentBalance", 0))
        cur = acc.get("currencyCode") or "PLN"
        if cur != "PLN" and bal:
            try:
                bal, _, _ = await core.fx.convert(bal, cur, "PLN", today)
            except Exception:
                continue
        accounts_pln += bal
    invest = await core.investments.valuate()
    invest_total = round(sum(i["value"] or 0 for i in invest), 2)
    debt_now = round(sum(d["now"] for d in debts), 2)

    return {
        "generated": today.isoformat(),
        "investments": invest,
        "worth": {"accounts": round(accounts_pln, 2), "investments": invest_total, "debts": debt_now,
                  "net": round(accounts_pln + invest_total - debt_now, 2)},
        "base": base_key,
        "living": living,
        "rows": rows,
        "debts": debts,
        "savings": savings,
        "insights": insights,
        "totals": {
            "debt_now": round(sum(d["now"] for d in debts), 2),
            "debt_end": round(sum(d["path"][-1] for d in debts), 2) if debts else 0,
        },
    }


def summary_text(f: dict, url: str = "") -> str:
    from .core import fmt_money

    rows, base = f["rows"], f"net_{f['base']}"
    typical = sorted(r[base] for r in rows)[len(rows) // 2]
    cur = "PLN"
    lines = [
        f"🔮 *Prognoza {rows[0]['month']} – {rows[-1]['month']}*",
        f"Przychody: {fmt_money(rows[0]['income'], cur)} · plan: {fmt_money(-rows[0]['planned'], cur)} · na życie: {fmt_money(-f['living'][f['base']], cur)}",
        f"Typowy miesiąc: *{fmt_money(typical, cur)}*",
        f"Narastająco na koniec: *{fmt_money(rows[-1]['cum_' + f['base']], cur)}* "
        f"(widełki {fmt_money(min(rows[-1]['cum_' + k] for k in f['living']), cur)} … {fmt_money(max(rows[-1]['cum_' + k] for k in f['living']), cur)})",
        f"Długi: {fmt_money(f['totals']['debt_now'], cur)} → {fmt_money(f['totals']['debt_end'], cur)}",
    ]
    w = f.get("worth")
    if w:
        lines.append(f"Majątek netto: konta {fmt_money(w['accounts'], cur)} + inwestycje {fmt_money(w['investments'], cur)}"
                     f" − długi {fmt_money(w['debts'], cur)} = *{fmt_money(w['net'], cur)}*")
    if f["savings"]:
        lines.append("Oszczędności: " + " · ".join(f"{s['name']} {fmt_money(s['balance'], cur)}/{fmt_money(s['target'], cur)}" for s in f["savings"]))
    if f["insights"]:
        lines.append("")
        icons = {"warn": "⚠️", "neg": "❗", "ok": "✅", "info": "ℹ️"}
        lines += [f"{icons.get(i['kind'], '•')} *{i['title']}*: {i['text']}" for i in f["insights"][:6]]
    if url:
        lines.append(f"\nWykresy i tabela: {url}")
    lines.append("Założenia: config/forecast.yaml")
    return "\n".join(lines)


def render_html(f: dict, template: str) -> str:
    return template.replace("/*__DATA__*/null", json.dumps(f, ensure_ascii=False))
