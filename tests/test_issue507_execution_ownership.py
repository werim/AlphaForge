from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from alphaforge.execution_ownership import (
    acquire_execution_ownership,
    validate_execution_ownership,
)
from alphaforge.persistence import init_db
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator


class _Adapter:
    def __init__(self) -> None:
        self.calls = 0

    async def submit(self, decision, market_ctx):
        self.calls += 1
        return {"order_id": f"live-{self.calls}", "status": "filled"}


def _engine(tmp_path, name: str = "ownership.sqlite3"):
    return init_db(f"sqlite+pysqlite:///{tmp_path / name}")


def _runtime(
    engine,
    *,
    mode: ExecutionMode,
    scope: str = "",
    adapter=None,
) -> RuntimeOrchestrator:
    return RuntimeOrchestrator(
        config=RuntimeConfig(
            execution_mode=mode,
            execution_account_scope=scope,
            live_trading_enabled=mode is ExecutionMode.LIVE,
            allow_live_orders=mode is ExecutionMode.LIVE,
            operator_live_acknowledged=mode is ExecutionMode.LIVE,
        ),
        ai_brain=None,
        market_scanner=None,
        real_execution_adapter=adapter,
        persistence_engine=engine,
    )


def test_two_runtimes_same_account_scope_only_one_owner(tmp_path):
    engine = _engine(tmp_path)
    first = acquire_execution_ownership(
        engine,
        account_scope="live:binance:test-account",
        mode="LIVE",
        owner_instance_id="runtime-a",
        owner_startup_id="startup-a",
        lease_ttl_sec=30.0,
        now=100.0,
    )
    second = acquire_execution_ownership(
        engine,
        account_scope="live:binance:test-account",
        mode="LIVE",
        owner_instance_id="runtime-b",
        owner_startup_id="startup-b",
        lease_ttl_sec=30.0,
        now=100.0,
    )

    assert first.acquired is True
    assert first.fencing_token == 1
    assert second.acquired is False
    assert second.fencing_token == first.fencing_token
    assert second.reason == "EXECUTION_ACCOUNT_OWNED_BY_ANOTHER_RUNTIME"


def test_stale_owner_is_fenced_after_lease_transfer(tmp_path):
    engine = _engine(tmp_path)
    first = acquire_execution_ownership(
        engine,
        account_scope="live:binance:test-account",
        mode="LIVE",
        owner_instance_id="runtime-a",
        owner_startup_id="startup-a",
        lease_ttl_sec=10.0,
        now=100.0,
    )
    early_takeover = acquire_execution_ownership(
        engine,
        account_scope="live:binance:test-account",
        mode="LIVE",
        owner_instance_id="runtime-b",
        owner_startup_id="startup-b",
        lease_ttl_sec=10.0,
        now=105.0,
    )
    transferred = acquire_execution_ownership(
        engine,
        account_scope="live:binance:test-account",
        mode="LIVE",
        owner_instance_id="runtime-b",
        owner_startup_id="startup-b",
        lease_ttl_sec=10.0,
        now=111.0,
    )

    assert first.acquired is True
    assert early_takeover.acquired is False
    assert transferred.acquired is True
    assert transferred.fencing_token == 2

    stale = validate_execution_ownership(
        engine,
        account_scope="live:binance:test-account",
        owner_instance_id="runtime-a",
        owner_startup_id="startup-a",
        fencing_token=first.fencing_token,
        now=111.0,
    )
    assert stale.acquired is False
    assert stale.reason in {
        "EXECUTION_OWNER_INSTANCE_MISMATCH",
        "EXECUTION_OWNER_STARTUP_MISMATCH",
        "EXECUTION_FENCING_TOKEN_MISMATCH",
    }


def test_independent_paper_campaigns_are_explicitly_isolated(tmp_path):
    engine = _engine(tmp_path)
    first = _runtime(engine, mode=ExecutionMode.PAPER)
    second = _runtime(engine, mode=ExecutionMode.PAPER)
    first._campaign_id = "camp-a"
    second._campaign_id = "camp-b"

    first_identity = first._execution_account_identity()
    second_identity = second._execution_account_identity()

    assert first_identity["account_model"] == "ISOLATED_CAMPAIGN"
    assert second_identity["account_model"] == "ISOLATED_CAMPAIGN"
    assert first_identity["account_scope"] == "paper:campaign:camp-a"
    assert second_identity["account_scope"] == "paper:campaign:camp-b"
    assert first_identity["account_scope"] != second_identity["account_scope"]


def test_same_paper_campaign_has_single_mutation_owner(tmp_path):
    engine = _engine(tmp_path)
    first = _runtime(engine, mode=ExecutionMode.PAPER)
    second = _runtime(engine, mode=ExecutionMode.PAPER)
    first._campaign_id = second._campaign_id = "camp-shared-evidence-scope"

    first_owner = first._authorize_execution_ownership()
    assert first_owner["acquired"] is True
    with pytest.raises(RuntimeError, match="EXECUTION_OWNERSHIP_BLOCKED"):
        second._authorize_execution_ownership()

    assert first._release_execution_ownership() is True
    second_owner = second._authorize_execution_ownership()
    assert second_owner["acquired"] is True
    assert second_owner["fencing_token"] > first_owner["fencing_token"]


def test_live_mutation_requires_explicit_account_scope(tmp_path):
    engine = _engine(tmp_path)
    adapter = _Adapter()
    runtime = _runtime(engine, mode=ExecutionMode.LIVE, scope="", adapter=adapter)

    with pytest.raises(RuntimeError, match="EXECUTION_ACCOUNT_SCOPE_REQUIRED"):
        asyncio.run(
            runtime._execute(
                "BTCUSDT",
                {"signal_id": "issue507-live", "order_type": "LIMIT"},
                {"entry": 100.0, "side": "LONG"},
            )
        )
    assert adapter.calls == 0


def test_runtime_stale_owner_cannot_reach_adapter_after_handoff(tmp_path):
    engine = _engine(tmp_path)
    adapter = _Adapter()
    first = _runtime(
        engine, mode=ExecutionMode.LIVE, scope="binance:test-account", adapter=adapter
    )
    second = _runtime(
        engine, mode=ExecutionMode.LIVE, scope="binance:test-account", adapter=adapter
    )

    first_owner = first._authorize_execution_ownership()
    assert first_owner["acquired"] is True
    assert first._release_execution_ownership() is True
    second_owner = second._authorize_execution_ownership()
    assert second_owner["fencing_token"] > first_owner["fencing_token"]

    with pytest.raises(RuntimeError, match="EXECUTION_OWNERSHIP_FENCED"):
        first._validate_execution_ownership()
    assert adapter.calls == 0


def test_external_or_manual_position_remains_reconciliation_blocker(tmp_path):
    engine = _engine(tmp_path)
    runtime = _runtime(
        engine, mode=ExecutionMode.LIVE, scope="binance:test-account", adapter=_Adapter()
    )
    runtime._qualification_report = SimpleNamespace(
        qualified=True, verdict="LIVE_READY"
    )
    runtime._reconciliation_status = "CLEAN"
    runtime._orphan_positions = [{"symbol": "ETHUSDT", "qty": 1.0}]
    runtime._authorize_execution_ownership()

    authorization = runtime._authoritative_live_authorization()

    assert authorization["execution_owner_valid"] is True
    assert authorization["reconciliation_passed"] is False
