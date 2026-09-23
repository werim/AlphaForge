from __future__ import annotations
import argparse, os, signal, sys
from sqlalchemy import text
from sqlalchemy.orm import Session
from alphaforge.persistence import init_db, save_order_decision
from alphaforge.burnin_campaign import bootstrap_campaign_schema
from alphaforge.burnin_resolver import persist_pending_position, resolve_position_closure
import alphaforge.burnin_resolver as resolver

def mark(name):
    print(name, flush=True)
    signal.pause()

def position(conn, trade="trade-p1d"):
    return persist_pending_position(conn, trade_id=trade, campaign_id="camp-p1d",
        burnin_run_id="run-p1d", signal_id="sig-p1d", source_decision_id="dec-p1d",
        decision_time="2026-09-23T18:00:00Z", symbol="BTCUSDT", side="LONG",
        setup_type="CONTINUATION", entry_time="2026-09-23T18:00:00Z",
        planned_entry=100.0, simulated_fill=100.02, stop=99.0, target=102.0,
        quantity=0.1, notional=10.002, entry_spread=0.001, entry_slippage=0.0,
        entry_fee=0.001, regime="TRENDING",
        source_provenance={"execution_cost_unit":"USD","campaign_id":"camp-p1d",
            "burnin_run_id":"run-p1d","runtime_instance_id":"runtime-p1d"})

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("db"); ap.add_argument("boundary")
    a=ap.parse_args(); engine=init_db(f"sqlite+pysqlite:///{a.db}")
    with engine.begin() as conn: bootstrap_campaign_schema(conn)
    if a.boundary=="accept_before_fill":
        with Session(engine) as s:
            save_order_decision(s, decision_id="dec-p1d", signal_id="sig-p1d",
                symbol="BTCUSDT", mode="PAPER", phase="ai_internal_real",
                decision="ACCEPTED", score=.9, rr=2.0, effective_rr=1.8,
                execution_ctx={"evidence_status":"COMPLETE"})
            s.commit()
        mark("ACCEPT_DURABLE")
    if a.boundary=="fill_before_persistence":
        # Fill is deliberately process-local. No authoritative position/final
        # evidence may exist until production persistence commits.
        fill={"status":"filled","order_id":"trade-p1d","fill_price":100.02}
        assert fill["status"]=="filled"
        mark("FILL_PROCESS_LOCAL")
    if a.boundary=="pending_after_persistence":
        with engine.begin() as conn: position(conn)
        mark("PENDING_DURABLE")
    if a.boundary=="resolver_mid_finalization":
        with engine.begin() as conn:
            position(conn)
        original=resolver.persist_burnin_trade_outcome
        def crash_boundary(*args, **kwargs):
            mark("RESOLVER_TRANSACTION_OPEN")
            return original(*args, **kwargs)
        resolver.persist_burnin_trade_outcome=crash_boundary
        with engine.begin() as conn:
            resolve_position_closure(conn, trade_id="trade-p1d",
                exit_time="2026-09-23T18:01:00Z", exit_price=102.0, exit_reason="TP_HIT",
                exit_costs={"exit_spread":.001,"exit_slippage":.001,"exit_fee":.001,
                    "funding":0.0,"latency_impact_penalty":0.0})
if __name__=="__main__": main()
