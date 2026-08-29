from __future__ import annotations

TABLE = "trade_lifecycle_events"
SQLITE_PK_SQL = "id INTEGER PRIMARY KEY AUTOINCREMENT"
TEXT_COLUMNS = (
    "event_id", "signal_id", "order_id", "symbol", "mode", "trade_id",
    "lifecycle_state", "state", "event_type", "payload", "decision",
    "reject_reason", "expectancy_bucket", "execution_ctx", "event_ts", "created_at",
    "cancel_reason", "lifecycle_id", "failure_reason", "reconciliation_reason",
    "incident_payload", "event_payload",
)
REAL_COLUMNS = ("score", "rr", "effective_rr")
INTEGER_COLUMNS = ("execution_ctx_missing", "lifecycle_seq", "order_intent_id")
WRITER_COLUMNS = frozenset(TEXT_COLUMNS[:-1] + REAL_COLUMNS + INTEGER_COLUMNS[:-1])
UNIQUE_IDENTITIES = (("event_id",), ("signal_id", "event_ts", "lifecycle_state"))
CURRENT_REVISION = "0008_database_doctor_lifecycle_contract"

from dataclasses import dataclass
@dataclass(frozen=True)
class TableContract:
    surface:str; requirement:str="OPTIONAL"; role:str="RUNTIME_OWNED"; columns:tuple[str,...]=(); owner:str="INIT_DB"; source:str="src/alphaforge/persistence.py:init_db"
def _c(surface, requirement="OPTIONAL", role="RUNTIME_OWNED", columns=(), owner="INIT_DB", source="src/alphaforge/persistence.py:init_db"):
    return TableContract(surface,requirement,role,tuple(columns),owner,source)
CONTRACTS={
 "signals":_c("core","REQUIRED",columns=("id","signal_id","symbol","mode","created_at")),
 "order_decisions":_c("core","REQUIRED",columns=("id","decision_id","signal_id","decision","created_at")),
 "signal_id_state":_c("core","REQUIRED",columns=("id","scope","last_signal_id")),
 "positions":_c("core","CONDITIONAL","ADAPTER",("id","position_id","symbol","qty","status")),
 "orders":_c("core","CONDITIONAL","ADAPTER",("id","order_id","symbol","status")),
 TABLE:_c("lifecycle","REQUIRED",columns=("id",)+tuple(sorted(WRITER_COLUMNS)),owner="MULTIPLE",source="alembic/versions/0008_database_doctor_lifecycle_contract.py;src/alphaforge/persistence.py:init_db"),
 "runtime_control_state":_c("runtime","REQUIRED",columns=("id","mode_requested","runtime_status","updated_at"),owner="MULTIPLE"),
 "runtime_heartbeats":_c("runtime","CONDITIONAL",columns=("id","runtime_instance_id","execution_mode","heartbeat_ts"),owner="RUNTIME_BOOTSTRAP"),
 "runtime_state_snapshots":_c("runtime","REQUIRED",columns=("id","timestamp","instance_id","mode"),owner="MULTIPLE"),
}
for n in ("fills","paper_events","backtest_runs","backtest_events","symbol_snapshots","exchange_symbols"): CONTRACTS[n]=_c("core")
for n in ("runtime_control_audit_events","runtime_recovery_events","exchange_reconciliation_events","reconciliation_incidents"): CONTRACTS[n]=_c("runtime")
for n in ("burnin_runs","burnin_observations","burnin_trade_outcomes","burnin_reject_outcomes","burnin_regime_metrics","burnin_execution_metrics","burnin_calibration_metrics","burnin_drawdown_events"): CONTRACTS[n]=_c("burnin","CONDITIONAL","EVIDENCE_ONLY",owner="BURNIN_BOOTSTRAP")
for n in ("burnin_campaigns","burnin_campaign_runs","burnin_campaign_events","burnin_pending_reject_labels","burnin_pending_position_outcomes"): CONTRACTS[n]=_c("campaign","CONDITIONAL","EVIDENCE_ONLY",owner="CAMPAIGN_BOOTSTRAP")
for n in ("burnin_preflight_reports","burnin_health_history","burnin_ops_incidents","burnin_integrity_audits","burnin_source_evidence_hashes"): CONTRACTS[n]=_c("campaign","CONDITIONAL","EVIDENCE_ONLY",owner="OPS_BOOTSTRAP")
for n in ("closed_trade_reviews","rejected_signal_reviews","adaptive_stats","setup_expectancy_stats","regime_expectancy_stats","symbol_expectancy_stats"): CONTRACTS[n]=_c("adaptive_expectancy",role="EVIDENCE_ONLY")
for n in ("adaptive_threshold_stats","expectancy_stats"): CONTRACTS[n]=_c("adaptive_expectancy",role="EVIDENCE_ONLY",owner="ORM_METADATA")
for n in ("live_readiness_reports","live_alert_delivery_evidence","live_rollback_validation_evidence","release_gate_snapshots","operator_acknowledgements","canary_run_events","rollback_verification_events","runbook_evidence"): CONTRACTS[n]=_c("readiness_release",role="EVIDENCE_ONLY",owner="RUNTIME_BOOTSTRAP")
OWNER_MAP={n:{"owners":[c.owner],"sources":c.source.split(";")} for n,c in CONTRACTS.items()}
WRITER_READER_MATRIX={"persistence.save_signal":("signals",),"persistence.save_order_decision":("order_decisions",),"persistence.save_trade_lifecycle_event":(TABLE,),"runtime_heartbeat":("runtime_heartbeats",),"runtime_state":("runtime_state_snapshots",),"runtime_control":("runtime_control_state",),"burnin":("burnin_runs","burnin_observations"),"adaptive_learning":("adaptive_stats",)}
