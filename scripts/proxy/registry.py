"""In-memory route registry used by the broker to configure proxy routes."""

from __future__ import annotations

from typing import Dict, Optional
from dataclasses import dataclass

from .config import ModelConfig


@dataclass
class RouteState:
    """Track current and pending credentials for a route token."""
    current: ModelConfig
    pending: Optional[ModelConfig] = None


_ROUTES: Dict[str, RouteState] = {}


def register_route(token: str, cfg: ModelConfig) -> None:
    """
    Register or replace a per-session upstream route.

    For initial registration, sets as current.
    For updates, sets as pending (will swap on next turn).
    """
    if token in _ROUTES:
        # Existing route - queue credentials for next turn
        _ROUTES[token].pending = cfg
    else:
        # New route - set as current immediately
        _ROUTES[token] = RouteState(current=cfg)


def get_route(token: str) -> Optional[ModelConfig]:
    """
    Return the upstream configuration for a route token.

    On each call (new turn), swaps pending→current if pending exists.
    This ensures mid-turn requests keep same credentials.
    """
    state = _ROUTES.get(token)
    if state is None:
        return None

    # New turn detected - swap pending credentials if they exist
    if state.pending is not None:
        state.current = state.pending
        state.pending = None

    return state.current


def update_credentials(token: str, cfg: ModelConfig) -> None:
    """
    Update credentials for an existing route (queued for next turn).

    Credentials will be applied on the next inbound request (new turn).
    """
    if token in _ROUTES:
        _ROUTES[token].pending = cfg
    else:
        # If route doesn't exist, register it
        register_route(token, cfg)


def unregister_route(token: str) -> None:
    """Remove a previously registered route token."""
    _ROUTES.pop(token, None)


def clear_routes() -> None:
    """Remove all registered routes (useful in tests)."""
    _ROUTES.clear()


__all__ = [
    "register_route",
    "get_route",
    "update_credentials",
    "unregister_route",
    "clear_routes",
]
