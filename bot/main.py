import asyncio
import logging
import signal

import httpx

from dotenv import load_dotenv

load_dotenv()

from .config import Config  # noqa: E402
from .core import Core  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("wallet-bot")

CHECK_EVERY = 300  # s


async def reminder_loop(core: Core, notifiers: list) -> None:
    """Raz na kilka minut: po godzinie REMINDER_HOUR wysyła przypomnienia o terminach
    płatności (każdy termin najwyżej raz dziennie — stan w pliku, więc restart nie dubluje)."""
    from datetime import datetime

    while True:
        try:
            if datetime.now(core.cfg.tz).hour >= core.cfg.reminder_hour:
                for reply in await core.due_reminders():
                    for notify in notifiers:
                        await notify(reply)
        except Exception:
            log.exception("Błąd pętli przypomnień")
        await asyncio.sleep(CHECK_EVERY)


async def run() -> None:
    cfg = Config()
    if not cfg.telegram_token:
        raise SystemExit("Ustaw TELEGRAM_BOT_TOKEN")
    core = Core(cfg)
    try:
        await core.refresh_catalog(force=True)  # wcześnie wykrywa zły token Wallet (WalletError = koniec)
    except httpx.HTTPError as e:  # chwilowy brak sieci — katalog dociągnie się przy pierwszej wiadomości
        log.warning("Wallet API niedostępne przy starcie: %r", e)
    if core.stt:
        stt_preload = asyncio.create_task(core.stt.preload())  # w tle; boty startują od razu  # noqa: F841

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    from . import telegram_bot

    if not cfg.telegram_allowed:
        log.warning("TELEGRAM_ALLOWED_USERS puste — bot odpowie tylko na /whoami")
    tg_app = await telegram_bot.start(core)
    notifiers = [telegram_bot.notifier(tg_app, core)]
    reminders = asyncio.create_task(reminder_loop(core, notifiers))

    web_runner = None
    if cfg.web_port:
        from . import web

        try:
            web_runner = await web.start(core, cfg.web_port)
        except OSError as e:
            log.warning("Strona prognozy nie wystartowała: %s", e)

    mail_task = None
    if cfg.mail_user and cfg.mail_password:
        from .statement import MailWatcher

        if not cfg.mail_allowed_from:
            log.warning("MAIL_ALLOWED_FROM puste — maile będą tylko pokazywane, nie przetwarzane")

        async def notify_all(reply) -> None:
            for notify in notifiers:
                await notify(reply)

        mail_task = asyncio.create_task(MailWatcher(core, notify_all).loop())

    try:
        await stop.wait()
    finally:
        log.info("Zamykanie…")
        for task in (reminders, mail_task):
            if task:
                task.cancel()
        if web_runner:
            await web_runner.cleanup()
        await telegram_bot.stop(tg_app)
        await core.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
