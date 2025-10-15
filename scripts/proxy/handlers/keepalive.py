"""HTTP handler for the /keep-alive endpoint."""

from __future__ import annotations

from aiohttp import web


async def handle_keep_alive(request: web.Request) -> web.Response:
    """Mirror CLIProxyAPI's keep-alive endpoint with a lightweight response."""

    return web.json_response({"status": "ok"})


__all__ = ["handle_keep_alive"]
