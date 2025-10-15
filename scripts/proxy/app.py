"""Application bootstrap helpers."""

from __future__ import annotations

from aiohttp import web

from .handlers.health import handle_health
from .handlers.models import handle_models
from .handlers.keepalive import handle_keep_alive
from .handlers.logging import handle_get_logging, handle_set_logging
from .handlers.messages import handle_messages


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/keep-alive", handle_keep_alive)
    app.router.add_get("/logging", handle_get_logging)
    app.router.add_post("/logging", handle_set_logging)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_post("/v1/messages", handle_messages)
    return app


async def start_proxy(host: str = "127.0.0.1", port: int = 8082):
    app = make_app()
    # Disable default access logger to avoid redundant Apache-style logs
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner


__all__ = ["make_app", "start_proxy"]
