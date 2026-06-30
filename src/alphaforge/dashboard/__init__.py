"""Read-only web dashboard for AlphaForge operations visibility."""

from __future__ import annotations

from typing import Any


def create_app(*args: Any, **kwargs: Any) -> Any:
    """Lazy FastAPI app factory to keep non-web dashboard helpers importable without FastAPI."""
    from .app import create_app as _create_app

    return _create_app(*args, **kwargs)
