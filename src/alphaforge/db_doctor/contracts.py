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

