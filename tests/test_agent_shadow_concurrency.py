import asyncio

from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

from alphaforge.agents.persistence import create_agent_shadow_engine
from alphaforge.persistence import init_db, save_rejected_decision_artifact, save_trade_lifecycle_event
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator
from alphaforge.runtime_heartbeat import save_runtime_heartbeat
from alphaforge.runtime_state import save_exchange_reconciliation_event


async def _scanner():
    return []


def test_disabled_canonical_init_has_no_agent_ddl(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'canonical.db'}")
    assert not {"agent_runs", "agent_stage_events"}.intersection(inspect(engine).get_table_names())


def test_60_shadow_decisions_use_one_bounded_worker_and_isolated_database(tmp_path):
    canonical = init_db(f"sqlite+pysqlite:///{tmp_path / 'canonical.db'}")
    shadow_url = f"sqlite+pysqlite:///{tmp_path / 'shadow.db'}"
    Session = sessionmaker(bind=canonical, expire_on_commit=False, future=True)
    runtime = RuntimeOrchestrator(RuntimeConfig(
        execution_mode=ExecutionMode.PAPER, agent_graph_enabled=True, agent_graph_shadow=True,
        agent_graph_max_pending_runs=64, agent_graph_database_url=shadow_url), object(), _scanner)

    def authoritative_writes():
        for index in range(15):
            with Session() as session:
                assert save_trade_lifecycle_event(session, event_id=f"life-{index}",
                    signal_id=f"life-signal-{index}", symbol="BTCUSDT", mode="PAPER",
                    lifecycle_state="SIGNAL_CREATED", lifecycle_id=f"life-id-{index}")
            with Session() as session:
                assert save_rejected_decision_artifact(session, signal_id=f"reject-{index}",
                    symbol="ETHUSDT", mode="PAPER", reason="HIGH_SPREAD", score=None,
                    rr=None, effective_rr=None, execution_ctx={"evidence_status": "UNAVAILABLE"})
            save_runtime_heartbeat(canonical, runtime_instance_id=f"runtime-{index}",
                execution_mode="PAPER", scanner_source="TEST")
            save_exchange_reconciliation_event(canonical, instance_id=f"runtime-{index}",
                startup_id="startup", mode="PAPER", status="CLEAN")

    async def exercise():
        runtime._initialize_agent_shadow()
        assert runtime.metrics.agent_shadow_worker_count == 1
        for index in range(60):
            runtime._schedule_agent_shadow({"signal_id": f"shadow-{index}", "symbol": "BTCUSDT",
                                            "decision": "REJECTED", "reason": "HIGH_SPREAD"})
        assert runtime._shadow_queue is not None
        assert runtime._shadow_queue.qsize() <= 64
        await asyncio.gather(asyncio.to_thread(authoritative_writes), runtime._shadow_queue.join())
        assert runtime.metrics.agent_shadow_runs == 60
        assert runtime.metrics.agent_shadow_dropped == 0
        assert runtime.metrics.agent_shadow_worker_count == 1
        assert len([task for task in asyncio.all_tasks() if task.get_name() == "agent_shadow_worker"]) == 1
        runtime._shadow_worker_task.cancel()
        try:
            await runtime._shadow_worker_task
        except asyncio.CancelledError:
            pass

    asyncio.run(exercise())
    shadow = create_agent_shadow_engine(shadow_url)
    with shadow.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM agent_runs")).scalar_one() == 60
        assert conn.execute(text("SELECT count(*) FROM agent_stage_events")).scalar_one() == 480
    with canonical.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM trade_lifecycle_events")).scalar_one() >= 30
        assert conn.execute(text("SELECT count(*) FROM runtime_heartbeats")).scalar_one() == 15
        assert conn.execute(text("SELECT count(*) FROM exchange_reconciliation_events")).scalar_one() == 15


def test_queue_overload_drops_newest_without_creating_tasks(tmp_path):
    runtime = RuntimeOrchestrator(RuntimeConfig(agent_graph_enabled=True, agent_graph_shadow=True,
        agent_graph_max_pending_runs=2, agent_graph_persist_traces=False), object(), _scanner)

    async def exercise():
        runtime._shadow_queue = asyncio.Queue(maxsize=2)
        runtime._schedule_agent_shadow({"signal_id": "one"})
        runtime._schedule_agent_shadow({"signal_id": "two"})
        runtime._schedule_agent_shadow({"signal_id": "three"})
        assert runtime._shadow_queue.qsize() == 2
        assert runtime.metrics.agent_shadow_dropped == 1
        assert runtime._shadow_worker_task is None

    asyncio.run(exercise())
