"""Production-sink regression for repeated reconciliation timeouts (no transport)."""
import asyncio
import importlib
import json
import threading

import pytest
from sqlalchemy import text

from alphaforge import contracts
from alphaforge.persistence import init_db
from alphaforge.runtime import _build_runtime_from_env


def test_package_import_cannot_admit_noncanonical_error_repair_transition():
    import alphaforge

    importlib.reload(alphaforge)
    assert not contracts.validate_transition('ERROR', 'RECONCILIATION_REPAIR')
    assert contracts.validate_transition('ERROR', 'SIGNAL_CREATED')


@pytest.mark.parametrize('mode', ['PAPER', 'LIVE_PRECHECK'])
def test_repeated_timeout_persists_audit_and_recovers_without_weakening_gate(tmp_path, monkeypatch, mode):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'timeouts.db'}")
    monkeypatch.setenv('ALPHAFORGE_PERSISTENCE_ENABLED', '1')
    monkeypatch.setenv('ALPHAFORGE_EXECUTION_MODE', mode)
    monkeypatch.setenv('BINANCE_API_KEY', 'synthetic-unused-key')
    monkeypatch.setenv('BINANCE_API_SECRET', 'synthetic-unused-secret')
    runtime = _build_runtime_from_env(persistence_engine=engine)
    runtime.config.reconciliation_timeout_sec = 0.01

    class Provider:
        def __init__(self):
            self.release = threading.Event()

        def snapshot(self):
            self.release.wait(1)
            return {'evidence_status': 'COMPLETE', 'orders': [], 'positions': [], 'fills': []}

    provider = Provider()
    runtime.live_reconciliation_provider = provider

    async def check():
        try:
            for i in range(2):
                provider.release.clear()
                monkeypatch.setattr('alphaforge.runtime.canonical_utc_timestamp',
                                    lambda *args, i=i: f'2026-10-26T04:38:0{i}Z')
                await runtime._run_reconciliation_once()
                assert runtime._unknown_exchange_state
                assert runtime._runtime_status == 'RECOVERY_REQUIRED'
                assert runtime._execution_reconciliation_blocked()
                provider.release.set()
                while runtime._reconciliation_reader._pending is not None and not runtime._reconciliation_reader._pending.done():
                    await asyncio.sleep(0.001)
            # A fresh complete read can recover; abandoned COMPLETE is not reused.
            await runtime._run_reconciliation_once()
            assert runtime._reconciliation_status == 'CLEAN'
            assert not runtime._unknown_exchange_state
        finally:
            provider.release.set()
            await runtime._reconciliation_reader.close(1)

    asyncio.run(check())
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT lifecycle_state,failure_reason,payload FROM trade_lifecycle_events WHERE symbol='GLOBAL' ORDER BY id")).all()
        assert len(rows) == 2
        for count, row in enumerate(rows, 1):
            assert row.lifecycle_state == 'ERROR'
            assert row.failure_reason == 'INVALID_LIFECYCLE_TRANSITION'
            details = json.loads(row.payload)
            assert details['attempted_state'] == 'RECONCILIATION_REPAIR'
            assert details['reason'] == 'reconciliation_timeout'
            assert details['incident_count'] == count
    engine.dispose()
