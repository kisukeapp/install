"""Models endpoint handler."""

from __future__ import annotations

from aiohttp import web


async def handle_models(_request: web.Request) -> web.Response:
    return web.json_response({"data": [{"id": "claude-3-5-sonnet-latest", "type": "model"}]})


__all__ = ["handle_models"]
