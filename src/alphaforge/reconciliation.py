from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Engine


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class ReconciliationFinding:
    finding_type: str
    severity: str
    symbol: str
    lifecycle_ref: str
    timestamp: str
    evidence: dict[str, Any]
    suggested_remediation: str
    fail_closed: bool

    def to_incident_row(self) -> dict[str, Any]:
        return {
            "incident_type": self.finding_type,
            "severity": self.severity,
            "symbol": self.symbol,
            "lifecycle_ref": self.lifecycle_ref,
            "remediation_status": "PENDING",
            "operator_acknowledged": 0,
            "fail_closed": 1 if self.fail_closed else 0,
            "forensic_payload": json.dumps({
                "timestamp": self.timestamp,
                "evidence": self.evidence,
                "suggested_remediation": self.suggested_remediation,
            }),
        }


@dataclass(frozen=True, slots=True)
class RepairRecommendation:
    category: str
    symbol: str
    requires_operator_approval: bool
    action_payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ReconciliationSnapshot:
    orders: list[dict[str, Any]]
    positions: list[dict[str, Any]]
    fills: list[dict[str, Any]]
    captured_at: str


class ReconciliationEngine:
    def __init__(self, *, stale_order_seconds: int = 120, fail_closed_on_high: bool = True) -> None:
        self.stale_order_seconds = stale_order_seconds
        self.fail_closed_on_high = fail_closed_on_high

    def snapshot_from_source(self, source: Mapping[str, Any]) -> ReconciliationSnapshot:
        return ReconciliationSnapshot(
            orders=[dict(o) for o in list(source.get("orders", []))],
            positions=[dict(p) for p in list(source.get("positions", []))],
            fills=[dict(f) for f in list(source.get("fills", []))],
            captured_at=str(source.get("captured_at") or _utcnow_iso()),
        )

    def reconcile(
        self,
        *,
        intended_orders: list[Mapping[str, Any]],
        lifecycle_state_by_symbol: Mapping[str, str],
        snapshot: ReconciliationSnapshot,
        mode: str,
    ) -> tuple[list[ReconciliationFinding], list[RepairRecommendation], dict[str, Any]]:
        findings: list[ReconciliationFinding] = []
        recommendations: list[RepairRecommendation] = []
        now = datetime.now(timezone.utc)
        intended_ids = {str(o.get("order_id")) for o in intended_orders if o.get("order_id")}

        for order in snapshot.orders:
            oid = str(order.get("order_id") or "")
            symbol = str(order.get("symbol") or "UNKNOWN")
            status = str(order.get("status") or "UNKNOWN").upper()
            if oid and oid not in intended_ids:
                findings.append(self._mk("ORPHAN_ORDER", "HIGH", symbol, f"order:{oid}", {"order": order}, "cancel stale order", True))
                recommendations.append(self._repair("cancel_stale_order", symbol, mode, {"order_id": oid}))
            if status == "OPEN" and self._is_stale(order, now):
                findings.append(self._mk("STALE_ORDER", "MEDIUM", symbol, f"order:{oid}", {"order": order}, "refresh exchange snapshot", False))
                recommendations.append(self._repair("refresh_exchange_snapshot", symbol, mode, {"order_id": oid}))

        open_symbols = {str(o.get("symbol") or "") for o in snapshot.orders if str(o.get("status") or "").upper() in {"OPEN", "PARTIAL_FILL"}}
        for symbol, state in lifecycle_state_by_symbol.items():
            if state in {"ENTRY_FILLED", "ENTRY_ACKNOWLEDGED"} and symbol not in open_symbols and not any(str(p.get("symbol")) == symbol for p in snapshot.positions):
                findings.append(self._mk("LIFECYCLE_DIVERGENCE", "HIGH", symbol, f"symbol:{symbol}", {"lifecycle": state, "open_symbols": sorted(open_symbols)}, "escalate incident", True))
                recommendations.append(self._repair("escalate_incident", symbol, mode, {"lifecycle_state": state}))

        intended_symbols = {str(o.get("symbol") or "") for o in intended_orders}
        for pos in snapshot.positions:
            symbol = str(pos.get("symbol") or "")
            qty = float(pos.get("qty", 0.0) or 0.0)
            if symbol not in intended_symbols and abs(qty) > 0:
                findings.append(self._mk("ORPHAN_POSITION", "HIGH", symbol, f"position:{symbol}", {"position": pos}, "close orphan position", True))
                recommendations.append(self._repair("close_orphan_position", symbol, mode, {"qty": qty}))

        metrics = {
            "reconciliation_latency_ms": 0,
            "reconciliation_success": 1 if not any(f.fail_closed for f in findings) else 0,
            "orphan_incident_rate": len([f for f in findings if "ORPHAN" in f.finding_type]),
            "stale_order_frequency": len([f for f in findings if f.finding_type == "STALE_ORDER"]),
            "lifecycle_divergence_frequency": len([f for f in findings if f.finding_type == "LIFECYCLE_DIVERGENCE"]),
            "repair_recommendation_counts": len(recommendations),
        }
        return findings, recommendations, metrics

    def _is_stale(self, order: Mapping[str, Any], now: datetime) -> bool:
        created_at = str(order.get("created_at") or "")
        if not created_at:
            return False
        try:
            ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        return (now - ts).total_seconds() >= self.stale_order_seconds

    def _mk(self, t: str, sev: str, symbol: str, ref: str, evidence: dict[str, Any], remediation: str, fail_closed: bool) -> ReconciliationFinding:
        return ReconciliationFinding(t, sev, symbol, ref, _utcnow_iso(), evidence, remediation, fail_closed and self.fail_closed_on_high)

    def _repair(self, category: str, symbol: str, mode: str, payload: dict[str, Any]) -> RepairRecommendation:
        return RepairRecommendation(category, symbol, str(mode).upper() == "LIVE", {"dry_run": True, "shadow_mode": True, **payload})


def ensure_reconciliation_tables(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS reconciliation_incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                symbol TEXT NOT NULL,
                lifecycle_ref TEXT NOT NULL,
                remediation_status TEXT NOT NULL,
                operator_acknowledged INTEGER NOT NULL DEFAULT 0,
                fail_closed INTEGER NOT NULL DEFAULT 0,
                forensic_payload TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recon_incident_created_at ON reconciliation_incidents(created_at)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recon_incident_symbol ON reconciliation_incidents(symbol)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_recon_incident_severity ON reconciliation_incidents(severity)"))


def persist_findings(engine: Engine, findings: list[ReconciliationFinding]) -> None:
    if not findings:
        return
    ensure_reconciliation_tables(engine)
    with engine.begin() as conn:
        for finding in findings:
            row = finding.to_incident_row()
            conn.execute(text("""
                INSERT INTO reconciliation_incidents
                (incident_type, severity, symbol, lifecycle_ref, remediation_status, operator_acknowledged, fail_closed, forensic_payload)
                VALUES
                (:incident_type, :severity, :symbol, :lifecycle_ref, :remediation_status, :operator_acknowledged, :fail_closed, :forensic_payload)
            """), row)
