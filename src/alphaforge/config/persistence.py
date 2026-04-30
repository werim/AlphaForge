from sqlalchemy.orm import Session

from alphaforge.models.schema import ConfigSnapshot


def persist_config_snapshot(session: Session, component: str, version: str, payload: dict) -> ConfigSnapshot:
    snapshot = ConfigSnapshot(component=component, version=version, payload=payload)
    session.add(snapshot)
    session.flush()
    return snapshot
