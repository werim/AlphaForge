import asyncio
import json

from sqlalchemy import text

from alphaforge.signed_read_budget import SignedReadDenied
from test_issue662_reconciliation_read_lifetime import runtime


def test_periodic_budget_phase_start_denial_uses_existing_fail_closed_flow(monkeypatch):
    from alphaforge import signed_read_budget

    attempts = []
    sleeps = []
    provider_reads = []

    class ExhaustedBudget:
        def start(self, phase):
            attempts.append(phase)
            raise SignedReadDenied("SIGNED_READ_BUDGET_DENIED")

    class Provider:
        def snapshot(self):
            provider_reads.append(True)
            raise AssertionError("provider called after budget denial")

    orchestrator = runtime(Provider())
    orchestrator.config.reconciliation_interval_sec = 5
    orchestrator._runtime_status = "OPERATING"
    monkeypatch.setattr(signed_read_budget, "configured", lambda: ExhaustedBudget())

    async def stop_after_canonical_sleep(delay):
        sleeps.append(delay)
        orchestrator._stop_event.set()

    monkeypatch.setattr(asyncio, "sleep", stop_after_canonical_sleep)
    asyncio.run(orchestrator._reconciliation_loop())

    assert attempts == ["periodic"]
    assert provider_reads == []
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 5
    assert orchestrator._runtime_status == "RECOVERY_REQUIRED"
    assert orchestrator._execution_reconciliation_blocked()
    assert orchestrator._exchange_read_only_status == "UNAVAILABLE"
    with orchestrator._resolve_persistence_engine().connect() as conn:
        row = conn.execute(text(
            "SELECT status, diagnostics_json FROM exchange_reconciliation_events"
        )).one()
    assert row.status == "EXCHANGE_STATE_UNKNOWN"
    diagnostics = json.loads(row.diagnostics_json)
    assert diagnostics["errors"] == ["SignedReadDenied"]
    assert diagnostics["reconciliation_trace_id"]
