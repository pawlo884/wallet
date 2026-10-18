"""Mały serwer WWW w kontenerze: prognoza jako strona (/) i JSON (/api/prognoza),
panel asystenta w React (/panel) z API (/api/v1, bot/panel_api.py).

Bez własnego logowania — wystawiany tylko w sieci Nginx Proxy Managera, a dostęp
(LAN / Tailscale / hasło) ustawia się w NPM listą dostępu, tak jak dla innych paneli.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from . import forecast, panel_api

if TYPE_CHECKING:
    from .core import Core

log = logging.getLogger(__name__)
TEMPLATE = Path(__file__).with_name("templates") / "prognoza.html"
# Zbudowany front (npm run build w frontend/); w obrazie Dockera kopiowany tutaj w pierwszym etapie.
PANEL_DIR = Path(__file__).with_name("static") / "panel"


def build_app(core: "Core") -> web.Application:
    async def page(_: web.Request) -> web.Response:
        data = await forecast.build(core)
        html = forecast.render_html(data, TEMPLATE.read_text(encoding="utf-8"))
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def api(_: web.Request) -> web.Response:
        return web.json_response(await forecast.build(core), dumps=lambda o: __import__("json").dumps(o, ensure_ascii=False))

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def panel(_: web.Request) -> web.StreamResponse:
        index = PANEL_DIR / "index.html"
        if not index.exists():
            return web.Response(status=503, text="Panel nie jest zbudowany (cd frontend && npm run build).")
        return web.FileResponse(index, headers={"Cache-Control": "no-store"})

    async def panel_redirect(_: web.Request) -> web.Response:
        raise web.HTTPFound("/panel/")

    app = web.Application()
    app.add_routes([web.get("/", page), web.get("/api/prognoza", api), web.get("/health", health)])
    app.add_routes(panel_api.routes(core))
    app.add_routes([web.get("/panel", panel_redirect), web.get("/panel/", panel)])
    if (PANEL_DIR / "assets").is_dir():  # pliki z hashem w nazwie — mogą być cache'owane
        app.router.add_static("/panel/assets", PANEL_DIR / "assets")
    return app


async def start(core: "Core", port: int) -> web.AppRunner:
    runner = web.AppRunner(build_app(core), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Prognoza WWW na porcie %d", port)
    return runner
