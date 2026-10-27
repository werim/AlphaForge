"""Source observations for new BACKTESTs; never capital-performance authority."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import nullcontext
from bisect import bisect_right
import hashlib
import json
import math
from typing import Any, Mapping
import time

from sqlalchemy import text

from .realized_accounting import finite_cash, filled_position_quantity, position_cash_geometry, realized_cash_ledger, sum_cash

VERSION = "backtest-source-v1"
BOUNDARY_VERSION = "backtest-boundary-v1"
DDL = (
    """CREATE TABLE IF NOT EXISTS backtest_source_runs (
        run_id TEXT PRIMARY KEY, identity_hash TEXT NOT NULL, identity_json TEXT NOT NULL,
        initialization_json TEXT NOT NULL, initialization_hash TEXT NOT NULL, observed_ms INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS backtest_source_events (
        run_id TEXT NOT NULL REFERENCES backtest_source_runs(run_id), sequence INTEGER NOT NULL,
        event_key TEXT NOT NULL, kind TEXT NOT NULL, effective_ms INTEGER NOT NULL,
        observed_ms INTEGER NOT NULL, payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
        PRIMARY KEY(run_id,event_key), UNIQUE(run_id,sequence))""",
    *tuple(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()}
        BEFORE {operation} ON {table} BEGIN
        SELECT RAISE(ABORT,'immutable BACKTEST source evidence'); END"""
        for table in ("backtest_source_runs", "backtest_source_events") for operation in ("UPDATE", "DELETE")),
)


def evidence_settings(rows):
    """Connection URLs can contain credentials even when registry.secret is false."""
    return [row for row in rows if not row["secret"]
            and not row["env_name"].endswith("_URL")]


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def millis():
    return time.time_ns() // 1_000_000


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    run_id: str
    window_id: str
    start_ms: int
    end_ms: int
    interval_ms: int
    git_sha: str
    code_hash: str
    config_hash: str
    strategy_hash: str
    cost_hash: str
    universe_hash: str
    data_hash: str
    currency: str = "USDT"
    version: str = VERSION

    def __post_init__(self):
        if any(type(v) is not int or v < 0 for v in (self.start_ms, self.end_ms, self.interval_ms)):
            raise ValueError("source timestamps/interval must be nonnegative integer milliseconds")
        if not self.start_ms < self.end_ms or not self.interval_ms:
            raise ValueError("invalid source window")
        if self.currency != "USDT" or self.version not in {VERSION, BOUNDARY_VERSION}:
            raise ValueError("unsupported source settlement/version")
        for name in ("run_id", "window_id"):
            if type(getattr(self, name)) is not str or not getattr(self, name).strip():
                raise ValueError(f"missing source {name}")
        for name in ("git_sha", "code_hash", "config_hash", "strategy_hash", "cost_hash", "universe_hash", "data_hash"):
            value = getattr(self, name)
            length = 40 if name == "git_sha" else 64
            if type(value) is not str or len(value) != length or any(c not in "0123456789abcdef" for c in value):
                raise ValueError(f"invalid source {name}")


class BacktestSourceProducer:
    """Observer attached before BacktestPortfolioState initialization.

    Captures eager simulator output in actual source order. Effective times are
    legacy candle labels; they are not represented as exact intrabar fills.
    """
    def __init__(self, engine, identity: SourceIdentity, candles: Mapping[str, list[Any]], *, provenance: Mapping[str, Any]):
        if engine.dialect.name != "sqlite":
            raise ValueError("source producer currently supports SQLite only")
        if sum(len(rows) for rows in candles.values()) > 100000 or (identity.end_ms - identity.start_ms) // identity.interval_ms > 10000:
            raise ValueError("source capture budget exceeded")
        with engine.begin() as conn:
            for statement in DDL:
                conn.execute(text(statement))
        self.engine, self.identity = engine, identity
        self.candles = candles
        self.source = {s: [asdict(c) for c in rows] for s, rows in sorted(candles.items())}
        if digest(self.source) != identity.data_hash or digest(sorted(self.source)) != identity.universe_hash:
            raise ValueError("source data/universe identity mismatch")
        self.provenance = dict(provenance)
        for field, key in (("config_hash", "configuration"), ("strategy_hash", "strategy"),
                           ("cost_hash", "costs"), ("code_hash", "source_files")):
            if key not in provenance or digest(provenance[key]) != getattr(identity, field):
                raise ValueError(f"source {key} provenance mismatch")
        self.state = None
        self.expected_cash = None

    def initialize(self, state):
        initial = finite_cash(state.initial_equity)
        if type(state.initial_equity) not in (int, float) or initial is None or initial <= 0 or state.current_equity != initial or state.open_positions:
            raise ValueError("source initialization requires a new flat positive-capital simulation")
        identity = asdict(self.identity)
        init = {"mode": "BACKTEST", "product_semantics": ("USDT_LINEAR_NO_FUNDING" if self.identity.version == BOUNDARY_VERSION else "LEGACY_NOTIONAL_PNL_SIMULATION"), "kind": "SIMULATION_INITIALIZED", "effective_ms": self.identity.start_ms,
                "initial_equity_usdt": initial, "opening_positions": [],
                "capital_origin": "BACKTEST_PORTFOLIO_CONSTRUCTOR", "source_order": 0,
                "flow_authority": "INSTRUMENTED_CLOSE_ONLY_V1", "source_candles": self.source,
                "provenance": self.provenance,
                "observed_time_basis": "PRODUCER_WALL_UTC_NOT_HISTORICAL_DELIVERY"}
        if self.identity.version == BOUNDARY_VERSION:
            init['timing_semantics'] = 'SIMULATED_CANDLE_BOUNDARIES_NOT_OBSERVED_INTRABAR'
            init['cost_recognition'] = 'ENTRY_COST_ACCRUAL_NET_CASH_ON_FULL_CLOSE'
        with self.engine.begin() as conn:
            old = conn.execute(text("SELECT * FROM backtest_source_runs WHERE run_id=:r"), {"r": self.identity.run_id}).mappings().first()
            if old:
                if old["identity_hash"] != digest(identity) or old["initialization_json"] != encoded(init):
                    raise ValueError("source run/initialization replay conflict")
            else:
                existing = conn.execute(text("SELECT COUNT(*) FROM decision_evidence WHERE run_id=:r"), {"r": self.identity.run_id}).scalar_one()
                if existing:
                    raise ValueError("cannot backfill an existing BACKTEST run")
                conn.execute(text("INSERT INTO backtest_source_runs VALUES (:r,:h,:i,:p,:ph,:o)"),
                             dict(r=self.identity.run_id, h=digest(identity), i=encoded(identity), p=encoded(init), ph=digest(init), o=millis()))
        self.state, self.expected_cash = state, initial

    def _append(self, entries, conn=None):
        if self.state is None:
            raise ValueError("source must be initialized by portfolio constructor")
        with (self.engine.begin() if conn is None else nullcontext(conn)) as conn:
            for key, kind, at, payload in entries:
                old = conn.execute(text("SELECT payload_hash,kind,effective_ms FROM backtest_source_events WHERE run_id=:r AND event_key=:k"), dict(r=self.identity.run_id, k=key)).mappings().first()
                if old:
                    if (old["payload_hash"], old["kind"], old["effective_ms"]) != (digest(payload), kind, at):
                        raise ValueError("source event replay conflict")
                    continue
                done = conn.execute(text("SELECT 1 FROM backtest_source_events WHERE run_id=:r AND event_key='FINAL'"), dict(r=self.identity.run_id)).first()
                if done:
                    raise ValueError("source run already finalized")
                seq = conn.execute(text("SELECT COALESCE(MAX(sequence),0)+1 FROM backtest_source_events WHERE run_id=:r"), dict(r=self.identity.run_id)).scalar_one()
                if seq > 100000:
                    raise ValueError("source event budget exceeded")
                conn.execute(text("INSERT INTO backtest_source_events VALUES (:r,:s,:k,:t,:e,:o,:p,:h)"),
                             dict(r=self.identity.run_id, s=seq, k=key, t=kind, e=at, o=millis(), p=encoded(payload), h=digest(payload)))

    def assert_cash_authority(self):
        if self.state is None or self.state.current_equity != self.expected_cash:
            raise ValueError("unsupported BACKTEST capital mutation")

    def external_flow(self, amount):
        raise ValueError("external capital flows are unsupported by this simulation")

    def cash_closed(self, *, position_id, timestamp, net_pnl_usdt, before, after):
        if before != self.expected_cash:
            raise ValueError("unsupported BACKTEST capital mutation")
        self._append([(f"cash:{position_id}", "CASH_CLOSED", int(timestamp),
                       dict(position_id=position_id, net_pnl_usdt=finite_cash(net_pnl_usdt), before=before, after=after))])
        self.expected_cash = after

    def observe_simulation(self, rows, balance, risk_pct, market_ctx):
        entries = []
        for row in rows:
            if not row.status_after.startswith("POSITION_"):
                continue
            payload = {k: getattr(row, k) for k in (
                "timestamp", "symbol", "side", "position_id", "lifecycle_id", "lifecycle_seq", "status_after",
                "trigger_price", "sl", "close_price", "filled_quantity", "net_pnl_usdt", "gross_pnl_usd",
                "initial_risk_usd", "total_cost_usd", "net_r", "realized_evidence_status", "realized_cost_source", "realized_costs_json")}
            if row.status_after == "POSITION_OPENED":
                payload["filled_quantity"] = filled_position_quantity(balance=balance, risk_pct=risk_pct,
                    fill=row.trigger_price, risk_scale=market_ctx.get("risk_scale", 1.0), notional=market_ctx.get("notional"))
            key = f"lifecycle:{row.lifecycle_id}:{row.lifecycle_seq}:{row.status_after}"
            entries.append((key, "LIFECYCLE", int(row.timestamp), payload))
        self._append(entries)

    def finish(self):
        # Acquire the writer reservation before reading source + canonical SQL.
        # Final root/population verification and final insert commit atomically.
        with self.engine.begin() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            packet = export_source_evidence(conn, self.identity.run_id)
            source = [e["payload"] for e in packet["events"] if e["kind"] == "LIFECYCLE"]
            closures = _sql_population(conn, self.identity.run_id)
            captured = sorted((p["position_id"], p["timestamp"], p["net_pnl_usdt"]) for p in source if p["status_after"] == "POSITION_CLOSED")
            if captured != closures:
                raise ValueError("source/SQL closure population mismatch")
            _verify_sql_closure_facts(conn, self.identity.run_id, source)
            gaps = []
            sql_positions = _sql_positions(conn, self.identity.run_id)
            if _source_positions(source) != sql_positions:
                raise ValueError("source/SQL position population mismatch")
            if self.state.current_equity != self.expected_cash or self.state.initial_equity != packet["initialization"]["initial_equity_usdt"]:
                gaps.append("UNSUPPORTED_CAPITAL_MUTATION")
            if digest({s: [asdict(c) for c in rows] for s, rows in sorted(self.candles.items())}) != self.identity.data_hash:
                gaps.append("SOURCE_DATA_DRIFT")
            opened_ids = {p["position_id"] for p in source if p["status_after"] == "POSITION_OPENED"}
            closed_ids = {p["position_id"] for p in source if p["status_after"] == "POSITION_CLOSED"}
            if set(self.state.open_positions) != opened_ids - closed_ids or self.state.closed_position_ids != closed_ids:
                gaps.append("PORTFOLIO_POSITION_POPULATION_MISMATCH")
            snapshots, inventory_gaps = _source_valuations(self.identity, packet["initialization"], packet["events"])
            gaps.extend(inventory_gaps)
            cash = [e["payload"] for e in packet["events"] if e["kind"] == "CASH_CLOSED"]
            if sorted((p["position_id"], p["net_pnl_usdt"]) for p in cash) != sorted((p[0], p[2]) for p in captured):
                gaps.append("CASH_POPULATION_MISMATCH")
            expected = packet["initialization"]["initial_equity_usdt"]
            seen_cash = set()
            for movement in cash:
                pid = movement["position_id"]
                if pid in seen_cash:
                    raise ValueError("duplicate cash realization")
                seen_cash.add(pid)
                reconciled = sum_cash([expected, movement["net_pnl_usdt"]])
                if movement["before"] != expected or movement["after"] != reconciled:
                    gaps.append("CASH_CONSERVATION_FAILURE")
                expected = reconciled
            if expected != self.state.current_equity:
                gaps.append("CLOSING_CASH_MISMATCH")
            if any(v["settled_cash_usdt"] is not None and v["settled_cash_usdt"] < 0 for v in snapshots):
                gaps.append("NEGATIVE_SETTLED_CASH")
            effective = [e["effective_ms"] for e in packet["events"]]
            if effective != sorted(effective):
                gaps.append("EAGER_SIMULATION_SOURCE_ORDER_NOT_CHRONOLOGICAL")
            _qualify_valuations(snapshots, gaps)
            result = dict(status="INCOMPLETE" if gaps else "COMPLETE", gaps=sorted(set(gaps)),
                          coverage_end_ms=None if gaps else self.identity.end_ms,
                          source_event_count=len(packet["events"]), source_digest=digest(packet["events"]),
                          initialization_hash=packet["initialization_hash"], identity_hash=packet["identity_hash"],
                          asof_observed_ms=max([packet["initialization_observed_ms"], *[e["observed_ms"] for e in packet["events"]]]),
                          closure_population=[list(row) for row in closures],
                          position_population=sql_positions, sql_snapshot_hash=_sql_snapshot_hash(conn, self.identity.run_id),
                          closing_cash_usdt=self.state.current_equity,
                          external_flows=[],
                          zero_flow_coverage="INCOMPLETE" if "UNSUPPORTED_CAPITAL_MUTATION" in gaps else "OWNED_CLOSE_ONLY",
                          valuations=snapshots, valuation_basis=("CANDLE_BOUNDARY_MODELED_EXECUTION_BOOK_EQUITY" if self.identity.version == BOUNDARY_VERSION else "CANDLE_CLOSE_NOT_EXECUTABLE_QUOTE"),
                          capital_return_pct=None, max_drawdown_pct=None)
            self._append([("FINAL", "FINAL", self.identity.end_ms, result)], conn=conn)
        return result


def _source_valuations(identity, initialization, events):
    if identity.version == BOUNDARY_VERSION:
        from .backtest_chronology import replay_boundary_evidence
        return replay_boundary_evidence(identity, initialization, events)
    return _valuations(identity, initialization,
        [e["payload"] for e in events if e["kind"] == "LIFECYCLE"], initialization["source_candles"])


def _qualify_valuations(snapshots, gaps):
    # A missing population or flow cannot leave a favorable flat-equity value.
    for snapshot in snapshots:
        snapshot["evidence_status"] = "INCOMPLETE" if gaps else "COMPLETE"
        if gaps:
            snapshot["marked_equity_usdt"] = None


def _valuations(identity, initialization, events, source):
    gaps, snapshots = [], []
    if not source:
        gaps.append("SOURCE_UNIVERSE_EMPTY")
    expected = set(range(identity.start_ms, identity.end_ms, identity.interval_ms))
    indexes = {}
    for symbol, bars in source.items():
        labels = [bar["timestamp"] for bar in bars]
        if any(type(at) is not int for at in labels) or labels != sorted(set(labels)):
            raise ValueError("source candle timestamps must be unique ordered integer milliseconds")
        if not expected.issubset(labels):
            gaps.append("SOURCE_CANDLE_COVERAGE_INCOMPLETE")
        if any(at >= identity.end_ms for at in labels):
            gaps.append("FUTURE_SOURCE_CANDLE")
        indexes[symbol] = labels
    starts, ends = {}, {}
    for p in events:
        pid = p["position_id"]
        if not pid or type(p["timestamp"]) is not int:
            raise ValueError("invalid source lifecycle identity/time")
        if p["status_after"] == "POSITION_OPENED":
            if pid in starts:
                raise ValueError("duplicate source opening")
            starts[pid] = p
            if p["side"] not in {"LONG", "SHORT"} or any(finite_cash(p[k]) is None or finite_cash(p[k]) <= 0 for k in ("trigger_price", "filled_quantity")):
                gaps.append("OPEN_POSITION_GEOMETRY_UNAVAILABLE")
        elif p["status_after"] == "POSITION_CLOSED":
            if pid in ends:
                raise ValueError("duplicate source closure")
            ends[pid] = p
        else:
            gaps.append("UNSUPPORTED_PARTIAL_LIFECYCLE")
    if events:
        gaps.append("CANDLE_LABEL_EVENT_TIME_ONLY")
    if any(p["timestamp"] < identity.start_ms or p["timestamp"] >= identity.end_ms for p in events):
        gaps.append("LIFECYCLE_OUTSIDE_WINDOW")
    if identity.start_ms % identity.interval_ms or identity.end_ms % identity.interval_ms:
        gaps.append("UNALIGNED_WINDOW")
    if len(starts) * ((identity.end_ms - identity.start_ms) // identity.interval_ms + 1) > 100000:
        raise ValueError("source valuation budget exceeded")
    net = {}
    for pid, p in ends.items():
        if pid not in starts or p["timestamp"] < starts[pid]["timestamp"]:
            gaps.append("POSITION_POPULATION_INCOMPLETE")
            continue
        try:
            costs = json.loads(p["realized_costs_json"])
            needed = {"entry_spread", "exit_spread", "entry_slippage", "exit_slippage", "entry_fee", "exit_fee", "funding", "latency_impact_penalty"}
            if not needed.issubset(costs) or costs["entry_slippage"] != 0 or p["realized_evidence_status"] != "COMPLETE" or p["realized_cost_source"] in {"", "UNKNOWN", "UNAVAILABLE"}:
                raise ValueError("incomplete costs")
            geometry = position_cash_geometry(fill=p["trigger_price"], stop=p["sl"], quantity=p["filled_quantity"], exit_price=p["close_price"], side=p["side"])
            ledger = realized_cash_ledger(gross_pnl=geometry["gross_pnl_usd"], initial_risk=geometry["initial_risk_usd"], costs={"funding_cost" if k == "funding" else k: v for k, v in costs.items()})
            if any(p[key] != starts[pid][key] for key in ("filled_quantity", "trigger_price", "symbol", "side", "sl")):
                raise ValueError("unsupported partial/changed fill")
            checks = {"gross_pnl_usd": geometry["gross_pnl_usd"], "initial_risk_usd": geometry["initial_risk_usd"],
                      "total_cost_usd": ledger["total_cost_usd"], "net_r": ledger["net_r"]}
            if any(value is None or finite_cash(p[key]) is None or not math.isclose(value, p[key], rel_tol=1e-9, abs_tol=1e-10) for key, value in checks.items()):
                raise ValueError("inconsistent canonical cash components")
            expected = ledger["net_pnl_usd"]
            if expected is None or finite_cash(p["net_pnl_usdt"]) is None or not math.isclose(expected, p["net_pnl_usdt"], rel_tol=1e-9, abs_tol=1e-10):
                raise ValueError("inconsistent cash")
            net[pid] = expected
        except (ValueError, TypeError, ZeroDivisionError):
            gaps.append("CANONICAL_CLOSURE_CASH_INCOMPLETE")
    times = list(range(identity.start_ms, identity.end_ms, identity.interval_ms)) + [identity.end_ms]
    for at in times:
        inventory = []
        for pid, p in sorted(starts.items()):
            # Opening boundary precedes all in-window events; closing boundary
            # excludes events with an effective label exactly at end.
            opened = p["timestamp"] < at
            closed = pid in ends and ends[pid]["timestamp"] < at
            if not opened or closed:
                continue
            index = bisect_right(indexes.get(p["symbol"], []), at - identity.interval_ms) - 1
            bar = source[p["symbol"]][index] if index >= 0 else None
            mark = None if bar is None else finite_cash(bar["close"])
            if mark is None or mark <= 0 or at != bar["timestamp"] + identity.interval_ms:
                gaps.append("MISSING_OR_STALE_MARK")
                mark = None
            inventory.append(dict(position_id=pid, symbol=p["symbol"], side=p["side"],
                                  quantity=p["filled_quantity"], fill=p["trigger_price"], mark=mark,
                                  mark_effective_ms=None if bar is None else bar["timestamp"] + identity.interval_ms,
                                  mark_source_hash=None if bar is None else digest(bar)))
        if inventory:
            gaps.append("OPEN_POSITION_ACCRUED_COSTS_UNAVAILABLE")
        closed_ids = [pid for pid, p in ends.items() if p["timestamp"] < at]
        cash = sum_cash([initialization["initial_equity_usdt"], *(net.get(pid) for pid in closed_ids)])
        gross_marks = []
        for position in inventory:
            if position["mark"] is None or finite_cash(position["quantity"]) is None:
                gross_marks.append(None)
            else:
                gross_marks.append(position_cash_geometry(fill=position["fill"], stop=position["fill"],
                    quantity=position["quantity"], exit_price=position["mark"], side=position["side"])["gross_pnl_usd"])
        snapshots.append(dict(effective_ms=at, positions=inventory, settled_cash_usdt=cash,
                              unrealized_gross_usdt=sum_cash(gross_marks),
                              accrued_costs_usdt=None if inventory else 0.0,
                              marked_equity_usdt=None if inventory else cash))
    return snapshots, gaps


def _verify_sql_closure_facts(conn, run_id, source):
    captured = {p["position_id"]: p for p in source if p["status_after"] == "POSITION_CLOSED"}
    for row in conn.execute(text("""SELECT * FROM decision_evidence WHERE run_id=:r
            AND lifecycle_state_after='POSITION_CLOSED'"""), dict(r=run_id)).mappings():
        observation = captured[row["position_id"]]
        proof = json.loads(row["diagnostics_json"])
        for key in ("symbol", "side", "trigger_price", "sl", "close_price"):
            if observation[key] != row[key]:
                raise ValueError("source/SQL closure geometry mismatch")
        for key in ("filled_quantity", "initial_risk_usd", "total_cost_usd", "net_r",
                    "realized_evidence_status", "realized_cost_source", "realized_costs_json"):
            if observation[key] != proof.get(key):
                raise ValueError("source/SQL closure accounting mismatch")
        if observation["gross_pnl_usd"] != proof.get("gross_pnl"):
            raise ValueError("source/SQL closure gross cash mismatch")


def _sql_snapshot_hash(conn, run_id):
    rows = conn.execute(text("SELECT * FROM decision_evidence WHERE run_id=:r ORDER BY evidence_id"), dict(r=run_id)).mappings()
    return digest([dict(row) for row in rows])


def _source_positions(events):
    return sorted([[p["position_id"], p["lifecycle_id"], p["lifecycle_seq"],
                    p["status_after"], p["timestamp"]] for p in events])


def _sql_positions(conn, run_id):
    return [[*row[:-1], _persisted_ms(row[-1])] for row in conn.execute(text("""SELECT position_id,lifecycle_id,lifecycle_seq,
        lifecycle_state_after,timestamp FROM decision_evidence WHERE run_id=:r
        AND lifecycle_state_after LIKE 'POSITION_%'
        ORDER BY position_id,lifecycle_id,lifecycle_seq,lifecycle_state_after,timestamp"""), dict(r=run_id)).all()]


def _persisted_ms(value):
    # decision_evidence owns a TEXT timestamp column containing epoch ms.
    if type(value) is not str or not value.isascii() or not value.isdigit():
        raise ValueError("invalid canonical SQL timestamp")
    return int(value)


def _sql_population(conn, run_id):
    return [(row[0], _persisted_ms(row[1]), row[2]) for row in conn.execute(text("SELECT position_id,timestamp,net_pnl_usdt FROM decision_evidence WHERE run_id=:r AND lifecycle_state_after='POSITION_CLOSED' ORDER BY position_id,timestamp"), dict(r=run_id)).all()]


def validate_source_packet(packet, *, expected_identity_hash, expected_completion_hash):
    """Pins must come from canonical SQL/independent receipt, never this packet."""
    identity = SourceIdentity(**packet["identity"])
    init = packet["initialization"]
    if (init.get("kind") != "SIMULATION_INITIALIZED" or init.get("mode") != "BACKTEST"
            or type(init.get("initial_equity_usdt")) not in (int, float)
            or finite_cash(init["initial_equity_usdt"]) is None or init["initial_equity_usdt"] <= 0
            or init.get("opening_positions") != [] or init.get("effective_ms") != identity.start_ms):
        raise ValueError("invalid attributable source opening")
    if digest(packet["identity"]) != expected_identity_hash or packet["identity_hash"] != expected_identity_hash:
        raise ValueError("source identity mismatch")
    init = packet["initialization"]
    for field, key in (("config_hash", "configuration"), ("strategy_hash", "strategy"),
                       ("cost_hash", "costs"), ("code_hash", "source_files")):
        if digest(init["provenance"][key]) != getattr(identity, field):
            raise ValueError("source provenance mismatch")
    if digest(init) != packet["initialization_hash"] or digest(init["source_candles"]) != packet["identity"]["data_hash"]:
        raise ValueError("source initialization/data mismatch")
    events, done = packet["events"], packet["completion"]
    if len({e["event_key"] for e in events}) != len(events):
        raise ValueError("duplicate source event")
    allowed = {"LIFECYCLE", "CASH_CLOSED"}
    if identity.version == BOUNDARY_VERSION:
        allowed |= {"SNAPSHOT", "COST_ACCRUED", "MODEL_DIAGNOSTIC"}
    if any(e["kind"] not in allowed for e in events):
        raise ValueError("unsupported source event kind")
    if [e["sequence"] for e in events] != list(range(1, len(events) + 1)):
        raise ValueError("source sequence/population mismatch")
    if any(type(e["observed_ms"]) is not int or e["observed_ms"] < packet["initialization_observed_ms"] for e in events):
        raise ValueError("source observation chronology mismatch")
    if done is None:
        if expected_completion_hash is not None:
            raise ValueError("source completion missing")
        return
    if digest(done) != expected_completion_hash or done["source_event_count"] != len(events) or done["source_digest"] != digest(events):
        raise ValueError("source completion population mismatch")
    if done["identity_hash"] != expected_identity_hash or done["initialization_hash"] != packet["initialization_hash"]:
        raise ValueError("source completion scope mismatch")
    if done["status"] not in {"COMPLETE", "INCOMPLETE"} or bool(done["gaps"]) != (done["status"] == "INCOMPLETE"):
        raise ValueError("invalid source completion status")
    if done["coverage_end_ms"] != (packet["identity"]["end_ms"] if done["status"] == "COMPLETE" else None):
        raise ValueError("invalid source completion watermark")
    lifecycle = [e["payload"] for e in events if e["kind"] == "LIFECYCLE"]
    snapshots, gaps = _source_valuations(identity, init, events)
    _qualify_valuations(snapshots, done["gaps"])
    if done["valuations"] != snapshots or not set(gaps).issubset(done["gaps"]):
        raise ValueError("source valuation replay mismatch")
    if done["position_population"] != _source_positions(lifecycle):
        raise ValueError("source position inventory mismatch")
    if any(e["observed_ms"] > done["asof_observed_ms"] for e in events):
        raise ValueError("source as-of cutoff mismatch")


def export_source_evidence(conn, run_id, *, expected_identity=None):
    """Read a consistent canonical SQLite snapshot; no bootstrap or mutation."""
    if conn.dialect.name != "sqlite":
        raise ValueError("source evidence currently supports SQLite only")
    if not conn.connection.driver_connection.in_transaction:
        conn.exec_driver_sql("BEGIN")
    row = conn.execute(text("SELECT * FROM backtest_source_runs WHERE run_id=:r"), dict(r=run_id)).mappings().one()
    identity = json.loads(row["identity_json"])
    if expected_identity is not None and identity != asdict(expected_identity):
        raise ValueError("source identity mismatch")
    events, completion, completion_hash = [], None, None
    for e in conn.execute(text("SELECT * FROM backtest_source_events WHERE run_id=:r ORDER BY sequence"), dict(r=run_id)).mappings():
        payload = json.loads(e["payload_json"])
        if digest(payload) != e["payload_hash"]:
            raise ValueError("source event hash mismatch")
        entry = dict(sequence=e["sequence"], kind=e["kind"], effective_ms=e["effective_ms"],
                     observed_ms=e["observed_ms"], event_key=e["event_key"], payload=payload)
        if e["kind"] == "FINAL":
            completion, completion_hash = payload, e["payload_hash"]
        else:
            events.append(entry)
    packet = dict(identity=identity, identity_hash=row["identity_hash"], initialization=json.loads(row["initialization_json"]),
                  initialization_hash=row["initialization_hash"], initialization_observed_ms=row["observed_ms"],
                  events=events, completion=completion, completion_hash=completion_hash,
                  status="INTERRUPTED" if completion is None else completion["status"])
    validate_source_packet(packet, expected_identity_hash=row["identity_hash"], expected_completion_hash=completion_hash)
    if completion and _sql_snapshot_hash(conn, run_id) != completion["sql_snapshot_hash"]:
        raise ValueError("canonical SQL snapshot drift")
    if completion and _sql_positions(conn, run_id) != completion["position_population"]:
        raise ValueError("canonical SQL position population mismatch")
    if completion and [list(row) for row in _sql_population(conn, run_id)] != completion["closure_population"]:
        raise ValueError("canonical SQL closure population mismatch")
    return packet
