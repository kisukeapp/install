"""Handlers for toggling proxy request logging."""

from __future__ import annotations

import json
from aiohttp import web

from .. import logging_control


async def handle_get_logging(request: web.Request) -> web.Response:
    """Return the current logging state for clients that expect CLIProxyAPI parity."""

    return web.json_response({"enabled": logging_control.is_enabled()})


async def handle_set_logging(request: web.Request) -> web.Response:
    """Update the logging toggle based on a JSON payload."""

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)
    enabled = bool(body.get("enabled"))
    logging_control.set_enabled(enabled)
    return web.json_response({"enabled": logging_control.is_enabled()})


__all__ = ["handle_get_logging", "handle_set_logging"]
