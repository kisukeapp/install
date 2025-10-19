"""/v1/messages/count_tokens handler that dispatches to provider token counters."""

from __future__ import annotations

import logging
from typing import Optional

from aiohttp import web

from .. import logging_control
from ..errors import anthropic_error_payload
from ..providers import get_executor
from ..registry import get_route
from ..utils import mask_secret


log = logging.getLogger(__name__)


def _extract_token(request: web.Request) -> Optional[str]:
    auth = (request.headers.get("Authorization") or "").strip()
    x_api = (request.headers.get("x-api-key") or "").strip()

    if auth:
        parts = auth.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
    if x_api:
        return x_api
    return None


def _extract_alt(request: web.Request) -> Optional[str]:
    alt = request.rel_url.query.get("alt")
    if alt is None:
        alt = request.rel_url.query.get("$alt")
    if alt == "sse":
        return ""
    return alt


async def handle_count_tokens(request: web.Request) -> web.Response:
    token = _extract_token(request)
    if not token:
        log.info("Auth failed: missing Authorization header")
        return web.Response(status=401, text="missing Authorization or x-api-key")

    route = get_route(token)
    if route is None:
        log.info("Auth failed: unknown route token=%s", mask_secret(token))
        return web.Response(status=401, text="unknown route token")

    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            anthropic_error_payload("Invalid JSON body", "invalid_request_error"),
            status=400,
        )

    requested_model = body.get("model", "")

    # Always log basic request info
    log.info(
        "CountTokens: provider=%s model=%s",
        route.provider,
        requested_model or route.model,
    )

    # Log detailed info in debug mode
    if logging_control.is_enabled():
        log.info(
            "  token=%s base_url=%s key=%s",
            token,
            route.base_url,
            mask_secret(route.api_key),
        )

    if not route.api_key:
        return web.json_response(
            anthropic_error_payload("Route is missing an api_key", "invalid_request_error"),
            status=400,
        )

    alt = _extract_alt(request)

    executor = get_executor(route.provider, route, body, requested_model, alt=alt)

    # Dispatch to provider-specific counter when available
    if hasattr(executor, "count_tokens"):
        return await executor.count_tokens(request)

    # Provider doesn't support token counting
    return web.json_response(
        anthropic_error_payload("Provider does not support token counting", "not_supported_error"),
        status=400,
    )


__all__ = ["handle_count_tokens"]

