"""Compatibility shim for the canonical ``alphaforge.config`` package.

The real implementation lives in ``src/alphaforge/config/__init__.py``.  This
file is intentionally kept constant-free so environment/default filter values do
not drift between a module copy and the package imported at runtime.
"""

from __future__ import annotations

from .config import *  # noqa: F401,F403
