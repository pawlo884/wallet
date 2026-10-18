"""Test przepływu bez sieci: atrapy Wallet API i Claude. Uruchom: python -m tests.test_offline"""
import asyncio
import inspect
import os

os.environ.setdefault("WALLET_API_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:abc")
# stan (w tym szkice z data/session.json) w katalogu tymczasowym, nie w repo
import tempfile as _tempfile
os.environ.setdefault("STATE_FILE", os.path.join(_tempfile.mkdtemp(), "state.json"))

from bot.config import Config
from bot.core import Core
from bot.parser import ParsedRecord, ParseResult
from bot.wallet_api import UNKNOWN_EXPENSE

ACC = [
    {"id": "pln", "name": "Ogólne", "currencyCode": "PLN", "recordStats": {"recordCount": 182}, "balance": {"currentBalance": -10.5}},
    {"id": "eur", "name": "Euro", "currencyCode": "EUR", "recordStats": {"recordCount": 2}, "balance": {"currentBalance": 325}},
]
CAT = [{"id": "food", "name": "Groceries", "group": {"name": "Food & Drinks"}}]


class FakeWallet:
    def __init__(self):
        self.created, self.deleted = [], []
    async def accounts(self): return ACC
    async def categories(self): return CAT
    async def create_records(self, recs):
        self.created += recs
        return [{"inputIndex": i, "success": True, "id": f"r{i}"} for i in range(len(recs))]
    async def delete_records(self, ids): self.deleted += ids
    async def records(self, *a, **k):
        return [{"amount": {"value": -50}, "convertedAmount": {"value": -50}, "category": {"name": "Groceries"}},
                {"amount": {"value": 100}, "convertedAmount": {"value": 100}}]
    async def close(self): pass


def rec(**kw):
    base = dict(currency=None, counterparty=None, note="")
    return ParsedRecord(**{**base, **kw})


class FakeParser:
    def __init__(self):
        self.histories = []

    async def parse(self, text, images, today, catalog_prompt, history=None, memory="", **kw):
        assert "Ogólne" in catalog_prompt and "Groceries" in catalog_prompt
        self.histories.append(history or [])
        return ParseResult(question=None, amends=False, records=[
            rec(amount=-54.3, type="expense", category_id="food", account_id="pln", date=today.isoformat(), counterparty="Biedronka"),
            rec(amount=20, type="expense", category_id="nope", account_id="bad", date="2999-01-01", note="x"),
        ])

    async def parse_books(self, images, text):
        return []


class FakeFX:
    async def convert(self, amount, src, dst, on):
        rates = {"USD": 3.8881, "EUR": 4.3745, "PLN": 1.0}
        ratio = rates[src] / rates[dst]
        return round(amount * ratio, 2), ratio, on

    async def rate(self, code, on):
        return {"USD": 3.8881, "EUR": 4.3745, "CHF": 4.6, "GBP": 5.1, "PLN": 1.0}[code], on

    async def close(self):
        pass


async def main():
    core = Core(Config())
    core.wallet, core.parser = FakeWallet(), FakeParser()
    r = await core.handle_message("tg:1", "biedronka 54,30", [])
    print(r.text, r.buttons, sep="\n")
    ok = r.buttons[0][1]
    assert (await core.handle_callback("tg:2", ok)).text.startswith("Ten szkic"), "obcy user nie może zatwierdzić"
    saved = await core.handle_callback("tg:1", ok)
    print(saved.text, saved.buttons, sep="\n")
    c = core.wallet.created
    assert c[0]["amount"]["value"] == -54.3 and c[0]["counterParty"] == "Biedronka"
    assert c[1]["categoryId"] == UNKNOWN_EXPENSE and c[1]["accountId"] == "pln" and c[1]["amount"]["value"] == -20
    assert c[0]["note"] == "biedronka 54,30", "pusta notatka → treść wiadomości"
    undo = await core.handle_callback("tg:1", saved.buttons[0][1])
    print(undo.text); assert core.wallet.deleted == ["r0", "r1"]
    print((await core.balances()).text)
    seen = {}
    orig_records = core.wallet.records
    async def spy(*a, **k):
        seen.update(k)
        return await orig_records(*a, **k)
    core.wallet.records = spy
    print((await core.month_summary()).text)
    assert seen.get("accountId") == "pln,eur", "podsumowanie tylko z aktywnych kont (bez archiwum)"
    core.wallet.records = orig_records

    # głosówka: transkrypcja → zwykły przepływ, transkrypt na górze odpowiedzi
    class FakeSTT:
        async def transcribe(self, audio):
            assert audio == b"OggS..."
            return "biedronka 54,30"
    core.stt = FakeSTT()
    v = await core.handle_voice("tg:1", b"OggS...")
    print(v.text)
    assert v.text.startswith("🎤 „biedronka 54,30”") and v.buttons[0][0] == "✅ Zapisz"
    import bot.stt  # moduł importuje się bez ładowania modelu
    # dekodowanie audio tak, jak robi to faster-whisper (łapie niezgodne wersje PyAV) — bez modelu
    import io
    from pathlib import Path
    from faster_whisper.audio import decode_audio
    samples = decode_audio(io.BytesIO(Path(__file__).with_name("fixtures").joinpath("biedronka.wav").read_bytes()))
    assert len(samples) > 16000, "co najmniej 1 s audio"

    # adaptery się budują
    from bot import telegram_bot
    telegram_bot.build(core)
    # SDK ma messages.parse z output_format
    from anthropic import AsyncAnthropic
    assert "output_format" in inspect.signature(AsyncAnthropic(api_key="x").messages.parse).parameters
    print("\nOK")

asyncio.run(main())


async def planned():
    """Płatności cykliczne na prawdziwym schedule.yaml (lub przykładzie)."""
    import tempfile
    from pathlib import Path
    from bot.planned import Planned

    sched = "config/schedule.yaml" if Path("config/schedule.yaml").exists() else "config/schedule.example.yaml"
    state = Path(tempfile.mkdtemp()) / "state.json"
    os.environ.update(SCHEDULE_FILE=sched, STATE_FILE=str(state))
    core = Core(Config())
    core.wallet, core.parser = FakeWallet(), FakeParser()
    core.today = lambda: __import__("datetime").date(2026, 10, 4)
    today = core.today()

    pending = core.planned.pending(today)
    print("\nDo potwierdzenia:", [(p.id, str(d)) for p, d in pending])
    first = await core.due_reminders()
    assert len(first) == len(pending) and await core.due_reminders() == [], "max 1 przypomnienie dziennie"
    print(first[0].text, first[0].buttons)

    keys = {p.id: f"{p.id}@{d:%Y%m%d}" for p, d in pending}
    if "mieszkanie" in keys:
        r = await core.handle_callback("tg:1", f"sp:{keys['mieszkanie']}")
        print(r.text)
        rec = core.wallet.created[-1]
        assert rec["amount"]["value"] == -1000 and rec["recordDate"].startswith("2026-10-01"), rec
        assert (await core.handle_callback("tg:1", f"sp:{keys['mieszkanie']}")).text.startswith("Już")
        undo = await core.handle_callback("tg:1", r.buttons[0][1])
        assert keys["mieszkanie"] in {f"{p.id}@{d:%Y%m%d}" for p, d in core.planned.pending(today)}, "cofnięcie przywraca termin"
        print(undo.text)

        print((await core.handle_callback("tg:1", f"sa:{keys['ubezp-mbank']}")).text)
        r = await core.handle_message("tg:1", "950,50", [])
        print(r.text)
        assert core.wallet.created[-1]["amount"]["value"] == -950.5

        print((await core.handle_callback("tg:1", f"ss:{keys['multisport']}")).text)
        reloaded = Planned(core)  # stan z pliku
        assert reloaded.state.handled(keys["multisport"])["status"] == "skipped"
        assert reloaded.state.handled(keys["ubezp-mbank"])["status"] == "paid"

    replies = await core.planned_overview()
    picker = replies[-1]
    assert picker.column and all(d.startswith('pk:') for _, d in picker.buttons)
    future_key = picker.buttons[0][1][3:]
    r = await core.handle_callback('tg:1', picker.buttons[0][1])
    print('\nWybór:', r.text, r.buttons)
    assert r.text.startswith('📅 *Termin')
    r = await core.handle_callback('tg:1', f'sp:{future_key}')
    print(r.text)
    assert core.wallet.created[-1]['recordDate'].startswith('2026-10-04'), 'opłata z góry = data dzisiejsza'
    later = __import__('datetime').date(2026, 11, 30)
    assert future_key not in {f'{p.id}@{d:%Y%m%d}' for p, d in core.planned.pending(later)}
    for r in await core.planned_overview():
        print("---\n" + r.text, r.buttons or "")
    print("\nOK planned")


asyncio.run(planned())


async def plans():
    """Dodawanie i usuwanie płatności cyklicznych przez bota (/plan, /plany)."""
    import datetime as dt
    import tempfile
    from pathlib import Path
    from bot.parser import PlanDraft, PlanResult

    state = Path(tempfile.mkdtemp()) / "state.json"
    os.environ.update(SCHEDULE_FILE="config/schedule.yaml", STATE_FILE=str(state))

    class PlanParser(FakeParser):
        async def parse_plan(self, text, today, catalog_prompt):
            return PlanResult(question=None, plan=PlanDraft(
                name="Netflix", amount=49, type="expense", category_id="food", freq="MONTHLY",
                interval=1, first_date="2026-10-15", count=None, counterparty=None))

    core = Core(Config())
    core.wallet, core.parser = FakeWallet(), PlanParser()
    core.today = lambda: dt.date(2026, 10, 4)
    n_before = len(core.planned.schedule.payments)

    assert "/plan" in (await core.plan_add("tg:1", "")).text
    draft = await core.plan_add("tg:1", "netflix 49 co miesiąc 15-go")
    print(draft.text, draft.buttons, sep="\n")
    assert "15.10.2026" in draft.text
    print((await core.handle_callback("tg:1", draft.buttons[0][1])).text)
    assert core.planned.schedule.get("netflix") and len(core.planned.schedule.payments) == n_before + 1
    assert Path(state).with_name("payments.json").is_file(), "zapis na wolumenie"

    lst = await core.plans_list()
    assert lst.column and ("🗑 Netflix", "rm:netflix") in lst.buttons
    # termin 15.10 widoczny jako nadchodzący, bez zaległości sprzed dodania
    assert ("netflix", dt.date(2026, 10, 15)) in [
        (p.id, d) for p, d in core.planned.pending(dt.date(2026, 10, 15))
    ]

    ask = await core.handle_callback("tg:1", "rm:netflix")
    assert ask.buttons[0][1] == "rmy:netflix"
    print((await core.handle_callback("tg:1", "rmy:netflix")).text)
    assert not core.planned.schedule.get("netflix")

    # płatność z pliku: wyłączana w stanie, plik nietknięty
    print((await core.handle_callback("tg:1", "rmy:spotify")).text)
    from bot.planned import Planned
    assert not Planned(core).schedule.get("spotify") and "spotify" in Path("config/schedule.yaml").read_text(encoding="utf-8")
    print("\nOK plans")


asyncio.run(plans())


async def memory_and_fx():
    """Pamięć rozmowy (poprawki szkicu i zapisanego rekordu) + przeliczanie walut."""
    import datetime as dt

    class ScriptedParser(FakeParser):
        def __init__(self, script):
            super().__init__()
            self.script = list(script)

        async def parse(self, text, images, today, catalog_prompt, history=None, memory="", **kw):
            self.histories.append(history or [])
            return self.script.pop(0)

    anthropic15 = rec(amount=15, currency="USD", type="expense", category_id="food", account_id="pln",
                      date="2026-10-04", counterparty="Anthropic")
    script = [
        ParseResult(records=[], amends=False, question="Ile wyniosło doładowanie?"),          # 1: brak kwoty
        ParseResult(records=[anthropic15], amends=False, question=None),                       # 2: „15$”
        ParseResult(records=[anthropic15.model_copy(update={"amount": 20})], amends=True, question=None),  # 3: „zmień na 20$”
        ParseResult(records=[anthropic15.model_copy(update={"amount": 25, "note": "x · 20,00 USD po 3,8881 (NBP 04.10)"})],
                    amends=True, question=None),                                               # 4: po zapisie „jednak 25$”
    ]
    core = Core(Config())
    core.wallet, core.parser, core.fx = FakeWallet(), ScriptedParser(script), FakeFX()
    core.today = lambda: dt.date(2026, 10, 4)

    r1 = await core.handle_message("tg:1", "doładowanie konta anthropic", [])
    r2 = await core.handle_message("tg:1", "15$", [])
    hist = core.parser.histories[1]
    assert [m["role"] for m in hist] == ["user", "assistant"] and "anthropic" in hist[0]["content"], hist
    print(r2.text)
    assert "−58,32 PLN" in r2.text and "15,00 USD po 3,8881" in r2.text, r2.text

    r3 = await core.handle_message("tg:1", "zmień na 20$", [])
    print(r3.text)
    assert len(core._drafts) == 1 and "−77,76 PLN" in r3.text, "poprawka zastępuje szkic"
    saved = await core.handle_callback("tg:1", r3.buttons[0][1])
    assert core.wallet.created[-1]["amount"]["value"] == -77.76

    r4 = await core.handle_message("tg:1", "jednak 25$", [])
    print(r4.text, r4.buttons)
    assert r4.text.startswith("✏️") and r4.buttons[0][0] == "✅ Zapisz poprawkę"
    assert r4.text.count("NBP") == 1, "stara notatka z przeliczeniem usunięta"
    done = await core.handle_callback("tg:1", r4.buttons[0][1])
    print(done.text)
    assert core.wallet.deleted == ["r0"] and core.wallet.created[-1]["amount"]["value"] == -97.2

    # model powtarza rekord z otwartego szkicu przy nowej transakcji → odfiltrowany
    kawa = rec(amount=14, type="expense", category_id="food", account_id="pln", date="2026-10-04")
    core.parser.script = [
        ParseResult(records=[anthropic15], amends=False, question=None),
        ParseResult(records=[anthropic15, kawa], amends=False, question=None),
    ]
    await core.handle_message("tg:1", "anthropic 15$", [])
    r5 = await core.handle_message("tg:1", "i jeszcze kawa 14", [])
    print(r5.text)
    assert "Anthropic" not in r5.text and "−14,00 PLN" in r5.text

    print((await core.fx_quote("100 eur")).text)
    print((await core.fx_quote("")).text)
    assert "437,45 PLN" in (await core.fx_quote("100 eur")).text
    print("\nOK memory+fx")


asyncio.run(memory_and_fx())


async def statements():
    """Wyciąg: zgodne z Wallet, płatność cykliczna, brakujące → szkice; .eml; wykrywanie wklejki."""
    import datetime as dt
    import tempfile
    from pathlib import Path
    from bot.statement import email_to_text, looks_like_statement

    # Własny harmonogram testowy — prawdziwe kwoty w config/schedule.yaml mogą się zmieniać.
    tmp = Path(tempfile.mkdtemp())
    (tmp / "schedule.yaml").write_text(
        "start: 2026-10-01\npayments:\n"
        "  - {id: multisport, name: Multi Sport, amount: 225, category_id: food, rrule: FREQ=MONTHLY, from: 2025-09-03}\n",
        encoding="utf-8",
    )
    os.environ.update(SCHEDULE_FILE=str(tmp / "schedule.yaml"), STATE_FILE=str(tmp / "s.json"))

    ops = [
        rec(amount=54.3, type="expense", category_id="food", account_id="pln", date="2026-10-04", counterparty="Biedronka", note="zakupy"),
        rec(amount=59.10, type="expense", category_id="food", account_id="pln", date="2026-10-05", counterparty="Anthropic", note="API"),
        rec(amount=225, type="expense", category_id="food", account_id="pln", date="2026-10-03", counterparty="Benefit Systems", note="multisport"),
        rec(amount=37.5, type="expense", category_id="food", account_id="pln", date="2026-10-05", counterparty="Apteka", note="leki"),
    ]

    class StmtParser(FakeParser):
        async def parse_statement(self, text, today, catalog_prompt, memory=""):
            return ParseResult(records=[r.model_copy() for r in ops], amends=False, question=None)

    class StmtWallet(FakeWallet):
        async def records(self, *a, **k):
            assert k.get("accountId") == "pln,eur", "tylko aktywne konta"
            return [
                {"id": "w1", "amount": {"value": -54.3}, "recordDate": "2026-10-04T10:00:00Z", "counterParty": "Biedronka"},
                # 15 USD po NBP = 58,32 zł; bank pobrał 59,10 zł → zgodne w tolerancji kursu
                {"id": "w2", "amount": {"value": -58.32}, "recordDate": "2026-10-04T12:00:00Z", "counterParty": "Anthropic",
                 "note": "doładowanie · 15,00 USD po 3,8881 (NBP 02.10)"},
            ]

    core = Core(Config())
    core.wallet, core.parser = StmtWallet(), StmtParser()
    core.today = lambda: dt.date(2026, 10, 6)

    replies = await core.statement("tg:1", "wyciąg…")
    for r in replies:
        print("---\n" + r.text, r.buttons or "")
    assert "Operacje z wyciągu: 4" in replies[0].text and "już w Wallet: 2" in replies[0].text and "brakuje: 1" in replies[0].text
    assert any(b[1].startswith("sp:multisport@20261003") for r in replies for b in r.buttons), "płatność cykliczna"
    draft = replies[-1]
    assert "Apteka" in draft.text and draft.buttons[0][1].startswith("ok:")
    await core.handle_callback("tg:1", draft.buttons[0][1])
    assert core.wallet.created[-1]["amount"]["value"] == -37.5

    # szkic z maila ("*") może zatwierdzić każda dozwolona osoba
    mail_replies = await core.reconciler.reconcile("*", "x", "maila")
    ok = mail_replies[-1].buttons[0][1]
    assert (await core.handle_callback("tg:7", ok)).text.startswith("✅")

    eml = (
        "From: Pawel <pawlo884@gmail.com>\r\nSubject: Fwd: Zestawienie operacji\r\nMIME-Version: 1.0\r\n"
        "Content-Type: text/html; charset=utf-8\r\n\r\n"
        "<p>04.10 BIEDRONKA <b>-54,30</b> PLN</p><table><tr><td>05.10</td><td>APTEKA</td><td>-37,50</td></tr></table>"
    ).encode()
    sender, subject, text = email_to_text(eml)
    print(sender, subject, repr(text))
    assert "pawlo884@gmail.com" in sender and "BIEDRONKA -54,30 PLN" in text and "APTEKA | -37,50" in text
    assert looks_like_statement("04.10 BIEDRONKA -54,30\n" * 20) and not looks_like_statement("biedronka 54,30")
    print("\nOK statements")


asyncio.run(statements())


async def debts():
    """Dług bez stałych rat: /dlug, spłata z etykietą, ile zostało, /dlugi."""
    import datetime as dt
    import tempfile
    from pathlib import Path

    os.environ.update(SCHEDULE_FILE="config/schedule.yaml", STATE_FILE=str(Path(tempfile.mkdtemp()) / "s.json"))

    class DebtWallet(FakeWallet):
        def __init__(self):
            super().__init__()
            self.label_records = []
        async def labels(self): return []
        async def create_label(self, name, color="Orange"): return {"id": "lbl-a6", "name": name}
        async def create_records(self, recs):
            self.label_records += [r for r in recs if r.get("labelIds") == ["lbl-a6"]]
            return await super().create_records(recs)
        async def records(self, *a, **k):
            if k.get("labelId") == "lbl-a6":
                return [{"amount": r["amount"]} for r in self.label_records]
            return await super().records(*a, **k)

    class DebtParser(FakeParser):
        async def parse(self, text, images, today, catalog_prompt, history=None, memory="", **kw):
            assert "a6 | A6" in catalog_prompt, "prompt zna długi"
            return ParseResult(amends=False, question=None, records=[
                rec(amount=500, type="expense", category_id="food", account_id="pln", date=today.isoformat(),
                    note="spłata A6", debt_id="a6")])

    core = Core(Config())
    core.wallet, core.parser = DebtWallet(), DebtParser()
    core.today = lambda: dt.date(2026, 10, 4)

    print((await core.debt_add("A6 9 100")).text)
    assert core.debts.items["a6"].total == 9100 and core.debts.items["a6"].label_id == "lbl-a6"
    draft = await core.handle_message("tg:1", "spłata A6 500", [])
    print(draft.text)
    assert "🏦 spłata: A6" in draft.text
    saved = await core.handle_callback("tg:1", draft.buttons[0][1])
    print(saved.text)
    assert core.wallet.created[-1]["labelIds"] == ["lbl-a6"] and "zostało *8 600,00 PLN*" in saved.text
    lst = await core.debts_list()
    print(lst.text)
    assert lst.buttons == [("🗑 Przestań śledzić: A6", "dl:a6")]
    print((await core.handle_callback("tg:1", "dly:a6")).text)
    assert not core.debts.items

    # kredyt: płatność cykliczna powiązana z długiem — potwierdzona rata zmniejsza dług
    from bot.planned import Payment
    core.wallet.create_label = lambda name, color="Orange": asyncio.sleep(0, {"id": "lbl-a6", "name": name})
    core.wallet.label_records = []  # atrapa ma jedną etykietę — czyścimy spłaty A6
    await core.debts.add("Credit Agricole", 57266.30, installment=762.77, paid_before=2165.32, monthly_rate=0.007)
    assert "kapitał do spłaty *55 100,98 PLN*" in await core.debts.status_line(core.debts.items["credit-agricole"])
    assert core.debts.items["credit-agricole"].installment == 762.77
    core.planned.add(Payment(id="kredyt-ca-t", name="Rata CA", amount=762.77, type="expense",
                             category_id="food", rrule="FREQ=MONTHLY;COUNT=101", start=dt.date(2026, 10, 5),
                             debt="credit-agricole"))
    core.today = lambda: dt.date(2026, 10, 5)
    paid = await core.handle_callback("tg:1", "sp:kredyt-ca-t@20261005")
    print(paid.text)
    # rata 762,77 = 385,71 odsetek (0,7% od 55 100,98) + 377,06 kapitału
    assert core.wallet.created[-1]["labelIds"] == ["lbl-a6"] and "kapitał do spłaty *54 723,92 PLN*" in paid.text
    assert "100 rat po 762,77" in paid.text and "oprocentowanie 8,40%" in paid.text
    # termin 29-go: w lutym 2027 → 28.02, liczba rat się nie zmienia
    p29 = Payment(id="t29", name="t", amount=71.1, type="expense", category_id="food",
                  rrule="FREQ=MONTHLY;COUNT=9", start=dt.date(2026, 11, 29))
    occ = p29.occurrences(dt.date(2026, 1, 1), dt.date(2028, 1, 1))
    assert len(occ) == 9 and dt.date(2027, 2, 28) in occ and occ[-1] == dt.date(2027, 7, 29), occ
    print("\nOK debts")


asyncio.run(debts())


async def forecast_web_transfer():
    """Prognoza z harmonogramu + strona WWW + przelew na konto oszczędnościowe."""
    import datetime as dt
    import json as js
    import tempfile
    from pathlib import Path
    from aiohttp.test_utils import TestClient, TestServer
    from bot import forecast, web

    tmp = Path(tempfile.mkdtemp())
    # Własne założenia testowe — prawdziwe config/forecast.yaml może się zmieniać.
    (tmp / "forecast.yaml").write_text(
        "months: 12\nliving: {oszczędny: 900, bazowy: 1400, luźny: 2000}\nsavings:\n"
        "  - {account: Awaryjne, target: 3000, monthly: 300}\n"
        "  - {account: Poduszka finansowa, target: 11600, monthly: 500, from: 2027-02}\n"
        "  - {account: Extra, target: 1000, monthly: 0}\n",
        encoding="utf-8",
    )
    os.environ.update(SCHEDULE_FILE="config/schedule.yaml", STATE_FILE=str(tmp / "s.json"),
                      FORECAST_FILE=str(tmp / "forecast.yaml"))
    ACC.append({"id": "awar", "name": "Awaryjne", "currencyCode": "PLN", "balance": {"currentBalance": 0}})

    class TransferParser(FakeParser):
        async def parse(self, text, images, today, catalog_prompt, history=None, memory="", **kw):
            return ParseResult(amends=False, question=None, records=[
                rec(amount=300, type="expense", category_id="food", account_id="pln", date=today.isoformat(),
                    note="odkładam na awaryjne", transfer_to="awar")])

    core = Core(Config())
    core.wallet, core.parser, core.fx = FakeWallet(), TransferParser(), FakeFX()
    core.today = lambda: dt.date(2026, 10, 4)

    f = await forecast.build(core)
    assert len(f["rows"]) == 12 and f["rows"][0]["month"] == "2026-10"
    jan = next(r for r in f["rows"] if r["month"] == "2027-01")
    assert {y["name"] for y in jan["yearly"]} >= {"Kontener", "Opłata za grobek"}
    assert jan["net_bazowy"] < 0 and any("na minusie" in i["title"] for i in f["insights"])
    assert [s["name"] for s in f["savings"]] == ["Awaryjne", "Poduszka finansowa", "Extra"] and f["savings"][0]["found"]
    assert f["rows"][0]["savings"] == 300 and f["rows"][4]["savings"] == 800 and f["rows"][-1]["cum_free"] < f["rows"][-1]["cum_bazowy"]
    assert any(i["title"] == "Extra" and "dodatkowych dochodów" in i["text"] and "brakuje 1 000 zł" in i["text"] for i in f["insights"])
    # prawdziwa konfiguracja też musi się poprawnie wczytać
    assert forecast.load_assumptions("config/forecast.yaml")["savings"]
    print(forecast.summary_text(f, "https://wallet.example"))
    js.dumps(f)

    async with TestClient(TestServer(web.build_app(core))) as client:
        page = await (await client.get("/")).text()
        assert "const DATA = {" in page and "/*__DATA__*/" not in page
        api = await (await client.get("/api/prognoza")).json()
        assert api["rows"][0]["month"] == "2026-10"
        assert (await client.get("/health")).status == 200

    draft = await core.handle_message("tg:1", "300 na awaryjne", [])
    print(draft.text)
    assert "↔ przelew na: Awaryjne" in draft.text
    await core.handle_callback("tg:1", draft.buttons[0][1])
    sent = core.wallet.created[-1]
    assert sent["transfer"] == {"pairingMode": "new", "accountId": "awar"} and "categoryId" not in sent
    assert sent["amount"]["value"] == -300
    ACC.pop()
    print("\nOK forecast+web+transfer")


asyncio.run(forecast_web_transfer())


async def balance_fix():
    """/korekta: różnica jako wpis albo zmiana salda początkowego."""
    import datetime as dt
    import tempfile
    from pathlib import Path

    os.environ.update(STATE_FILE=str(Path(tempfile.mkdtemp()) / "s.json"))

    class FixWallet(FakeWallet):
        def __init__(self):
            super().__init__()
            self.patched = []
        async def accounts(self):
            return [{**a, "balance": {"currentBalance": -10.5, "initial": 0}} if a["id"] == "pln" else a for a in ACC]
        async def set_initial_balance(self, account_id, initial):
            self.patched.append((account_id, initial))

    core = Core(Config())
    core.wallet, core.parser = FixWallet(), FakeParser()
    core.today = lambda: dt.date(2026, 10, 4)
    await core.refresh_catalog(force=True)

    assert "Podaj" in (await core.balance_fix("tg:1", "")).text
    r = await core.balance_fix("tg:1", "2 345,67")
    print(r.text)
    assert "Różnica: *+2 356,17 PLN*" in r.text and [b[1][:3] for b in r.buttons] == ["kr:", "ki:", "kn:"]
    out = await core.handle_callback("tg:1", r.buttons[1][1])  # saldo początkowe
    print(out.text)
    assert core.wallet.patched == [("pln", 2356.17)]

    r = await core.balance_fix("tg:1", "Euro 300")
    out = await core.handle_callback("tg:1", r.buttons[0][1])  # wpis
    sent = core.wallet.created[-1]
    assert sent["accountId"] == "eur" and sent["amount"]["value"] == -25 and "Korekta salda" in sent["note"]
    assert "nie znam" in (await core.balance_fix("tg:1", "xyz 10")).text.lower()
    print("\nOK balance_fix")


asyncio.run(balance_fix())


async def investments():
    """Inwestycje: dodanie (szkic → ✅), wycena w PLN, zysk, w prognozie jako majątek."""
    import datetime as dt
    import tempfile
    from pathlib import Path
    from bot import forecast
    from bot.parser import InvestmentDraft, InvestmentResult

    os.environ.update(STATE_FILE=str(Path(tempfile.mkdtemp()) / "s.json"), SCHEDULE_FILE="config/schedule.yaml",
                      FORECAST_FILE="config/forecast.yaml")

    class InvParser(FakeParser):
        async def parse_investment(self, text, today):
            return InvestmentResult(question=None, investment=InvestmentDraft(
                name="Srebro", kind="metal", symbol="xag", quantity=2, unit="oz", cost_pln=400.0, bought="2024"))

    core = Core(Config())
    core.wallet, core.parser, core.fx = FakeWallet(), InvParser(), FakeFX()
    core.today = lambda: dt.date(2026, 10, 4)

    async def fake_quote(h):
        assert h.symbol == "XAG"
        return 60.52, "USD"
    core.investments._quote = fake_quote

    assert "/inwestycja" in (await core.investments_list()).text  # pusto → instrukcja
    draft = await core.investment_add("tg:1", "srebro 2 uncje 2024 za 400 zł")
    print(draft.text)
    assert "62,2 g" in draft.text and draft.buttons[0][1].startswith("ia:")
    added = await core.handle_callback("tg:1", draft.buttons[0][1])
    print(added.text)
    lst = await core.investments_list()
    print(lst.text)
    # 60,52 USD × 3,8881 = 235,31 zł/oz × 2 = 470,62 zł; zysk 70,62 zł (+17,7%)
    assert "470,62 PLN" in lst.text and "+70,62 PLN (+17,7%)" in lst.text
    f = await forecast.build(core)
    assert f["worth"]["investments"] == 470.62
    assert "Majątek netto" in forecast.summary_text(f)
    # konto „Srebro” w Wallet: codzienna wycena przez saldo początkowe, bez liczenia podwójnie w majątku
    ACC.append({"id": "silv", "name": "Srebro", "currencyCode": "PLN", "balance": {"currentBalance": 400.0, "initial": 400.0}})
    patched = []
    async def set_initial(aid, initial):
        patched.append((aid, initial))
    core.wallet.set_initial_balance = set_initial
    items = core.investments.items
    hid = next(iter(items))
    items[hid].account = "Srebro"
    core.investments._write(items)
    await core.refresh_catalog(force=True)
    done = await core.investments.sync_accounts()
    assert patched == [("silv", 470.62)] and done, (patched, done)
    f2 = await forecast.build(core)
    assert f2["worth"]["accounts"] == f["worth"]["accounts"], "konto Srebro nie dubluje inwestycji"
    ACC.pop()
    assert (await core.handle_callback("tg:1", f"iry:{lst.buttons[0][1][3:]}")).text.startswith("🗑")
    assert not core.investments.items
    print("\nOK investments")


asyncio.run(investments())


async def multisport_test():
    """Multisport: wejścia ze Stravy vs próg, dedup dzienny, basen z GPS pomijany, w prognozie i stronie."""
    import datetime as dt
    import tempfile
    from pathlib import Path
    from aiohttp.test_utils import TestClient, TestServer
    from bot import forecast, multisport, web

    tmp = Path(tempfile.mkdtemp())
    (tmp / "forecast.yaml").write_text(
        "months: 12\nliving: {bazowy: 1400}\nmultisport: {payment: ms, ticket: 25, months: 3, "
        "sport_types: [WeightTraining, Swim, Yoga]}\n", encoding="utf-8")
    (tmp / "schedule.yaml").write_text(
        "start: 2026-08-01\npayments:\n  - {id: ms, name: Multisport, amount: 232.09, category_id: food, rrule: FREQ=MONTHLY, from: 2026-01-03}\n",
        encoding="utf-8")
    os.environ.update(STATE_FILE=str(tmp / "s.json"), SCHEDULE_FILE=str(tmp / "schedule.yaml"), FORECAST_FILE=str(tmp / "forecast.yaml"))

    def act(day, typ, gps=False):
        return {"id": day + typ, "name": "", "type": typ, "date": day, "ts": 0, "gps": gps, "minutes": 60}

    aug = [act(f"2026-08-{d:02d}", "WeightTraining") for d in range(1, 13)]           # 12 → opłaca się
    sep = [act(f"2026-09-{d:02d}", "WeightTraining") for d in range(1, 5)] + [
        act("2026-09-01", "WeightTraining"),                                              # duplikat dnia → nie liczy się
        act("2026-09-10", "Swim"), act("2026-09-11", "Swim", gps=True),                  # basen + otwarta woda (pomijana)
        act("2026-09-12", "Run"),                                                         # bieg — nie na kartę
    ]
    octo = [act("2026-10-02", "Yoga"), act("2026-10-03", "WeightTraining")]

    class FakeStrava:
        configured = connected = True
        tokens = {"athlete": "Paweł"}
        async def activities(self, after_ts):
            return aug + sep + octo
        async def close(self): pass

    core = Core(Config())
    core.wallet, core.parser, core.fx, core.strava = FakeWallet(), FakeParser(), FakeFX(), FakeStrava()
    core.today = lambda: dt.date(2026, 10, 4)

    a = await multisport.analyze(core)
    assert a["breakeven"] == 10 and [r["visits"] for r in a["rows"]] == [12, 5, 2], [r["visits"] for r in a["rows"]]
    assert a["rows"][1]["by_type"] == {"siłownia": 4, "basen": 1} and a["rows"][2]["partial"]
    assert a["avg_visits"] == 8.5 and not a["worth_it"] and a["current"]["to_breakeven"] == 8
    print(multisport.text(a))
    f = await forecast.build(core)
    assert f["multisport"]["status"] == "ok" and any(i["title"].startswith("Multisport") for i in f["insights"])
    async with TestClient(TestServer(web.build_app(core))) as client:
        assert '"multisport"' in await (await client.get("/")).text()

    class NoStrava(FakeStrava):
        configured = False
    core.strava = NoStrava()
    assert "STRAVA_CLIENT_ID" in (await core.multisport_report()).text
    assert "strava.com/settings/api" in (await core.strava_connect("")).text
    # link autoryzacji jako przycisk-URL (Markdown w treści zjadał podkreślenia w client_id)
    from bot.strava import Strava
    from bot.telegram_bot import _markup
    real = Strava(core); real.client_id, real.client_secret = "123456", "x"
    core.strava = real
    r = await core.strava_connect("")
    url = r.buttons[0][1]
    assert "client_id=123456" in url and "response_type=code" in url and "client_id" not in r.text
    btn = _markup(r).inline_keyboard[0][0]
    assert btn.url == url and btn.callback_data is None
    await real.close()

    # Motywacja: pierwszy przebieg cicho; nowe wejście → gratulacje; poniedziałek → status; koniec miesiąca → ostrzeżenie
    core.strava = FakeStrava()
    acts_now = list(octo)
    async def acts(after_ts):
        return aug + sep + acts_now
    core.strava.activities = acts
    days = iter([dt.date(2026, 10, 4), dt.date(2026, 10, 5), dt.date(2026, 10, 5), dt.date(2026, 10, 25), dt.date(2026, 11, 2)])
    def run(day):
        core.today = lambda: day
        return multisport.coach(core)
    assert await run(next(days)) == [], "pierwsze uruchomienie bez spamu"
    acts_now.append(act("2026-10-05", "Swim"))
    msgs = [r.text for r in await run(next(days))]  # poniedziałek 5.10
    print("\n".join(msgs))
    assert any("Wejście 3/10" in m and "116 zł → *77 zł*" in m for m in msgs) and any("tydzień" in m for m in msgs)
    assert await run(next(days)) == [], "ten sam dzień — bez powtórek"
    msgs = [r.text for r in await run(next(days))]  # 25.10, 6 dni do końca
    assert len(msgs) == 1 and "Tydzień do końca miesiąca" in msgs[0], msgs
    msgs = [r.text for r in await run(next(days))]  # 2.11 (poniedziałek): podsumowanie października + tydzień
    print("\n".join(msgs))
    assert any("październik 2026" in m and "3/10" in m and "zabrakło 7" in m for m in msgs)
    print("\nOK multisport")


asyncio.run(multisport_test())


async def restart():
    """Szkic, „Cofnij”, korekta i ✏️ działają po restarcie kontenera (deploy)."""
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    os.environ.update(STATE_FILE=str(tmp / "s.json"), SCHEDULE_FILE="config/schedule.yaml")

    def boot():
        core = Core(Config())
        core.wallet, core.parser = FakeWallet(), FakeParser()
        return core

    core = boot()
    draft = await core.handle_message("tg:1", "biedronka 54,30", [])
    fix = await core.balance_fix("tg:1", "100")
    key = next(iter(core.planned.schedule.payments)).id
    await core.planned.handle_callback("tg:1", "sa", f"{key}@20261004")

    core = boot()  # „restart”
    assert len(core._drafts) == 1 and core._drafts[next(iter(core._drafts))].records[0].counterparty == "Biedronka"
    saved = await core.handle_callback("tg:1", draft.buttons[0][1])
    assert saved.text.startswith("✅ Zapisano"), saved.text
    assert core._fix_drafts and core.planned._awaiting_amount == {"tg:1": f"{key}@20261004"}

    core = boot()
    undo = await core.handle_callback("tg:1", saved.buttons[0][1])
    assert undo.text.startswith("↩️") and core.wallet.deleted == ["r0", "r1"], undo.text
    r = await core.handle_callback("tg:1", fix.buttons[-1][1])
    assert r.text == "❌ Anulowano.", r.text
    assert not boot()._fix_drafts, "obsłużona korekta znika też z pliku"

    (tmp / "session.json").write_text("{zepsuty", encoding="utf-8")
    assert not boot()._drafts, "uszkodzony plik nie blokuje startu"
    print("\nOK restart")


asyncio.run(restart())


async def memory_test():
    """Bot uczy się sprzedawców: po zapisie „Piekarnia Julka” kolejny paragon dostaje tę samą kategorię i nazwę."""
    import tempfile
    from pathlib import Path
    from bot.memory import merchant_key

    tmp = Path(tempfile.mkdtemp())
    os.environ.update(STATE_FILE=str(tmp / "s.json"), SCHEDULE_FILE="config/schedule.yaml")
    cats = CAT + [{"id": "bakery", "name": "Piekarnia", "group": {"name": "Food & Drinks"}}]

    class MemWallet(FakeWallet):
        async def categories(self): return cats
        async def records(self, *a, **k):
            assert k.get("isTransfer") == "false"
            return [
                {"counterParty": "Lidl", "accountId": "pln", "category": {"id": "food"}, "amount": {"value": -20},
                 "recordDate": "2026-09-01T10:00:00Z", "note": "zakupy"},
                {"counterParty": "LIDL", "accountId": "pln", "category": {"id": "food"}, "amount": {"value": -30},
                 "recordDate": "2026-09-05T10:00:00Z", "note": "owoce"},
            ]

    class MemParser:
        def __init__(self):
            self.memories, self.out = [], None
        async def parse(self, text, images, today, catalog_prompt, history=None, memory="", **kw):
            self.memories.append(memory)
            return ParseResult(question=None, amends=False, records=[self.out(today)])

    def boot():
        core = Core(Config())
        core.wallet, core.parser = MemWallet(), MemParser()
        return core

    assert merchant_key("PIEKARNIA  Julka Sp. z o.o.") == merchant_key("piekarnia julka") == "piekarnia julka"
    core = boot()
    await core.memory.bootstrap()
    assert core.memory.items["lidl"]["count"] == 2 and core.memory.items["lidl"]["note"] == "owoce"

    # pierwszy paragon: model sam wybiera kategorię, użytkownik zapisuje
    core.parser.out = lambda d: rec(amount=12.5, type="expense", category_id="bakery", account_id="eur",
                                    date=d.isoformat(), counterparty="Piekarnia Julka", note="chleb, bułki")
    r = await core.handle_message("tg:1", "", [(b"jpg", "image/jpeg")])
    assert "🧠" not in r.text
    await core.handle_callback("tg:1", r.buttons[0][1])
    assert core.memory.items["piekarnia julka"]["category_id"] == "bakery"

    # po restarcie: pamięć w prompcie; model zwraca inną pisownię i nieznaną kategorię → uzupełnione z pamięci
    core = boot()
    core.parser.out = lambda d: rec(amount=8, type="expense", category_id="nope", account_id="pln",
                                    date=d.isoformat(), counterparty="PIEKARNIA JULKA", note="bułki")
    r = await core.handle_message("tg:1", "julka 8", [])
    print(r.text)
    mem = core.parser.memories[-1]
    assert "Piekarnia Julka | expense | bakery (Piekarnia) | eur (Euro) | chleb, bułki | 1×" in mem, mem
    assert "Lidl" in mem or "LIDL" in mem
    draft = core._drafts[r.buttons[0][1][3:]].records[0]
    assert draft.counterparty == "Piekarnia Julka" and draft.category_id == "bakery"
    assert "🧠 _Jak ostatnio: Piekarnia Julka_" in r.text

    # poprawka uczy: zapis z inną kategorią nadpisuje pamięć, licznik rośnie
    draft.category_id = "food"
    await core.handle_callback("tg:1", r.buttons[0][1])
    assert core.memory.items["piekarnia julka"]["category_id"] == "food" and core.memory.items["piekarnia julka"]["count"] == 2

    # /pamiec i zapominanie
    lst = await core.memory_list()
    print(lst.text)
    btn = next(d for t, d in lst.buttons if "Julka" in t)
    ask = await core.handle_callback("tg:1", btn)
    assert ask.buttons[0][1].startswith("mfy:")
    assert (await core.handle_callback("tg:1", ask.buttons[0][1])).text.startswith("🗑 Zapomniałem")
    assert "piekarnia julka" not in boot().memory.items

    # bootstrap tylko raz (plik już jest) — nie nadpisuje nauczonego
    await boot().memory.bootstrap()
    assert "piekarnia julka" not in boot().memory.items
    (tmp / "learned.json").write_text("{zepsuty", encoding="utf-8")
    assert boot().memory.items == {}, "uszkodzony plik nie blokuje startu"
    print("\nOK pamięć")


asyncio.run(memory_test())


async def lotto_test():
    """/lotto: bilans gier z kategorii Lottery na prawdziwym kształcie rekordów Wallet + szanse."""
    import tempfile
    from pathlib import Path
    from bot import lotto

    os.environ.update(STATE_FILE=str(Path(tempfile.mkdtemp()) / "s.json"), SCHEDULE_FILE="config/schedule.yaml")
    L = lotto.LOTTERY
    rows = [(43.3, "Wygrana EuroJackpot", "2026-10-16"), (-113, "wpłata na lotto", "2026-10-04"),
            (-100, "eurojackpot", "2026-02-24"), (-40, "eurojackot", "2026-02-12"),
            (-100, "EuroJackpot", "2025-12-15"), (-50, "EuroJackpot", "2025-12-02")]

    class LottoWallet(FakeWallet):
        async def records(self, date_from, date_to, **k):
            assert k["categoryId"] == L and date_from == "2000-01-01" and k["accountId"] == "pln,eur", k
            return [{"amount": {"value": v}, "note": n, "recordDate": f"{d}T10:00:00Z", "category": {"id": L}}
                    for v, n, d in rows]

    core = Core(Config())
    core.wallet, core.parser = LottoWallet(), FakeParser()
    core.today = lambda: __import__("datetime").date(2026, 10, 18)
    t = (await core.lotto_report()).text
    print(t)
    assert "🎰 *Eurojackpot*" in t and "Lotto" not in t.replace("/lotto", "")
    assert "Wydane: −403,00 PLN" in t and "Wygrane: 43,30 PLN" in t and "Bilans: *−359,70 PLN*" in t
    assert "Zwrot: 11%" in t and "Największa wygrana: 43,30 PLN (16.10.2026)" in t
    assert "główna wygrana 1 : 139 838 160, jakakolwiek wygrana 1 : 32" in t
    assert round(1 / lotto.ODDS[1]) == 32, "oficjalnie ~1:32"

    class Empty(FakeWallet):
        async def records(self, *a, **k): return []
    core.wallet = Empty()
    assert "Nie widzę" in (await core.lotto_report()).text
    print("\nOK lotto")


asyncio.run(lotto_test())


async def lists_test():
    """Zadania i książki ze zwykłych wiadomości i screenów, przyciski, cofanie, dziennik zdarzeń."""
    import datetime as dt
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace
    from bot.parser import BookAction, TaskAction

    os.environ.update(STATE_FILE=str(Path(tempfile.mkdtemp()) / "state.json"))

    class ListParser(FakeParser):
        def __init__(self):
            super().__init__()
            self.next, self.lists = None, []
        async def parse(self, text, images, today, catalog_prompt, history=None, memory="", lists="", **kw):
            self.lists.append(lists)
            self.on_usage("parse", "claude-haiku-4-5",
                          SimpleNamespace(input_tokens=200, cache_read_input_tokens=3000, output_tokens=80), 420)
            return self.next

    core = Core(Config())
    core.wallet, core.parser = FakeWallet(), ListParser()
    core.parser.on_usage = core._log_usage
    core.today = lambda: dt.date(2026, 10, 18)
    owner = "tg:1"

    # zadanie z terminem + książka do kupienia w jednej wiadomości, bez transakcji
    core.parser.next = ParseResult(records=[], amends=False, question=None, tasks=[
        TaskAction(action="add", task_id=None, title="Zadzwonić do ubezpieczyciela", due="2026-10-19"),
    ], books=[BookAction(action="add", book_id=None, title="Atomowe nawyki", author="James Clear", status="to_buy")])
    r = await core.handle_message(owner, "jutro zadzwonić do ubezpieczyciela i chcę kupić Atomowe nawyki", [])
    print(r.text, r.buttons)
    assert "Nowe zadanie: *Zadzwonić do ubezpieczyciela* (jutro)" in r.text and "Do kupienia" in r.text
    assert r.buttons[0][1].startswith("iu:") and core.wallet.created == []
    task = core.store.open_tasks()[0]
    book = core.store.books()[0]

    # ta sama książka ze screena drugi raz nie dubluje; blok dla modelu zna zadania i książki
    core.parser.next = ParseResult(records=[], amends=False, question=None, books=[
        BookAction(action="add", book_id=None, title="atomowe  NAWYKI", author=None, status="to_buy")])
    r = await core.handle_message(owner, "", [(b"png", "image/png")])
    assert "Już jest na liście" in r.text and len(core.store.books()) == 1
    assert f"{task.id} | Zadzwonić do ubezpieczyciela | 2026-10-19" in core.parser.lists[-1]
    assert f"{book.id} | Atomowe nawyki | James Clear | to_buy" in core.parser.lists[-1]

    # zakup książki z listy: wydatek jako szkic + status „do przeczytania”, oba z cofaniem
    core.parser.next = ParseResult(amends=False, question=None, records=[
        rec(amount=45, type="expense", category_id="food", account_id="pln", date="2026-10-18", counterparty="Empik",
            note="Atomowe nawyki")],
        books=[BookAction(action="status", book_id=book.id, title="Atomowe nawyki", author=None, status="to_read")])
    r = await core.handle_message(owner, "kupiłem atomowe nawyki 45 zł w empiku", [])
    print(r.text, r.buttons)
    assert "→ 📚 Do przeczytania" in r.text and "📝 *Do zapisania:*" in r.text
    assert [b[0] for b in r.buttons] == ["✅ Zapisz", "❌ Anuluj", "↩️ Cofnij listę"]
    assert core.store.book(book.id).status == "to_read"
    assert (await core.handle_callback(owner, r.buttons[2][1])).text == "↩️ Cofnięte."
    assert core.store.book(book.id).status == "to_buy"

    # zdjęcie, na którym główny parser nic nie znalazł → druga próba pytająca tylko o książki
    async def books_only(images, text):
        assert len(images) == 2 and text == ""
        return [BookAction(action="add", book_id=None, title="Passionate Marriage", author="David Schnarch",
                           status="to_buy")]
    core.parser.parse_books = books_only
    core.parser.next = ParseResult(records=[], amends=False, question="Co to jest?")
    r = await core.handle_message(owner, "", [(b"png", "image/png"), (b"png2", "image/png")])
    assert "Dodana: *Passionate Marriage — David Schnarch*" in r.text and "Co to jest" not in r.text, r.text
    core.store.delete_book(core.store.book_by_key("passionate marriage").id)

    # /zadania i przyciski: menu, przesunięcie, zrobione
    v = await core.tasks_list()
    print(v.text)
    assert "*📌 Dziś*" not in v.text and "*🗓 Najbliższe*" in v.text and v.buttons == [("Zadzwonić do ubezpieczyciela", f"tk:{task.id}")]
    menu = await core.handle_callback(owner, f"tk:{task.id}")
    assert [b[1] for b in menu.buttons] == [f"td:{task.id}", f"tp:{task.id}", f"tn:{task.id}", f"tx:{task.id}"]
    for _ in range(3):
        moved = await core.handle_callback(owner, f"tp:{task.id}")
    assert "trzecie przesunięcie" in moved.text and core.store.task(task.id).moves == 3
    assert "Zostało: 0" in (await core.handle_callback(owner, f"td:{task.id}")).text
    assert "Tego zadania już nie ma" in (await core.handle_callback(owner, f"td:{task.id}")).text
    assert "pusta" in (await core.tasks_list()).text

    # odhaczenie zwykłym zdaniem i cofnięcie przywraca zadanie
    t2 = core.store.add_task("Oddać książkę do biblioteki", None)
    core.parser.next = ParseResult(records=[], amends=False, question=None, tasks=[
        TaskAction(action="done", task_id=t2.id, title="Oddać książkę do biblioteki", due=None)])
    r = await core.handle_message(owner, "oddałem książkę", [])
    assert "Zrobione" in r.text and core.store.task(t2.id).status == "done"
    await core.handle_callback(owner, r.buttons[0][1])
    assert core.store.task(t2.id).status == "open"

    # /ksiazki i zmiana statusu przyciskiem
    v = await core.books_list()
    print(v.text, v.buttons)
    assert v.buttons == [("🛒 Atomowe nawyki", f"bk:{book.id}")]
    menu = await core.handle_callback(owner, f"bk:{book.id}")
    assert menu.buttons[0] == ("📚 Do przeczytania", f"bs:{book.id}.to_read"), menu.buttons
    assert "Gratulacje" in (await core.handle_callback(owner, f"bs:{book.id}.read")).text
    assert "Przeczytane (1)*: Atomowe nawyki" in (await core.books_list()).text

    # dziennik: wiadomości, przyciski i wywołania modelu z kosztem; /log
    log_text = (await core.event_log()).text
    print(log_text)
    kinds = [e["kind"] for e in core.store.recent_events(100)]
    assert kinds.count("message") == 5 and kinds.count("llm") == 5 and "button" in kinds
    msg = next(e for e in core.store.recent_events(100) if e["kind"] == "message")
    llm = next(e for e in core.store.recent_events(100) if e["kind"] == "llm" and e["trace"] == msg["trace"])
    assert msg["agent"] == "zadania" and llm["agent"] == "zadania" and llm["tokens_in"] == 3200
    assert abs(llm["cost_usd"] - (200 * 1 + 3000 * 0.1 + 80 * 5) / 1e6) < 1e-12
    assert "w tym miesiącu *$0.004*" in log_text and "🧠" in log_text

    # limit kosztu: jedno ostrzeżenie w miesiącu
    object.__setattr__(core.cfg, "llm_budget_usd", 0.001)
    assert core.budget_alert() is not None and core.budget_alert() is None
    print("\nOK listy")


asyncio.run(lists_test())


async def panel_test():
    """API panelu (/api/v1): zadania, książki, dziennik; zapisy tylko jako JSON."""
    import tempfile
    from pathlib import Path
    from aiohttp.test_utils import TestClient, TestServer
    from bot import web

    os.environ.update(STATE_FILE=str(Path(tempfile.mkdtemp()) / "state.json"))
    core = Core(Config())
    core.wallet, core.parser = FakeWallet(), FakeParser()
    async with TestClient(TestServer(web.build_app(core))) as c:
        r = await c.post("/api/v1/tasks", json={"title": "Wymienić opony", "due": "2026-10-25"})
        assert r.status == 201
        tid = (await r.json())["id"]
        r = await c.post("/api/v1/tasks", data="title=x", headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert r.status == 415, "zwykły formularz z obcej strony nie przejdzie"
        assert (await c.post("/api/v1/tasks", json={"title": "x", "due": "jutro"})).status == 400
        r = await c.patch(f"/api/v1/tasks/{tid}", json={"due": "2026-10-26"})
        assert (await r.json())["moves"] == 1
        await c.patch(f"/api/v1/tasks/{tid}", json={"status": "done"})
        t = await (await c.get("/api/v1/tasks")).json()
        assert t["open"] == [] and t["done"][0]["title"] == "Wymienić opony"

        r = await c.post("/api/v1/books", json={"title": "Diuna", "author": "Frank Herbert", "status": "to_buy"})
        bid = (await r.json())["id"]
        assert (await c.post("/api/v1/books", json={"title": "diuna", "status": "to_buy"})).status == 200, "duplikat"
        assert (await c.patch(f"/api/v1/books/{bid}", json={"status": "zle"})).status == 400
        await c.patch(f"/api/v1/books/{bid}", json={"status": "reading"})
        assert core.store.book(bid).status == "reading"
        assert (await c.delete(f"/api/v1/books/{bid}")).status == 200 and core.store.books() == []

        o = await (await c.get("/api/v1/overview")).json()
        assert o["tasks"]["done_today"] == 1 and o["month"]["expense"] == 50 and o["month"]["income"] == 100
        ev = (await (await c.get("/api/v1/events?kind=panel")).json())["events"]
        assert [e["name"] for e in ev][:2] == ["book_delete", "book_update"]
        days = (await (await c.get("/api/v1/stats/daily?days=7")).json())["days"]
        assert len(days) == 7 and days[-1]["date"] == core.today().isoformat()
        r = await c.get("/panel")
        assert r.status in (200, 503)  # 503, gdy front nie jest zbudowany (testy poza obrazem)
    print("\nOK panel")


asyncio.run(panel_test())


async def gcal_test():
    """Kalendarz Google: /google, zadania jako wydarzenia (cały dzień / godzina), kolizje, odczyt, panel."""
    import datetime as dt
    import json
    import re as _re
    import tempfile
    import time as _time
    from pathlib import Path
    from urllib.parse import parse_qs
    import httpx
    from aiohttp.test_utils import TestClient, TestServer
    from bot import web
    from bot.gcal import SCOPE
    from bot.parser import TaskAction

    os.environ.update(STATE_FILE=str(Path(tempfile.mkdtemp()) / "state.json"))

    class FakeGoogle:
        def __init__(self):
            self.cals: dict[str, dict[str, dict]] = {}  # kalendarz „Zadania” → wydarzenia
            self.cancelled: dict[str, dict] = {}  # usunięte (Google pokazuje je przy showDeleted)
            self.grants: list[str] = []
            self.n = 0

        def __call__(self, req: httpx.Request) -> httpx.Response:
            if req.url.host == "oauth2.googleapis.com":
                form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
                self.grants.append(form["grant_type"])
                if form["grant_type"] == "authorization_code":
                    assert form["code"] == "4/0Abc-def_ghi", form
                    return httpx.Response(200, json={"access_token": "a1", "refresh_token": "r1", "expires_in": 3600,
                                                     "scope": SCOPE})
                assert form["refresh_token"] == "r1"
                return httpx.Response(200, json={"access_token": "a2", "expires_in": 3600})
            assert req.headers["Authorization"].startswith("Bearer a")
            p, m = req.url.path.removeprefix("/calendar/v3"), req.method
            body = json.loads(req.content) if req.content else None
            if p == "/calendars" and m == "POST":
                self.n += 1
                cid = f"c{self.n}@group.calendar.google.com"
                self.cals[cid] = {}
                return httpx.Response(200, json={"id": cid, "summary": body["summary"]})
            if p == "/users/me/calendarList":
                return httpx.Response(200, json={"items": [
                    {"id": "primary@gmail.com", "summary": "Paweł", "selected": True},
                    {"id": "ukryty", "summary": "Ukryty"},
                    *({"id": c, "summary": "Zadania", "selected": True} for c in self.cals),
                ]})
            cal, eid = _re.fullmatch(r"/calendars/([^/]+)/events/?(.*)", p).groups()
            if cal == "primary@gmail.com":
                return httpx.Response(200, json={"items": [
                    {"summary": "Spotkanie z szefem", "start": {"dateTime": "2026-10-19T13:00:00Z"},
                     "end": {"dateTime": "2026-10-19T14:00:00Z"}},  # 15:00–16:00 w Warszawie
                    {"summary": "Urodziny Ani", "start": {"date": "2026-10-18"}, "end": {"date": "2026-10-19"}},
                    {"summary": "Odwołane", "status": "cancelled", "start": {"date": "2026-10-18"}, "end": {}},
                ]})
            if cal not in self.cals:
                return httpx.Response(404)
            events = self.cals[cal]
            if m == "GET" and not eid:
                assert req.url.params["showDeleted"] == "true"
                items = [{"id": k, **v} for k, v in events.items()]
                items += [{"id": k, "status": "cancelled"} for k in self.cancelled]
                return httpx.Response(200, json={"items": items})
            if m == "POST":
                self.n += 1
                events[f"e{self.n}"] = body
                return httpx.Response(200, json={"id": f"e{self.n}"})
            if eid not in events:
                return httpx.Response(410 if m == "DELETE" else 404)
            if m == "PUT":
                events[eid] = body
                return httpx.Response(200, json={"id": eid})
            if m == "DELETE":
                self.cancelled[eid] = events.pop(eid)
                return httpx.Response(204)
            raise AssertionError((m, p))

        def all(self) -> dict[str, dict]:
            return {k: v for c in self.cals.values() for k, v in c.items()}

    class TaskParser(FakeParser):
        next = None
        async def parse(self, *a, **kw):
            return self.next

    core = Core(Config())
    core.wallet, core.parser = FakeWallet(), TaskParser()
    core.today = lambda: dt.date(2026, 10, 18)
    g = core.gcal
    assert "console.cloud.google.com" in (await core.google_connect("")).text
    g.client_id, g.client_secret = "id.apps.googleusercontent.com", "sekret"
    fake = FakeGoogle()
    await g._http.aclose()
    g._http = httpx.AsyncClient(transport=httpx.MockTransport(fake))

    r = await core.google_connect("")
    url = r.buttons[0][1]
    assert "access_type=offline" in url and "prompt=consent" in url and "redirect_uri=http%3A%2F%2Flocalhost" in url
    assert "Nie widzę kodu" in (await core.google_connect("http://localhost/?error=access_denied")).text

    # zadanie sprzed połączenia trafia do kalendarza zaraz po /google; bez terminu — nie
    old = core.store.add_task("Wymienić opony", "2026-10-25")
    core.store.add_task("Kiedyś: nauczyć się żonglować", None)
    r = await core.google_connect("http://localhost/?code=4/0Abc-def_ghi&scope=https://www.googleapis.com/auth/calendar")
    assert "połączony" in r.text and "zadania z terminem: 1" in r.text, r.text
    await core.calendar_idle()
    old = core.store.task(old.id)
    ev = fake.all()[old.gcal_id]
    assert ev["start"] == {"date": "2026-10-25"} and ev["end"] == {"date": "2026-10-26"} and ev["summary"] == "Wymienić opony"
    assert len(fake.cals) == 1 and len(fake.all()) == 1

    # „jutro o 15 zadzwonić do ZUS”: wydarzenie z godziną i ostrzeżenie o spotkaniu o tej porze
    core.parser.next = ParseResult(records=[], amends=False, question=None, tasks=[
        TaskAction(action="add", task_id=None, title="Zadzwonić do ZUS", due="2026-10-19", time="15")])
    r = await core.handle_message("tg:1", "jutro o 15 zadzwonić do ZUS", [])
    print(r.text)
    assert "(jutro o 15:00)" in r.text and "⚠️ O 15:00 masz już: Spotkanie z szefem" in r.text
    await core.calendar_idle()
    zus = core.store.task(core.store.open_tasks()[0].id)
    assert zus.title == "Zadzwonić do ZUS" and zus.due_time == "15:00"
    ev = fake.all()[zus.gcal_id]
    assert ev["start"] == {"dateTime": "2026-10-19T15:00:00", "timeZone": "Europe/Warsaw"}
    assert ev["end"]["dateTime"] == "2026-10-19T15:30:00" and ev["transparency"] == "opaque"

    # przesunięcie przyciskiem zachowuje godzinę; zrobione → „✅” w kalendarzu (zostaje jako ślad)
    await core.handle_callback("tg:1", f"tp:{zus.id}")
    await core.calendar_idle()
    assert fake.all()[zus.gcal_id]["start"]["dateTime"] == "2026-10-19T15:00:00"
    await core.handle_callback("tg:1", f"td:{zus.id}")
    await core.calendar_idle()
    assert fake.all()[zus.gcal_id]["summary"] == "✅ Zadzwonić do ZUS"

    # dodane i od razu cofnięte — w kalendarzu nic nie zostaje
    core.parser.next = ParseResult(records=[], amends=False, question=None, tasks=[
        TaskAction(action="add", task_id=None, title="Kupić mleko", due="2026-10-18")])
    r = await core.handle_message("tg:1", "dziś kupić mleko", [])
    await core.handle_callback("tg:1", r.buttons[0][1])
    await core.calendar_idle()
    assert not any(e["summary"] == "Kupić mleko" for e in fake.all().values())

    # /zadania i /kalendarz pokazują Twoje wydarzenia (bez odwołanych, bez kalendarza „Zadania”)
    v = (await core.tasks_list()).text
    print(v)
    assert "📅 W kalendarzu dziś" in v and "📅 cały dzień Urodziny Ani" in v and "Odwołane" not in v
    assert "Spotkanie z szefem" not in v, "jutrzejsze tylko w /kalendarz"
    k = (await core.calendar_report()).text
    print(k)
    assert "📅 15:00–16:00 Spotkanie z szefem" in k and "🗒 cały dzień Wymienić opony" not in k

    # usunięcie zadania usuwa wydarzenie; wygasły token odświeża się sam
    st = g.state; st["expires_at"] = _time.time() - 10; g._save(st)
    await core.handle_callback("tg:1", f"txy:{old.id}")
    await core.calendar_idle()
    assert old.gcal_id not in fake.all() and fake.grants[-1] == "refresh_token"

    # kalendarz „Zadania” usunięty ręcznie w Google → bot zakłada nowy
    fake.cals.clear()
    t = core.store.add_task("Dentysta", "2026-10-20", "09:30")
    core.calendar_sync(t.id)
    await core.calendar_idle()
    assert len(fake.cals) == 1 and core.store.task(t.id).gcal_id in fake.all()

    # zmiany zrobione w samym Google wracają do listy: przeniesione, przemianowane, usunięte
    dino = core.store.add_task("Zakupy w Dino", "2026-10-19")
    core.calendar_sync(dino.id)
    await core.calendar_idle()
    dino, t = core.store.task(dino.id), core.store.task(t.id)
    cal = next(iter(fake.cals))
    fake.cals[cal][t.gcal_id].update(summary="Dentysta — kontrola", start={"dateTime": "2026-10-22T09:00:00Z"})
    fake.cancelled[dino.gcal_id] = fake.cals[cal].pop(dino.gcal_id)
    pulled = await core.calendar_pull()
    print(pulled)
    assert pulled == ["📅 Zmienione w kalendarzu: Dentysta — kontrola", "🗑 Usunięte w kalendarzu: Zakupy w Dino"]
    t = core.store.task(t.id)
    assert (t.title, t.due, t.due_time) == ("Dentysta — kontrola", "2026-10-22", "11:00") and core.store.task(dino.id) is None
    assert await core.calendar_pull() == [], "drugi raz nic nowego (te same dane)"
    assert g.state["pulled_at"]

    # panel: wydarzenia na 7 dni, godzina przy dodawaniu i zmianie
    async with TestClient(TestServer(web.build_app(core))) as c:
        d = await (await c.get("/api/v1/tasks")).json()
        assert d["google"] is True and [e["title"] for e in d["calendar"]] == ["Urodziny Ani", "Spotkanie z szefem"]
        assert next(x for x in d["open"] if x["id"] == t.id)["due_label"] == "czw 22.10 o 11:00"
        assert (await c.post("/api/v1/tasks", json={"title": "x", "due": "2026-10-20", "due_time": "25:00"})).status == 400
        r = await c.post("/api/v1/tasks", json={"title": "Fryzjer", "due": "2026-10-21", "due_time": "17:00"})
        fid = (await r.json())["id"]
        await c.patch(f"/api/v1/tasks/{t.id}", json={"due_time": "10:00"})
        await core.calendar_idle()
        assert core.store.task(t.id).moves == 0, "inna godzina tego dnia to nie przesunięcie"
        assert fake.all()[core.store.task(t.id).gcal_id]["start"]["dateTime"] == "2026-10-22T10:00:00"
        assert fake.all()[core.store.task(fid).gcal_id]["start"]["dateTime"] == "2026-10-21T17:00:00"
        await c.delete(f"/api/v1/tasks/{fid}")
        await core.calendar_idle()
        assert not any(e["summary"] == "Fryzjer" for e in fake.all().values())
    assert not [e for e in core.store.recent_events(200) if e["kind"] == "error"]
    await core.close()
    print("\nOK kalendarz")


asyncio.run(gcal_test())
