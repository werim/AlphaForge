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
    surface: str
    requirement: str = "OPTIONAL"
    role: str = "RUNTIME_OWNED"
    columns: tuple[str,...] = ()
    owners: tuple[str,...] = ("INIT_DB",)
    sources: tuple[str,...] = ("src/alphaforge/persistence.py:init_db",)

def c(surface, requirement="OPTIONAL", role="RUNTIME_OWNED", columns=("id",), owners=("INIT_DB",), sources=("src/alphaforge/persistence.py:init_db",)):
    return TableContract(surface,requirement,role,tuple(columns),tuple(owners),tuple(sources))

CONTRACTS={
"signals":c("core","REQUIRED",columns=("id","signal_id","symbol","side","timeframe","mode","created_at"),owners=("INIT_DB","ORM_METADATA"),sources=("src/alphaforge/persistence.py:init_db","src/alphaforge/models/ai_schema.py:Signal")),
"order_decisions":c("core","REQUIRED",columns=("id","decision_id","signal_id","decision","created_at"),owners=("INIT_DB","ORM_METADATA"),sources=("src/alphaforge/persistence.py:init_db","src/alphaforge/models/ai_schema.py:OrderDecision")),
"signal_id_state":c("core","REQUIRED",columns=("id","scope","last_signal_id")),
"positions":c("core","CONDITIONAL","ADAPTER",("id","position_id","symbol","qty","status"),("INIT_DB","SCHEMA_DOCTOR"),("src/alphaforge/persistence.py:init_db","src/alphaforge/schema_doctor.py:repair")),
"orders":c("core","CONDITIONAL","ADAPTER",("id","order_id","symbol","status"),("INIT_DB","SCHEMA_DOCTOR"),("src/alphaforge/persistence.py:init_db","src/alphaforge/schema_doctor.py:repair")),
TABLE:c("lifecycle","REQUIRED",columns=("id",)+tuple(sorted(WRITER_COLUMNS)),owners=("ALEMBIC","INIT_DB","ORM_METADATA"),sources=("alembic/versions/0008_database_doctor_lifecycle_contract.py","src/alphaforge/persistence.py:init_db","src/alphaforge/models/ai_schema.py:TradeLifecycleEvent")),
"runtime_control_state":c("runtime","REQUIRED",columns=("id","mode_requested","runtime_status","updated_at"),owners=("INIT_DB","RUNTIME_BOOTSTRAP"),sources=("src/alphaforge/persistence.py:init_db","src/alphaforge/runtime_control.py:ensure_runtime_control_schema")),
"runtime_control_audit_events":c("runtime",columns=("id","event_ts","action","source"),owners=("RUNTIME_BOOTSTRAP",),sources=("src/alphaforge/runtime_control.py:ensure_runtime_control_schema",)),
"runtime_heartbeats":c("runtime","CONDITIONAL",columns=("id","runtime_instance_id","execution_mode","heartbeat_ts","runtime_state","evidence_status"),owners=("RUNTIME_BOOTSTRAP",),sources=("src/alphaforge/runtime_heartbeat.py:ensure_runtime_heartbeat_schema",)),
"runtime_state_snapshots":c("runtime","REQUIRED",columns=("id","timestamp","instance_id","mode"),owners=("INIT_DB","RUNTIME_BOOTSTRAP"),sources=("src/alphaforge/persistence.py:init_db","src/alphaforge/runtime_state.py:ensure_runtime_state_schema")),
"runtime_recovery_events":c("runtime",columns=("id","event_ts","instance_id","status"),owners=("INIT_DB","RUNTIME_BOOTSTRAP")),
"exchange_reconciliation_events":c("runtime",columns=("id","event_ts","instance_id","status"),owners=("INIT_DB","RUNTIME_BOOTSTRAP")),
"reconciliation_incidents":c("runtime",columns=("id","incident_id","created_at","severity"),owners=("RUNTIME_BOOTSTRAP",),sources=("src/alphaforge/reconciliation.py:ensure_reconciliation_schema",)),
}
for n,cols in {"fills":("id","fill_id","order_id","qty","price"),"paper_events":("id","event_id","event_type","created_at"),"backtest_runs":("id","run_id","started_at"),"backtest_events":("id","event_id","run_id","event_type"),"symbol_snapshots":("id","symbol","snapshot_ts"),"exchange_symbols":("id","symbol","market_type","status")}.items(): CONTRACTS[n]=c("core",columns=cols,owners=("ALEMBIC","ORM_METADATA") if n=="exchange_symbols" else ("INIT_DB",),sources=("alembic/versions/0001_phase1_init.py","src/alphaforge/models/schema.py:ExchangeSymbol") if n=="exchange_symbols" else ("src/alphaforge/persistence.py:init_db",))
for n in ("burnin_runs","burnin_observations","burnin_trade_outcomes","burnin_reject_outcomes","burnin_regime_metrics","burnin_execution_metrics","burnin_calibration_metrics","burnin_drawdown_events"): CONTRACTS[n]=c("burnin","CONDITIONAL","EVIDENCE_ONLY",("id","schema_version"),("BURNIN_BOOTSTRAP",),("src/alphaforge/burnin.py:ensure_burnin_schema",))
for n in ("burnin_campaigns","burnin_campaign_runs","burnin_campaign_events","burnin_pending_reject_labels","burnin_pending_position_outcomes"): CONTRACTS[n]=c("campaign","CONDITIONAL","EVIDENCE_ONLY",("id","schema_version"),("CAMPAIGN_BOOTSTRAP",),("src/alphaforge/burnin_campaign.py:ensure_campaign_schema",))
for n in ("burnin_preflight_reports","burnin_health_history","burnin_ops_incidents","burnin_integrity_audits","burnin_source_evidence_hashes"): CONTRACTS[n]=c("campaign","CONDITIONAL","EVIDENCE_ONLY",("id","schema_version"),("OPS_BOOTSTRAP",),("src/alphaforge/burnin_ops.py:ensure_ops_schema",))
for n in ("closed_trade_reviews","rejected_signal_reviews","adaptive_stats","setup_expectancy_stats","regime_expectancy_stats","symbol_expectancy_stats"): CONTRACTS[n]=c("adaptive_expectancy",role="EVIDENCE_ONLY",columns=("id",) if n in ("closed_trade_reviews","rejected_signal_reviews","adaptive_stats") else (n.split("_expectancy_stats")[0],"samples","expectancy"),owners=("INIT_DB","ORM_METADATA") if n!="adaptive_stats" else ("INIT_DB",))
for n in ("adaptive_threshold_stats","expectancy_stats"): CONTRACTS[n]=c("adaptive_expectancy",role="EVIDENCE_ONLY",owners=("ORM_METADATA",),sources=("src/alphaforge/models/ai_schema.py",))
for n in ("live_readiness_reports","live_alert_delivery_evidence","live_rollback_validation_evidence","release_gate_snapshots","operator_acknowledgements","canary_run_events","rollback_verification_events","runbook_evidence"): CONTRACTS[n]=c("readiness_release",role="EVIDENCE_ONLY",owners=("RUNTIME_BOOTSTRAP","INIT_DB"),sources=("runtime evidence bootstrap","src/alphaforge/persistence.py:init_db"))
OWNER_MAP={n:{"owners":list(x.owners),"sources":list(x.sources)} for n,x in CONTRACTS.items()}
WRITER_READER_MATRIX={"persistence.save_signal":("signals",),"persistence.save_order_decision":("order_decisions",),"persistence.save_trade_lifecycle_event":(TABLE,),"runtime_heartbeat.record_runtime_heartbeat":("runtime_heartbeats",),"runtime_state.persist_runtime_state":("runtime_state_snapshots",),"runtime_control":("runtime_control_state","runtime_control_audit_events"),"reconciliation.persist_incident":("reconciliation_incidents",),"burnin.persist":("burnin_runs","burnin_observations"),"adaptive_learning":("adaptive_stats",)}
UNIQUE_REQUIREMENTS={"signals":(("signal_id",),),"order_decisions":(("decision_id",),),"signal_id_state":(("scope",),),"positions":(("position_id",),),"orders":(("order_id",),),TABLE:UNIQUE_IDENTITIES,"fills":(("fill_id",),),"paper_events":(("event_id",),),"backtest_runs":(("run_id",),),"backtest_events":(("event_id",),)}
INDEX_REQUIREMENTS={"runtime_heartbeats":(("execution_mode","heartbeat_ts"),),"runtime_state_snapshots":(("timestamp",),),"reconciliation_incidents":(("created_at",),)}
