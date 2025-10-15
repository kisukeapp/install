"""Helpers for normalising request metadata shared across providers."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Dict


_ACCOUNT_ID: str | None = None
_SESSION_ID: str | None = None
_USER_ID: str | None = None


def _stable_identifiers() -> str:
    """Return a process-wide stable user identifier.

    The Go CLI proxy seeds a pseudo account/session pair and derives a user id from
    them. Reproducing the same approach keeps downstream tools (and analytics) from
    seeing a constantly changing identifier across requests served by the same
    proxy instance.
    """

    global _ACCOUNT_ID, _SESSION_ID, _USER_ID

    if _ACCOUNT_ID is None:
        _ACCOUNT_ID = uuid.uuid4().hex
    if _SESSION_ID is None:
        _SESSION_ID = uuid.uuid4().hex
    if _USER_ID is None:
        digest = hashlib.sha256(f"{_ACCOUNT_ID}{_SESSION_ID}".encode("utf-8")).hexdigest()
        _USER_ID = f"user_{digest}_account_{_ACCOUNT_ID}_session_{_SESSION_ID}"
    return _USER_ID


def ensure_metadata(raw_body: Dict[str, Any] | None) -> Dict[str, Any]:
    """Return a metadata mapping that mirrors CLIProxyAPI's structure.

    The reference implementation guarantees a ``user_id`` string while discarding
    additional caller-supplied metadata. OpenAI rejects nested metadata payloads,
    so we follow the same rule by only preserving a pre-existing ``user_id`` when
    it is a non-empty string and otherwise synthesising one.
    """

    user_id: str | None = None
    if isinstance(raw_body, dict):
        candidate = raw_body.get("metadata")
        if isinstance(candidate, dict):
            value = candidate.get("user_id")
            if isinstance(value, str) and value.strip():
                user_id = value.strip()

    if not user_id:
        user_id = _stable_identifiers()

    return {"user_id": user_id}


def extract_thinking(raw_body: Dict[str, Any] | None) -> Dict[str, Any] | None:
    """Return a copy of the thinking configuration when present."""

    if not isinstance(raw_body, dict):
        return None
    thinking = raw_body.get("thinking")
    if isinstance(thinking, dict):
        return dict(thinking)
    return None


__all__ = ["ensure_metadata", "extract_thinking"]
