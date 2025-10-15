"""Health endpoint handler."""

from __future__ import annotations

from aiohttp import web


async def handle_health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


__all__ = ["handle_health"]
