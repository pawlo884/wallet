"""Wyciągi z banku: tekst (wklejony, plik .eml albo mail na skrzynkę bota) → uzgodnienie z Wallet.

Każdą operację z wyciągu porównujemy z rekordami w Wallet (kwota + data ±3 dni; przy wpisach
przeliczonych kursem NBP tolerancja kilku procent, bo bank liczy po swoim kursie) i z
niepotwierdzonymi płatnościami cyklicznymi. Brakujące przychodzą jako szkice do zatwierdzenia.
"""

import asyncio
import email
import email.policy
import html
import imaplib
import logging
import re
import secrets
from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .core import Core, Reply

log = logging.getLogger(__name__)

MATCH_DAYS = 3  # data księgowania bywa kilka dni po dacie zakupu
FX_TOLERANCE = 0.04  # wpisy przeliczone kursem NBP vs kurs banku
_AMOUNT_RE = re.compile(r"-?\d[\d  ]*[.,]\d{2}\b")


def looks_like_statement(text: str) -> bool:
    """Długi tekst z wieloma kwotami — prawie na pewno wklejony wyciąg, a nie pojedynczy wydatek."""
    return len(text) > 300 and len(_AMOUNT_RE.findall(text)) >= 3


def email_to_text(raw: bytes) -> tuple[str, str, str]:
    """(nadawca, temat, treść jako czysty tekst) z surowego maila (.eml / IMAP)."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body = msg.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body else ""
    if body is not None and body.get_content_type() == "text/html":
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", text)
        text = re.sub(r"(?i)</t[dh]>", " | ", text)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return str(msg.get("From", "")), str(msg.get("Subject", "")), text


class Reconciler:
    def __init__(self, core: "Core"):
        self.core = core

    async def reconcile(self, owner: str, text: str, source: str = "wyciągu") -> list["Reply"]:
        from .core import Reply, _Draft, fmt_money
        from .planned import occ_key

        core = self.core
        await core.refresh_catalog()
        result = await core.parser.parse_statement(
            text, core.today(), core._catalog_prompt, memory=core.memory.prompt_block()
        )
        ops = [core._sanitize(r) for r in result.records]
        if not ops:
            return [Reply(f"📄 Nie znalazłem żadnych operacji (źródło: {source})." +
                          (f"\n❓ {result.question}" if result.question else ""))]
        for r in ops:
            if not (r.note or "").strip():
                r.note = r.counterparty or "operacja z wyciągu"

        dates = [date.fromisoformat(r.date) for r in ops]
        lo, hi = min(dates) - timedelta(days=MATCH_DAYS + 1), max(dates) + timedelta(days=MATCH_DAYS + 1)
        # Tylko aktywne konta — zarchiwizowane (stara historia) nie mogą „zaliczać” operacji z wyciągu.
        active = ",".join(list(core._accounts)[:10])
        existing = await core.wallet.records(lo.isoformat(), hi.isoformat(), accountId=active) if active else []
        used: set[str] = set()

        def signed(r) -> float:
            return -r.amount if r.type == "expense" else r.amount

        def match_wallet(r) -> dict | None:
            d, v = date.fromisoformat(r.date), signed(r)
            best = None
            for w in existing:
                if w["id"] in used:
                    continue
                wv = (w.get("amount") or {}).get("value", 0)
                wd = date.fromisoformat(w["recordDate"][:10])
                tol = abs(v) * FX_TOLERANCE if "(NBP" in (w.get("note") or "") else 0.011
                if (wv < 0) == (v < 0) and abs(wv - v) <= tol and abs((wd - d).days) <= MATCH_DAYS:
                    gap = abs((wd - d).days)
                    if best is None or gap < best[0]:
                        best = (gap, w)
            if best:
                used.add(best[1]["id"])
                return best[1]
            return None

        # Niepotwierdzone płatności cykliczne w okolicy dat wyciągu.
        planned_open = [
            (p, d)
            for p in core.planned.schedule.payments
            for d in p.occurrences(lo, hi)
            if not core.planned.state.handled(occ_key(p, d))
        ]

        def match_planned(r):
            d, v = date.fromisoformat(r.date), signed(r)
            for p, pd in planned_open:
                if abs(p.signed() - v) <= 0.011 and abs((pd - d).days) <= MATCH_DAYS:
                    planned_open.remove((p, pd))
                    return p, pd
            return None

        matched, planned_hits, missing = [], [], []
        for r in ops:
            if w := match_wallet(r):
                matched.append((r, w))
            elif hit := match_planned(r):
                planned_hits.append((r, *hit))
            else:
                missing.append(r)

        cur = core.cfg.base_currency
        lines = [
            f"📄 *Operacje z {source}: {len(ops)}* ({min(dates):%d.%m}–{max(dates):%d.%m})",
            f"✅ już w Wallet: {len(matched)}",
        ]
        if planned_hits:
            lines.append(f"📅 pasuje do płatności cyklicznych: {len(planned_hits)}")
        lines.append(f"➕ brakuje: {len(missing)}" + (" — szkice poniżej" if missing else " — wszystko się zgadza 👌"))
        if matched:
            lines.append("\n*Zgodne:*")
            lines += [
                f"• {date.fromisoformat(r.date):%d.%m} {fmt_money(signed(r), cur)} ↔ "
                f"{(w.get('counterParty') or w.get('note') or (w.get('category') or {}).get('name', ''))[:40]}"
                for r, w in matched[:20]  # długa lista zgodnych operacji nic nie wnosi
            ]
            if len(matched) > 20:
                lines.append(f"• …i {len(matched) - 20} więcej")
        replies = [Reply("\n".join(lines))]

        today = core.today()
        for r, p, pd in planned_hits:
            rem = core.planned.reminder(p, pd, today)
            rem.text = f"📄 Z wyciągu: {fmt_money(signed(r), cur)} ({date.fromisoformat(r.date):%d.%m})\n" + rem.text
            replies.append(rem)

        for r in missing:  # osobny szkic dla każdej operacji — zatwierdzasz wybiórczo
            key = secrets.token_urlsafe(6)
            core._drafts[key] = _Draft(owner, [r])
            replies.append(
                Reply(
                    "➕ *Brak w Wallet:*\n" + core._describe(r),
                    [("✅ Zapisz", f"ok:{key}"), ("❌ Pomiń", f"no:{key}")],
                )
            )
        return replies


class MailWatcher:
    """Skrzynka bota przez IMAP: nowe maile od dozwolonych nadawców → uzgodnienie wyciągu."""

    def __init__(self, core: "Core", notify):
        self.core = core
        self.cfg = core.cfg
        self.notify = notify  # async (Reply) -> None, do wszystkich dozwolonych osób

    def _fetch_unseen(self) -> list[bytes]:
        box = imaplib.IMAP4_SSL(self.cfg.mail_imap_host, timeout=60)
        try:
            box.login(self.cfg.mail_user, self.cfg.mail_password)
            box.select(self.cfg.mail_folder)
            _, data = box.search(None, "UNSEEN")
            out = []
            for num in data[0].split():
                _, parts = box.fetch(num, "(RFC822)")  # pobranie oznacza mail jako przeczytany
                out += [p[1] for p in parts if isinstance(p, tuple)]
            return out
        finally:
            try:
                box.logout()
            except Exception:
                pass

    def _allowed(self, sender: str) -> bool:
        addr = (re.search(r"[\w.+-]+@[\w.-]+", sender) or [""])[0].lower()
        return any(addr == a or addr.endswith("@" + a) or addr.endswith("." + a) for a in self.cfg.mail_allowed_from)

    async def check(self) -> None:
        from .core import Reply

        for raw in await asyncio.to_thread(self._fetch_unseen):
            sender, subject, text = email_to_text(raw)
            if not self._allowed(sender):
                # Np. kod weryfikacyjny przekierowania z Gmaila — pokazujemy, ale nie przetwarzamy.
                await self.notify(Reply(f"📧 Mail od {sender}\n*{subject}*\n\n{text[:600]}\n\n_(nadawca spoza MAIL_ALLOWED_FROM — pominięty)_"))
                continue
            log.info("Wyciąg z maila: %s / %s", sender, subject)
            try:
                replies = await self.core.reconciler.reconcile("*", text, source=f"maila „{subject[:40]}”")
            except Exception:
                log.exception("Błąd uzgadniania wyciągu z maila")
                replies = [Reply(f"⚠️ Nie udało się przetworzyć maila „{subject}”.")]
            for reply in replies:
                await self.notify(reply)

    async def loop(self) -> None:
        log.info("Skrzynka %s: sprawdzanie co %d min", self.cfg.mail_user, self.cfg.mail_poll_minutes)
        while True:
            try:
                await self.check()
            except Exception as e:
                log.warning("IMAP: %r", e)
            await asyncio.sleep(self.cfg.mail_poll_minutes * 60)
