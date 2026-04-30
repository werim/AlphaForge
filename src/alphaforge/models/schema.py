import enum
from datetime import datetime

from sqlalchemy import JSON, BigInteger, CheckConstraint, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from alphaforge.db.base import Base


JSONVariant = JSON


class MarketType(str, enum.Enum):
    USDT_M = "USDT_M"
    COIN_M = "COIN_M"


class Decision(str, enum.Enum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"


class Side(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class ExchangeSymbol(Base):
    __tablename__ = "exchange_symbols"
    __table_args__ = (
        UniqueConstraint("venue", "market_type", "symbol", name="uq_exchange_symbol"),
        CheckConstraint("price_precision >= 0"),
        CheckConstraint("quantity_precision >= 0"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    market_type: Mapped[MarketType] = mapped_column(Enum(MarketType), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    pair: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_type: Mapped[str] = mapped_column(String(32), nullable=False)
    base_asset: Mapped[str] = mapped_column(String(32), nullable=False)
    quote_asset: Mapped[str] = mapped_column(String(32), nullable=False)
    margin_asset: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    onboard_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    price_precision: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_precision: Mapped[int] = mapped_column(Integer, nullable=False)
    tick_size: Mapped[float] = mapped_column(Numeric(20, 10), nullable=False)
    step_size: Mapped[float] = mapped_column(Numeric(20, 10), nullable=False)
    min_qty: Mapped[float] = mapped_column(Numeric(20, 10), nullable=False)
    min_notional: Mapped[float] = mapped_column(Numeric(20, 10), nullable=False)
    contract_size: Mapped[float] = mapped_column(Numeric(20, 10), nullable=False)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    raw_exchange_info_json: Mapped[dict] = mapped_column(JSONVariant, nullable=False)


class ConfigSnapshot(Base):
    __tablename__ = "config_snapshots"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    component: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONVariant, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RuntimeState(Base):
    __tablename__ = "runtime_state"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    value: Mapped[dict] = mapped_column(JSONVariant, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Candles(Base):
    __tablename__ = "candles"
    __table_args__ = (UniqueConstraint("symbol_id", "timeframe", "open_time", name="uq_candles_symbol_time"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("exchange_symbols.id"), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    close_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[float] = mapped_column(Numeric(20, 10), nullable=False)
    high: Mapped[float] = mapped_column(Numeric(20, 10), nullable=False)
    low: Mapped[float] = mapped_column(Numeric(20, 10), nullable=False)
    close: Mapped[float] = mapped_column(Numeric(20, 10), nullable=False)
    volume: Mapped[float] = mapped_column(Numeric(28, 10), nullable=False)


class IndicatorSnapshots(Base):
    __tablename__ = "indicator_snapshots"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    candle_id: Mapped[int] = mapped_column(ForeignKey("candles.id"), nullable=False)
    indicators: Mapped[dict] = mapped_column(JSONVariant, nullable=False)


class RegimeStates(Base):
    __tablename__ = "regime_states"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("exchange_symbols.id"), nullable=False)
    regime: Mapped[str] = mapped_column(String(32), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StrategySignals(Base):
    __tablename__ = "strategy_signals"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("exchange_symbols.id"), nullable=False)
    regime_state_id: Mapped[int | None] = mapped_column(ForeignKey("regime_states.id"))
    signal: Mapped[str] = mapped_column(String(32), nullable=False)
    signal_payload: Mapped[dict] = mapped_column(JSONVariant, nullable=False)


class SelectorDecisions(Base):
    __tablename__ = "selector_decisions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    strategy_signal_id: Mapped[int] = mapped_column(ForeignKey("strategy_signals.id"), nullable=False)
    decision: Mapped[Decision] = mapped_column(Enum(Decision), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)


class OrderIntents(Base):
    __tablename__ = "order_intents"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    selector_decision_id: Mapped[int] = mapped_column(ForeignKey("selector_decisions.id"), nullable=False)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("exchange_symbols.id"), nullable=False)
    side: Mapped[Side] = mapped_column(Enum(Side), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(20, 10), nullable=False)
    price: Mapped[float | None] = mapped_column(Numeric(20, 10))


class RiskDecisions(Base):
    __tablename__ = "risk_decisions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_intent_id: Mapped[int] = mapped_column(ForeignKey("order_intents.id"), nullable=False)
    decision: Mapped[Decision] = mapped_column(Enum(Decision), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)


class TradeLifecycleEvents(Base):
    __tablename__ = "trade_lifecycle_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_intent_id: Mapped[int] = mapped_column(ForeignKey("order_intents.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    event_payload: Mapped[dict] = mapped_column(JSONVariant, nullable=False)


class Positions(Base):
    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("exchange_symbols.id"), nullable=False)
    side: Mapped[Side] = mapped_column(Enum(Side), nullable=False)
    size: Mapped[float] = mapped_column(Numeric(20, 10), nullable=False)


class Orders(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_intent_id: Mapped[int] = mapped_column(ForeignKey("order_intents.id"), nullable=False)
    external_order_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24), nullable=False)


class ClosedTrades(Base):
    __tablename__ = "closed_trades"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"), nullable=False)
    pnl: Mapped[float] = mapped_column(Numeric(20, 10), nullable=False)


class RejectionAudit(Base):
    __tablename__ = "rejection_audit"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_intent_id: Mapped[int] = mapped_column(ForeignKey("order_intents.id"), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONVariant, nullable=False)


class OrderDecisionAudit(Base):
    __tablename__ = "order_decision_audit"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    risk_decision_id: Mapped[int] = mapped_column(ForeignKey("risk_decisions.id"), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONVariant, nullable=False)


class StrategyPerformance(Base):
    __tablename__ = "strategy_performance"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    strategy_name: Mapped[str] = mapped_column(String(64), nullable=False)
    metrics: Mapped[dict] = mapped_column(JSONVariant, nullable=False)


class RegimePerformance(Base):
    __tablename__ = "regime_performance"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    regime: Mapped[str] = mapped_column(String(32), nullable=False)
    metrics: Mapped[dict] = mapped_column(JSONVariant, nullable=False)


class OptimizerTrials(Base):
    __tablename__ = "optimizer_trials"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trial_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    params: Mapped[dict] = mapped_column(JSONVariant, nullable=False)


class OptimizerResults(Base):
    __tablename__ = "optimizer_results"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trial_id: Mapped[int] = mapped_column(ForeignKey("optimizer_trials.id"), nullable=False)
    result: Mapped[dict] = mapped_column(JSONVariant, nullable=False)
