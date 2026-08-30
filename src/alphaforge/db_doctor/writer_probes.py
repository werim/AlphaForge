from __future__ import annotations
import tempfile, uuid
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from alphaforge.persistence import save_signal, save_order_decision, save_trade_lifecycle_event
from alphaforge.reconciliation import ReconciliationFinding, persist_findings
from alphaforge.runtime_heartbeat import save_runtime_heartbeat
from alphaforge.runtime_state import RuntimeStateSnapshot, save_runtime_state_snapshot
from .backup import snapshot_database

def run_writer_probes(path: Path) -> dict:
    # Production writers commit internally, so probe a transactionally consistent
    # SQLite online-backup snapshot rather than contaminating the source evidence.
    with tempfile.TemporaryDirectory(prefix="alphaforge-doctor-probe-") as directory:
        probe = snapshot_database(path, Path(directory) / "probe.db")
        engine=create_engine(f"sqlite+pysqlite:///{probe}",future=True); token=f"db-doctor:{uuid.uuid4()}"
        checks=[]
        try:
            with Session(engine) as session:
                checks.append(("save_signal", bool(save_signal(session,signal_id=token,symbol="DBDOCTOR",side="LONG",timeframe="1m",mode="PAPER"))))
                checks.append(("save_trade_lifecycle_event_signal_created", save_trade_lifecycle_event(session,event_id=token+":created",signal_id=token,symbol="DBDOCTOR",mode="PAPER",lifecycle_state="SIGNAL_CREATED")))
                checks.append(("save_trade_lifecycle_event_upsert", save_trade_lifecycle_event(session,event_id=token+":created",signal_id=token,symbol="DBDOCTOR",mode="PAPER",lifecycle_state="SIGNAL_CREATED")))
                checks.append(("save_trade_lifecycle_event_signal_rejected", save_trade_lifecycle_event(session,event_id=token+":rejected",signal_id=token,symbol="DBDOCTOR",mode="PAPER",lifecycle_state="SIGNAL_REJECTED",previous_lifecycle_state="SIGNAL_CREATED")))
                checks.append(("save_order_decision", bool(save_order_decision(session,decision_id=token,signal_id=token,symbol="DBDOCTOR",mode="PAPER",decision="REJECTED",reject_reason="EXECUTION_RISK"))))
            save_runtime_heartbeat(engine,runtime_instance_id=token,execution_mode="PAPER",scanner_source="DATABASE_DOCTOR")
            checks.append(("runtime_heartbeat",True))
            save_runtime_state_snapshot(engine,RuntimeStateSnapshot(instance_id=token,mode="PAPER",requested_mode="PAPER",actual_mode="PAPER",runtime_status="OPERATING"))
            checks.append(("runtime_state_snapshot",True))
            persist_findings(engine,[ReconciliationFinding("LIFECYCLE_DIVERGENCE","HIGH","DBDOCTOR",token,"2026-08-29T00:00:00+00:00",{"probe":True},"OPEN",True)])
            checks.append(("reconciliation_persist_findings",True))
        except Exception as exc:
            checks.append(("exception",False)); return {"passed":False,"checks":[{"name":n,"passed":bool(v)} for n,v in checks],"error":repr(exc),"isolation":"private database copy removed"}
        finally: engine.dispose()
    return {"passed":all(v for _,v in checks),"checks":[{"name":n,"passed":bool(v)} for n,v in checks],"isolation":"private database copy removed"}
