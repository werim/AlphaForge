from __future__ import annotations
import re
SURFACES={
"runtime_heartbeat":"src/alphaforge/runtime_heartbeat.py","runtime_state":"src/alphaforge/runtime_state.py","runtime_control":"src/alphaforge/runtime_control.py","reconciliation":"src/alphaforge/reconciliation.py","rollback_evidence":"src/alphaforge/rollback_evidence.py","release_gates":"src/alphaforge/release_gates.py","live_readiness":"src/alphaforge/live_readiness.py","alert_delivery":"src/alphaforge/alert_delivery.py","adaptive_learning":"src/alphaforge/adaptive_learning.py","burnin":"src/alphaforge/burnin.py","campaign":"src/alphaforge/burnin_campaign.py","ops":"src/alphaforge/burnin_ops.py"}
PATTERNS=("PRAGMA","sqlite_master","AUTOINCREMENT","INSERT OR IGNORE","json_extract","json_valid","datetime(","strftime(")
def audit_runtime_sql(root=None):
    from pathlib import Path
    root=Path(root or Path(__file__).resolve().parents[3]); out={}
    for name,rel in SURFACES.items():
        text=(root/rel).read_text(encoding="utf-8")
        hits=[p for p in PATTERNS if re.search(re.escape(p),text,re.I)]
        out[name]={"classification":"SQLITE_ONLY" if hits else "DIALECT_NEUTRAL_SQLALCHEMY" if "sqlalchemy" in text.lower() else "PORTABLE_SQL","evidence":hits,"source":rel}
    return out
