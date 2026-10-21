"""Offline laboratory adapter to canonical writers; never starts an execution runtime.

Inputs are observations, not orders. Synthetic/recorded replay is diagnostic and
cannot certify future PAPER workload or grant admission.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alphaforge.burnin import persist_burnin_observation
from alphaforge.burnin_campaign import (
    bootstrap_campaign_schema, create_campaign, configure_sqlite_engine, start_or_resume_campaign,
)
from alphaforge.burnin_resolver import (
    persist_pending_reject_label, persist_pending_position,
    resolve_campaign_batch, resolve_campaign_positions,
)
from alphaforge.persistence import init_db
from alphaforge.position_management_shadow import (
    PaperPositionManagementShadowProvider, persist_position_management_shadow_proposals,
)
from alphaforge.runtime_heartbeat import save_runtime_heartbeat
from alphaforge.runtime_state import RuntimeStateSnapshot, save_runtime_state_snapshot
from alphaforge.storage_policy import StorageController, StoragePolicy, inventory
from alphaforge.symbol_selector import UniverseConstraints, build_selected_universe
from alphaforge.universe_evidence import (
    persist_universe_selection, persist_burnin_universe_selection_link,
)

ACTIONS = frozenset({"universe", "observation", "reject", "position", "resolve", "shadow", "heartbeat", "snapshot"})


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class Workload:
    """Fresh synthetic laboratory lineage only. No execution worker or network."""

    def __init__(self, directory: Path, cfg: Any, identity: dict, sha: str, tag: str):
        self.path = directory / "laboratory.db"
        if self.path.exists():
            raise ValueError("LAB_DATABASE_ALREADY_EXISTS")
        self.engine = configure_sqlite_engine(init_db(f"sqlite+pysqlite:///{self.path}"))
        self.policy = StoragePolicy.from_config(cfg.runtime)
        self.controller = StorageController(self.engine, self.policy)
        self.counts: Counter = Counter()
        self.next_maintenance_at = float("-inf")
        self.identity = identity
        self.sha = sha
        self.cfg = cfg
        self.run_id = f"calibration_{tag}"
        with self.engine.begin() as conn:
            bootstrap_campaign_schema(conn)
            lab = create_campaign(
                conn, release_id=f"CALIBRATION_LAB_{tag}", duration_days=0.25,
                symbols=[], intervals=["1h", "15m", "1m"], dynamic_universe=True,
                runtime_config=cfg.runtime,
                source_provenance={"provider": "OFFLINE_CALIBRATION", "exchange": "binance", "mode": "PAPER",
                                   "input_source": "OFFLINE_REPLAY_FIXTURE", "authenticated": False,
                                   "calibration_only": True, "execution_authority": False},
            )
            # Canonical synthetic DB lineage only: no execution worker or
            # runtime exists. This is necessary for canonical reject ownership.
            self.run_id = start_or_resume_campaign(conn, lab.campaign_id)["burnin_run_id"]
            self.campaign = {**asdict(lab), "active_run_id": self.run_id, "execution_mode": "PAPER"}
        # Keep a connection alive so closing the pool does not erase the WAL
        # before the final sample. No WAL PRAGMA is altered for a prettier rate.
        self.keeper = self.engine.connect()

    def apply(self, event: dict, timestamp: float) -> None:
        kind = event["kind"]
        data = dict(event.get("data", {}))
        # Scope comes exclusively from this laboratory. Never accept source
        # campaign/run/DB identities supplied inside an event payload.
        if {"campaign_id", "burnin_run_id", "release_id", "execution_mode", "git_commit"} & data.keys():
            raise ValueError("EVENT_SCOPE_OVERRIDE")
        common = {"campaign_id": self.campaign["campaign_id"], "burnin_run_id": self.run_id}
        now = iso(timestamp)
        if kind == "universe":
            constraints = UniverseConstraints.from_mapping(
                self.identity["config_payload"]["dynamic_universe_constraints"]
            )
            selection = build_selected_universe(
                data["candidates"], constraints, decision_timestamp=timestamp,
                execution_mode="PAPER", git_sha=self.sha,
                strategy_config_hash=self.identity["strategy_config_hash"],
            )
            with self.engine.begin() as conn:
                persist_universe_selection(conn, selection)
                persist_burnin_universe_selection_link(conn, selection, **common)
            self.counts["candidate_rows"] += len(selection.candidates)
        elif kind == "heartbeat":
            save_runtime_heartbeat(
                self.engine, runtime_instance_id=self.run_id, execution_mode="PAPER",
                scanner_source="OFFLINE_CALIBRATION", runtime_state="OPERATING",
                heartbeat_ts=now, payload=data,
            )
        elif kind == "snapshot":
            # These are laboratory observations, never forged authenticated
            # reconciliation. Canonical controller retains unknown dependencies.
            save_runtime_state_snapshot(self.engine, RuntimeStateSnapshot(
                instance_id=self.run_id, startup_id=self.run_id, mode="PAPER", requested_mode="PAPER", actual_mode="PAPER",
                runtime_status="LABORATORY", campaign_id=common["campaign_id"],
                burnin_run_id=self.run_id, release_id=self.campaign["release_id"],
                diagnostics_json={"calibration_only": True, "workload": data},
            ))
        else:
            with self.engine.begin() as conn:
                if kind == "observation":
                    persist_burnin_observation(
                        conn, observation_id=event["id"], burnin_run_id=self.run_id,
                        release_id=self.campaign["release_id"], execution_mode="PAPER",
                        observed_at=now, **{**data, "metrics": {**data.get("metrics", {}),
                            "campaign_id": common["campaign_id"], "calibration_only": True}},
                    )
                elif kind == "reject":
                    if persist_pending_reject_label(conn, **common, **data) is None:
                        raise ValueError("CANONICAL_REJECT_NOT_PERSISTED")
                elif kind == "position":
                    persist_pending_position(conn, **common, **data)
                elif kind == "resolve":
                    candles = data["candles_by_symbol"]
                    resolve_campaign_batch(conn, common["campaign_id"], candles, now=now)
                    # Canonical position resolver expects trade-scoped windows.
                    scoped = {}
                    rows = conn.exec_driver_sql("SELECT trade_id,symbol FROM burnin_pending_position_outcomes").all()
                    for trade, symbol in rows:
                        scoped[(symbol, "position", trade)] = candles.get(symbol, [])
                    resolve_campaign_positions(conn, common["campaign_id"], scoped, now=now)
                elif kind == "shadow":
                    row = conn.exec_driver_sql(
                        "SELECT * FROM burnin_pending_position_outcomes WHERE trade_id=?",
                        (data["trade_id"],),
                    ).mappings().one()
                    proposals = PaperPositionManagementShadowProvider(
                        max_evidence_age_seconds=self.cfg.runtime.stale_market_data_sec
                    )(dict(row), data["candles"], now, self.campaign)["proposals"]
                    persist_position_management_shadow_proposals(conn, proposals)
                else:
                    raise ValueError("WORKLOAD_ACTION_UNSUPPORTED")
        self.counts[kind] += 1

    def maintain(self, timestamp: float) -> dict | None:
        if timestamp < self.next_maintenance_at:
            return None
        self.next_maintenance_at = timestamp + self.policy.check_interval_sec
        # One SSOT: includes canonical full archive eligibility, archive work,
        # protected dependencies, writer arbitration and checkpoint policy.
        report = self.controller.check(force=True)
        return {k: report.get(k) for k in (
            "status", "blocked", "blockers", "checkpoint", "archives_verified", "rows_removed",
        )}

    def sample(self) -> dict:
        info = inventory(self.path, self.policy, detailed=False)
        # Reading these PRAGMAs does not checkpoint or truncate the WAL.
        with self.engine.connect() as conn:
            pragmas = {name: conn.exec_driver_sql(f"PRAGMA {name}").scalar_one() for name in (
                "journal_mode", "wal_autocheckpoint", "page_size", "page_count", "freelist_count",
            )}
        return {"main_bytes": info["main_bytes"], "wal_bytes": info["wal_bytes"],
                "shm_bytes": info["shm_bytes"], "allocated_live_bytes": info["allocated_live_bytes"],
                "pragmas": pragmas}

    def integrity(self) -> dict:
        with self.engine.connect() as conn:
            return {"integrity": conn.exec_driver_sql("PRAGMA integrity_check").scalar_one(),
                    "foreign_key_violations": len(conn.exec_driver_sql("PRAGMA foreign_key_check").all())}

    def close(self) -> None:
        self.keeper.close()
        self.engine.dispose()
