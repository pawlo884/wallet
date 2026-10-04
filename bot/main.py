import asyncio
import logging
import signal

from dotenv import load_dotenv

load_dotenv()

from .config import Config  # noqa: E402
from .core import Core  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("wallet-bot")


async def run() -> None:
    cfg = Config()
    if not cfg.telegram_token and not cfg.discord_token:
        raise SystemExit("Ustaw TELEGRAM_BOT_TOKEN i/lub DISCORD_BOT_TOKEN")
    core = Core(cfg)
    await core.refresh_catalog(force=True)  # wcześnie wykrywa zły token Wallet

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    tg_app = None
    dc_task = None
    if cfg.telegram_token:
        from . import telegram_bot

        if not cfg.telegram_allowed:
            log.warning("TELEGRAM_ALLOWED_USERS puste — bot odpowie tylko na /whoami")
        tg_app = await telegram_bot.start(core)
    if cfg.discord_token:
        from . import discord_bot

        if not cfg.discord_allowed:
            log.warning("DISCORD_ALLOWED_USERS puste — bot odpowie tylko na !whoami")
        dc_client = discord_bot.build(core)
        dc_task = asyncio.create_task(dc_client.start(cfg.discord_token))

        def on_discord_exit(t: asyncio.Task) -> None:
            if not t.cancelled() and t.exception():
                log.error("Discord bot padł: %r", t.exception())
                stop.set()  # restart kontenera przez Dockera

        dc_task.add_done_callback(on_discord_exit)

    try:
        await stop.wait()
    finally:
        log.info("Zamykanie…")
        if tg_app:
            await telegram_bot.stop(tg_app)
        if dc_task:
            await dc_client.close()
        await core.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
