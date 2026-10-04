"""Mały serwer WWW w kontenerze: prognoza jako strona (/) i JSON (/api/prognoza).

Bez własnego logowania — wystawiany tylko w sieci Nginx Proxy Managera, a dostęp
(LAN / Tailscale / hasło) ustawia się w NPM listą dostępu, tak jak dla innych paneli.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from . import forecast

if TYPE_CHECKING:
    from .core import Core

log = logging.getLogger(__name__)
TEMPLATE = Path(__file__).with_name("templates") / "prognoza.html"


def build_app(core: "Core") -> web.Application:
    async def page(_: web.Request) -> web.Response:
        data = await forecast.build(core)
        html = forecast.render_html(data, TEMPLATE.read_text(encoding="utf-8"))
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def api(_: web.Request) -> web.Response:
        return web.json_response(await forecast.build(core), dumps=lambda o: __import__("json").dumps(o, ensure_ascii=False))

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.add_routes([web.get("/", page), web.get("/api/prognoza", api), web.get("/health", health)])
    return app


async def start(core: "Core", port: int) -> web.AppRunner:
    runner = web.AppRunner(build_app(core), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Prognoza WWW na porcie %d", port)
    return runner
