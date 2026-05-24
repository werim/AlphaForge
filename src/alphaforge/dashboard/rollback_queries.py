from __future__ import annotations

from typing import Any

from sqlalchemy.engine import Engine

from alphaforge.rollback_evidence import latest_persisted_rollback_evidence


def fetch_rollback_evidence_status(engine: Engine) -> dict[str, Any]:
    """Read the latest persisted emergency-control validation evidence only."""

    return latest_persisted_rollback_evidence(engine)
