"""Test przepływu bez sieci: atrapy Wallet API i Claude. Uruchom: python -m tests.test_offline"""
import asyncio
import inspect
import os

os.environ.setdefault("WALLET_API_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:abc")

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


class FakeParser:
    async def parse(self, text, images, today, catalog_prompt):
        assert "Ogólne" in catalog_prompt and "Groceries" in catalog_prompt
        return ParseResult(question=None, records=[
            ParsedRecord(amount=-54.3, type="expense", category_id="food", account_id="pln", date=today.isoformat(), counterparty="Biedronka", note=None),
            ParsedRecord(amount=20, type="expense", category_id="nope", account_id="bad", date="2999-01-01", counterparty=None, note="x"),
        ])


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
    undo = await core.handle_callback("tg:1", saved.buttons[0][1])
    print(undo.text); assert core.wallet.deleted == ["r0", "r1"]
    print((await core.balances()).text)
    print((await core.month_summary()).text)

    # adaptery się budują
    from bot import telegram_bot, discord_bot
    telegram_bot.build(core); discord_bot.build(core)
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

        print((await core.handle_callback("tg:1", f"sa:{keys['krecha']}")).text)
        r = await core.handle_message("tg:1", "950,50", [])
        print(r.text)
        assert core.wallet.created[-1]["amount"]["value"] == -950.5

        print((await core.handle_callback("tg:1", f"ss:{keys['multisport']}")).text)
        reloaded = Planned(core)  # stan z pliku
        assert reloaded.state.handled(keys["multisport"])["status"] == "skipped"
        assert reloaded.state.handled(keys["krecha"])["status"] == "paid"

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
