"""
Utility functions for handlers.
"""
import uuid


def generate_id() -> str:
    """Generate a unique ID."""
    return f"id_{uuid.uuid4().hex[:12]}"


def generate_short_id() -> str:
    """Generate a short unique ID."""
    return uuid.uuid4().hex[:8]
