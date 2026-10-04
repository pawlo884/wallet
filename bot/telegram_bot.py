import asyncio
import logging

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .core import HELP, Core, Reply

log = logging.getLogger(__name__)


# Lista komend widoczna w Telegramie (kolejność = kolejność w menu).
COMMANDS = [
    ("saldo", "Salda kont"),
    ("miesiac", "Podsumowanie bieżącego miesiąca"),
    ("prognoza", "Prognoza na 12 miesięcy"),
    ("zaplanowane", "Płatności do potwierdzenia i najbliższe 30 dni"),
    ("plany", "Lista płatności cyklicznych (usuwanie)"),
    ("plan", "Nowa płatność cykliczna, np. /plan netflix 49 15-go"),
    ("dlugi", "Długi i ile zostało do spłaty"),
    ("dlug", "Nowy dług, np. /dlug A6 9100"),
    ("inwestycje", "Wycena inwestycji (srebro, złoto, ETF)"),
    ("inwestycja", "Dodaj inwestycję, np. /inwestycja srebro 2 uncje"),
    ("multisport", "Czy karta Multisport się opłaca (Strava)"),
    ("strava", "Połącz konto Strava"),
    ("kurs", "Kursy NBP, np. /kurs 100 eur"),
    ("wyciag", "Uzgodnij wyciąg z banku (wklej treść)"),
    ("korekta", "Popraw saldo do stanu z banku, np. /korekta 2345,67"),
    ("odswiez", "Odśwież konta i kategorie"),
    ("pomoc", "Jak korzystać z bota"),
]


def _markup(reply: Reply) -> InlineKeyboardMarkup | None:
    if not reply.buttons:
        return None
    buttons = [InlineKeyboardButton(t, callback_data=d) for t, d in reply.buttons]
    return InlineKeyboardMarkup([[b] for b in buttons] if reply.column else [buttons])


async def _send(bot, chat_id: int, reply: Reply) -> None:
    try:
        await bot.send_message(chat_id, reply.text, parse_mode=ParseMode.MARKDOWN, reply_markup=_markup(reply))
    except Exception:  # np. znak psujący Markdown w nazwie sklepu
        await bot.send_message(chat_id, reply.text, reply_markup=_markup(reply))


def build(core: Core) -> Application:
    allowed = core.cfg.telegram_allowed
    app = Application.builder().token(core.cfg.telegram_token).build()
    # Pusta lista = nikt (poza /whoami). Bot ma dostęp do Twoich finansów — bez wyjątków.
    user_filter = filters.User(user_id=allowed)

    async def send(update: Update, replies: Reply | list[Reply]) -> None:
        for reply in replies if isinstance(replies, list) else [replies]:
            await _send(update.get_bot(), update.effective_chat.id, reply)

    async def on_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await send(update, Reply(HELP + f"\n\nTwoje Telegram ID: `{update.effective_user.id}`"))

    async def on_whoami(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        # Działa także dla osób spoza listy — żeby dało się odczytać ID do konfiguracji.
        await update.effective_message.reply_text(f"Twoje Telegram ID: {update.effective_user.id}")

    async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        await ctx.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
        images = []
        if msg.photo:
            f = await msg.photo[-1].get_file()
            images.append((bytes(await f.download_as_bytearray()), "image/jpeg"))
        elif msg.document and (msg.document.mime_type or "").startswith("image/"):
            f = await msg.document.get_file()
            images.append((bytes(await f.download_as_bytearray()), msg.document.mime_type))
        text = msg.text or msg.caption or ""
        try:
            reply = await core.handle_message(f"tg:{update.effective_user.id}", text, images)
        except Exception:
            log.exception("Błąd obsługi wiadomości")
            reply = Reply("⚠️ Coś poszło nie tak. Spróbuj ponownie za chwilę.")
        await send(update, reply)

    async def on_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if update.effective_user.id not in allowed:
            await q.answer("Brak dostępu")
            return
        await q.answer()
        try:
            reply = await core.handle_callback(f"tg:{update.effective_user.id}", q.data)
        except Exception:
            log.exception("Błąd obsługi przycisku")
            reply = Reply("⚠️ Coś poszło nie tak. Spróbuj ponownie za chwilę.")
        await q.edit_message_reply_markup(None)
        await send(update, reply)

    def cmd(fn):
        async def handler(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
            await send(update, await fn())
        return handler

    app.add_handler(CommandHandler("whoami", on_whoami))
    app.add_handler(CommandHandler(["start", "pomoc", "help"], on_start, filters=user_filter))
    app.add_handler(CommandHandler("saldo", cmd(core.balances), filters=user_filter))
    app.add_handler(CommandHandler("miesiac", cmd(core.month_summary), filters=user_filter))
    app.add_handler(CommandHandler("zaplanowane", cmd(core.planned_overview), filters=user_filter))
    app.add_handler(CommandHandler("plany", cmd(core.plans_list), filters=user_filter))

    async def on_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        try:
            reply = await core.plan_add(f"tg:{update.effective_user.id}", " ".join(ctx.args or []))
        except Exception:
            log.exception("Błąd /plan")
            reply = Reply("⚠️ Coś poszło nie tak. Spróbuj ponownie za chwilę.")
        await send(update, reply)

    app.add_handler(CommandHandler("plan", on_plan, filters=user_filter))

    async def on_kurs(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await send(update, await core.fx_quote(" ".join(ctx.args or [])))

    app.add_handler(CommandHandler("kurs", on_kurs, filters=user_filter))

    async def on_korekta(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await send(update, await core.balance_fix(f"tg:{update.effective_user.id}", " ".join(ctx.args or [])))

    app.add_handler(CommandHandler("korekta", on_korekta, filters=user_filter))

    async def on_inwestycja(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        await send(update, await core.investment_add(f"tg:{update.effective_user.id}", " ".join(ctx.args or [])))

    app.add_handler(CommandHandler("inwestycja", on_inwestycja, filters=user_filter))
    app.add_handler(CommandHandler("inwestycje", cmd(core.investments_list), filters=user_filter))
    app.add_handler(CommandHandler("multisport", cmd(core.multisport_report), filters=user_filter))

    async def on_strava(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await send(update, await core.strava_connect(" ".join(ctx.args or [])))

    app.add_handler(CommandHandler("strava", on_strava, filters=user_filter))

    async def on_dlug(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await send(update, await core.debt_add(" ".join(ctx.args or [])))

    app.add_handler(CommandHandler("dlug", on_dlug, filters=user_filter))
    app.add_handler(CommandHandler("dlugi", cmd(core.debts_list), filters=user_filter))
    app.add_handler(CommandHandler("prognoza", cmd(core.forecast), filters=user_filter))

    async def on_wyciag(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        await ctx.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
        text = (msg.text or "").split(None, 1)[1] if len((msg.text or "").split(None, 1)) > 1 else ""
        await send(update, await core.statement(f"tg:{update.effective_user.id}", text))

    async def on_statement_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        await ctx.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
        f = await msg.document.get_file()
        try:
            replies = await core.statement_file(f"tg:{update.effective_user.id}", bytes(await f.download_as_bytearray()))
        except Exception:
            log.exception("Błąd wyciągu z pliku")
            replies = Reply("⚠️ Nie udało się przetworzyć pliku.")
        await send(update, replies)

    app.add_handler(CommandHandler("wyciag", on_wyciag, filters=user_filter))
    app.add_handler(
        MessageHandler(
            user_filter
            & (
                filters.Document.FileExtension("eml")
                | filters.Document.MimeType("message/rfc822")
                | filters.Document.MimeType("text/plain")
            ),
            on_statement_file,
        )
    )
    app.add_handler(CommandHandler("odswiez", cmd(core.refresh), filters=user_filter))
    app.add_handler(
        MessageHandler(
            user_filter & (filters.TEXT | filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND,
            on_message,
        )
    )

    async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        await ctx.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
        f = await (msg.voice or msg.audio).get_file()
        try:
            reply = await core.handle_voice(f"tg:{update.effective_user.id}", bytes(await f.download_as_bytearray()))
        except Exception:
            log.exception("Błąd obsługi głosówki")
            reply = Reply("⚠️ Coś poszło nie tak. Spróbuj ponownie za chwilę.")
        await send(update, reply)

    app.add_handler(MessageHandler(user_filter & (filters.VOICE | filters.AUDIO), on_voice))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(ok|no|undo|sp|sa|ss|pk|pa|pn|rm|rmy|keep|dl|dly|kr|ki|kn|ia|in|ir|iry):"))
    return app


def notifier(app: Application, core: Core):
    """Wysyła wiadomość z inicjatywy bota do każdej dozwolonej osoby (czat prywatny = ID użytkownika)."""

    async def notify(reply: Reply) -> None:
        for uid in core.cfg.telegram_allowed:
            try:
                await _send(app.bot, uid, reply)
            except Exception:
                log.exception("Telegram: nie udało się wysłać do %s (czy napisałeś do bota /start?)", uid)

    return notify


async def start(core: Core) -> Application:
    app = build(core)
    # Chwilowy brak sieci przy starcie nie powinien wywracać kontenera — ponawiamy z rosnącą przerwą.
    for attempt in range(1, 9):
        try:
            await app.initialize()
            break
        except (TimedOut, NetworkError) as e:
            if attempt == 8:
                raise
            log.warning("Telegram niedostępny przy starcie (%s), próba %d/8", e, attempt)
            await asyncio.sleep(min(5 * attempt, 30))
    await app.start()
    try:  # podpowiedzi po wpisaniu „/” i przycisk Menu — zawsze zgodne z tym, co bot obsługuje
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
    except (TimedOut, NetworkError) as e:
        log.warning("Nie ustawiono listy komend: %s", e)
    await app.updater.start_polling(drop_pending_updates=False)
    log.info("Telegram bot działa")
    return app


async def stop(app: Application) -> None:
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
