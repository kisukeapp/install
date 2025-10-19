"""Models endpoint handler."""

from __future__ import annotations

from aiohttp import web
from ..registry import list_routes


async def handle_models(_request: web.Request) -> web.Response:
    routes = list_routes()
    if not routes:
        return web.json_response({"data": []})
    data = [{"id": r.model, "type": "model"} for r in routes if getattr(r, "model", None)]
    # Fallback if models missing
    if not data:
        data = [{"id": routes[0].model or "", "type": "model"}]
    return web.json_response({"data": data})


__all__ = ["handle_models"]
