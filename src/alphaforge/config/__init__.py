from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from alphaforge.config_registry import decision_filter_config, effective_config_values, effective_config_subset
from alphaforge.env_contract import bootstrap_environment, dotenv_status, resolve_binance_environment
from alphaforge.database_defaults import resolve_runtime_database_url


def _clean_env_value(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    if "#" in value:
        value = value.split("#", 1)[0].strip()
    return value or None


def _string_env(env: Mapping[str, str], name: str, default: str) -> str:
    raw = _clean_env_value(env.get(name))
    return default if raw is None else raw


def _float_env(env: Mapping[str, str], name: str, default: float) -> float:
    raw = _clean_env_value(env.get(name))
    return default if raw is None else float(raw)


def _int_env(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _clean_env_value(env.get(name))
    return default if raw is None else int(raw)


def _bool_env(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _clean_env_value(env.get(name))
    return default if raw is None else raw.lower() not in {"0", "false", "no", "off", ""}


def _alias(env: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        raw = _clean_env_value(env.get(name))
        if raw is not None:
            return raw
    return None


def _comma_list(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    items = tuple(v.strip() for v in value.split(",") if v.strip())
    return items or default


def _default_runtime_database_url() -> str:
    return resolve_runtime_database_url({})


def _resolve_database_url(env: Mapping[str, str]) -> str:
    return resolve_runtime_database_url(env)

@dataclass(slots=True)
class RuntimeSettings:
    execution_mode: str = "PAPER"
    paper_enabled: bool = True
    live_enabled: bool = False
    min_signal_score: float = 0.62
    scan_interval_sec: float = 1.0
    heartbeat_interval_sec: float = 30.0
    reject_forward_horizon_bars: int = 240
    reject_resolver_interval_sec: float = 60.0
    max_symbols_per_scan: int = 5
    max_reject_log_entries: int = 1000
    max_concurrent_positions: int = 3
    symbol_cooldown_sec: float = 120.0
    max_notional_exposure: float = 100_000.0
    max_symbol_notional: float = 50_000.0
    max_daily_loss_pct: float = 0.03
    stale_market_data_sec: float = 15.0
    min_rr: float = 1.20
    min_effective_rr: float = 1.10
    max_spread_pct: float = 0.0025
    max_expected_slippage_pct: float = 0.0020
    paper_fee_bps: float = 4.0
    paper_execution_latency_ms: float = 50.0
    market_data_base_url: str = "https://fapi.binance.com"
    regime_timeframe: str = "1h"
    setup_timeframe: str = "15m"
    execution_timeframe: str = "1m"
    mtf_guided_signal_generation_enabled: bool = True
    regime_direction_threshold: float = 0.0005
    setup_direction_threshold: float = 0.0005
    execution_direction_threshold: float = 0.0005
    paper_decision_timeframe: str = "1m"  # deprecated compatibility mirror
    max_abs_funding_rate_pct: float = 0.0010
    min_liquidity_usd: float = 5_000_000.0
    max_trades_global_per_day: int = 10
    max_trades_symbol_per_day: int = 2
    min_sl_pct: float = 0.15
    max_sl_pct: float = 1.5
    min_atr_pct: float = 0.25
    max_atr_pct: float = 3.0
    block_unknown_expectancy: bool = True
    block_chop_market: bool = True
    require_regime_alignment: bool = True
    enable_orderbook_filter: bool = False
    stop_too_wide_hard_reject: bool = True
    stop_too_wide_soft_score_min: float = 9.0
    stop_too_wide_soft_effective_rr_min: float = 1.75
    stop_too_wide_max_risk_scale: float = 0.50
    stop_too_wide_extreme_mult: float = 1.50
    max_latency_ms: int = 2500
    global_kill_switch: bool = False
    require_live_qualification: bool = True
    enable_shadow_mode: bool = False
    agent_graph_enabled: bool = False
    agent_graph_shadow: bool = True
    agent_graph_max_steps: int = 12
    agent_graph_max_reflection_retries: int = 1
    agent_graph_stage_timeout_seconds: float = 5.0
    agent_graph_persist_traces: bool = True
    agent_graph_max_pending_runs: int = 64
    agent_graph_database_url: str = "sqlite+pysqlite:///data/runtime/alphaforge_agent_shadow.db"
    enable_canary_mode: bool = False
    operator_live_acknowledged: bool = False
    allow_live_orders: bool = False
    reconciliation_interval_sec: float = 5.0
    reconciliation_timeout_sec: float = 2.0
    require_exchange_connectivity_for_live: bool = True
    required_live_exchanges: tuple[str, ...] = ("binance",)
    exchange_connectivity_timeout_sec: float = 2.0
    enable_binance_readonly_reconciliation: bool = False
    binance_reconciliation_recv_window_ms: int = 5000
    binance_reconciliation_trade_lookback_ms: int = 3_600_000
    reconciliation_position_epsilon: str = "0.00000001"
    reconciliation_max_fill_symbols: int = 10

@dataclass(slots=True)
class BinanceSettings:
    base_url: str = "https://fapi.binance.com"
    market_data_base_url: str = "https://fapi.binance.com"
    ws_url: str = "wss://fstream.binance.com"
    environment: str = "production"
    resolution_source: str = "default"
    default_quote_asset: str = "USDT"
    default_market_type: str = "USD_M"
    recv_window_ms: int = 5000
    request_timeout_sec: float = 2.0


def normalize_binance_market_type(value: object) -> str:
    normalized = str(value or "").strip().upper().replace("-", "_")
    if normalized in {"USD_M", "USDT_M"}:
        return "USD_M"
    raise ValueError("BINANCE_DEFAULT_MARKET_TYPE unsupported market type; expected USD_M/USDT_M")


@dataclass(frozen=True, slots=True)
class ReconciliationSettings:
    base_url: str
    environment: str
    recv_window_ms: int
    timeout_sec: float
    trade_lookback_ms: int
    position_epsilon: str
    max_fill_symbols: int
    api_key: str
    api_secret: str
    sources: Mapping[str, str]


def load_reconciliation_settings(*, env: Mapping[str, str] | None = None) -> ReconciliationSettings:
    env = os.environ if env is None else env
    names = ("ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC", "ALPHAFORGE_BINANCE_RECV_WINDOW_MS",
             "ALPHAFORGE_BINANCE_RECONCILIATION_TRADE_LOOKBACK_MS", "ALPHAFORGE_RECONCILIATION_POSITION_EPSILON",
             "ALPHAFORGE_RECONCILIATION_MAX_FILL_SYMBOLS", "BINANCE_API_KEY", "BINANCE_API_SECRET",
             "BINANCE_ENVIRONMENT", "BINANCE_BASE_URL", "BINANCE_WS_URL")
    values = effective_config_subset(names, env=env, fail_on_alias_conflict=True, include_files=False)
    val = lambda name: values[name]["value"]
    endpoint_env = {name: str(val(name)) for name in ("BINANCE_ENVIRONMENT", "BINANCE_BASE_URL", "BINANCE_WS_URL") if val(name)}
    environment_source = str(values["BINANCE_ENVIRONMENT"]["source"])
    if environment_source.startswith("alias (BINANCE_TESTNET)"):
        endpoint_env["BINANCE_TESTNET"] = endpoint_env.pop("BINANCE_ENVIRONMENT")
    resolved = resolve_binance_environment(endpoint_env, require_websocket=False)
    from decimal import Decimal, InvalidOperation
    try:
        epsilon = Decimal(str(val("ALPHAFORGE_RECONCILIATION_POSITION_EPSILON")))
        if not epsilon.is_finite() or epsilon < 0:
            raise InvalidOperation
    except InvalidOperation:
        raise ValueError("ALPHAFORGE_RECONCILIATION_POSITION_EPSILON invalid decimal") from None
    loaded_keys = set(dotenv_status().keys_loaded) if env is os.environ else set()
    sources = {}
    for name, item in values.items():
        source = str(item["source"]).upper()
        aliases = item["setting"].deprecated_aliases
        if name in loaded_keys or any(alias in loaded_keys for alias in aliases):
            source = "DOTENV"
        sources[name] = source
    return ReconciliationSettings(
        base_url=resolved.rest_base_url, environment=resolved.environment,
        recv_window_ms=int(val("ALPHAFORGE_BINANCE_RECV_WINDOW_MS")),
        timeout_sec=float(val("ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC")),
        trade_lookback_ms=int(val("ALPHAFORGE_BINANCE_RECONCILIATION_TRADE_LOOKBACK_MS")),
        position_epsilon=str(epsilon),
        max_fill_symbols=int(val("ALPHAFORGE_RECONCILIATION_MAX_FILL_SYMBOLS")),
        api_key=str(val("BINANCE_API_KEY")), api_secret=str(val("BINANCE_API_SECRET")), sources=sources)

@dataclass(slots=True)
class HyperliquidSettings:
    api_url: str = "https://api.hyperliquid.xyz"
    enabled: bool = True

@dataclass(slots=True)
class ExchangeSettings:
    timeout_sec: float = 2.0
    binance: BinanceSettings = field(default_factory=BinanceSettings)
    hyperliquid: HyperliquidSettings = field(default_factory=HyperliquidSettings)

@dataclass(slots=True)
class BacktestFilterSwitches:
    low_score_enabled: bool = True
    too_choppy_enabled: bool = True
    weak_trend_no_range_enabled: bool = True
    stop_too_wide_enabled: bool = True
    rr_too_low_enabled: bool = True
    daily_symbol_trade_limit_enabled: bool = True
    regime_mismatch_enabled: bool = True
    panic_conditions_enabled: bool = True

    def disabled_filters(self) -> tuple[str, ...]:
        mapping = {
            "LOW_SCORE": self.low_score_enabled,
            "TOO_CHOPPY": self.too_choppy_enabled,
            "WEAK_TREND_AND_NO_RANGE_EDGE": self.weak_trend_no_range_enabled,
            "STOP_TOO_WIDE": self.stop_too_wide_enabled,
            "RR_TOO_LOW": self.rr_too_low_enabled,
            "DAILY_SYMBOL_TRADE_LIMIT": self.daily_symbol_trade_limit_enabled,
            "REGIME_MISMATCH": self.regime_mismatch_enabled,
            "PANIC_CONDITIONS": self.panic_conditions_enabled,
        }
        return tuple(reason for reason, enabled in mapping.items() if not enabled)

@dataclass(slots=True)
class BacktestSettings:
    top_n: int = 100
    timeframe: str = "1m"
    output_dir: str = "data/backtest"
    initial_balance: float = 1000.0
    risk_pct: float = 1.0
    max_trades: int = 0
    max_accepted_trades_per_day: int = 0
    max_symbol_trades_per_day: int = 0
    use_execution_costs: bool = True
    export_config_snapshot: bool = True
    days: int = 7
    filter_switches: BacktestFilterSwitches = field(default_factory=BacktestFilterSwitches)

@dataclass(slots=True)
class RiskSettings: pass
@dataclass(slots=True)
class ExecutionSettings: pass
@dataclass(slots=True)
class PersistenceSettings:
    database_url: str = ""
    enabled: bool = True
@dataclass(slots=True)
class LoggingSettings:
    level: str = "INFO"
@dataclass(slots=True)
class NotificationSettings:
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
@dataclass(slots=True)
class FeatureFlags: pass
@dataclass(slots=True)
class AppConfig: pass

@dataclass(slots=True)
class AlphaForgeConfig:
    app: AppConfig = field(default_factory=AppConfig)
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)
    exchange: ExchangeSettings = field(default_factory=ExchangeSettings)
    binance: BinanceSettings = field(default_factory=BinanceSettings)
    hyperliquid: HyperliquidSettings = field(default_factory=HyperliquidSettings)
    backtest: BacktestSettings = field(default_factory=BacktestSettings)
    risk: RiskSettings = field(default_factory=RiskSettings)
    execution: ExecutionSettings = field(default_factory=ExecutionSettings)
    persistence: PersistenceSettings = field(default_factory=PersistenceSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    notifications: NotificationSettings = field(default_factory=NotificationSettings)
    feature_flags: FeatureFlags = field(default_factory=FeatureFlags)



def runtime_filter_config(runtime: RuntimeSettings, *, mode: str | None = None) -> dict[str, object]:
    cfg = decision_filter_config(mode or runtime.execution_mode)
    cfg.update({
        "MIN_TRADE_SCORE": runtime.min_signal_score,
        "MIN_RR": runtime.min_rr,
        "MIN_EFFECTIVE_RR": runtime.min_effective_rr,
        "MAX_SPREAD_PCT": runtime.max_spread_pct,
        "MAX_EXPECTED_SLIPPAGE_PCT": runtime.max_expected_slippage_pct,
        "MIN_SL_PCT": getattr(runtime, "min_sl_pct", 0.15),
        "MAX_SL_PCT": getattr(runtime, "max_sl_pct", 1.5),
        "MIN_ATR_PCT": getattr(runtime, "min_atr_pct", 0.25),
        "MAX_ATR_PCT": getattr(runtime, "max_atr_pct", 3.0),
        "BLOCK_UNKNOWN_EXPECTANCY": getattr(runtime, "block_unknown_expectancy", True),
        "BLOCK_CHOP_MARKET": getattr(runtime, "block_chop_market", True),
        "REQUIRE_REGIME_ALIGNMENT": getattr(runtime, "require_regime_alignment", True),
        "ENABLE_ORDERBOOK_FILTER": getattr(runtime, "enable_orderbook_filter", False),
        "STOP_TOO_WIDE_HARD_REJECT": getattr(runtime, "stop_too_wide_hard_reject", True),
        "STOP_TOO_WIDE_SOFT_SCORE_MIN": getattr(runtime, "stop_too_wide_soft_score_min", 9.0),
        "STOP_TOO_WIDE_SOFT_EFFECTIVE_RR_MIN": getattr(runtime, "stop_too_wide_soft_effective_rr_min", 1.75),
        "STOP_TOO_WIDE_MAX_RISK_SCALE": getattr(runtime, "stop_too_wide_max_risk_scale", 0.50),
        "STOP_TOO_WIDE_EXTREME_MULT": getattr(runtime, "stop_too_wide_extreme_mult", 1.50),
        "SYMBOL_COOLDOWN_MINUTES": runtime.symbol_cooldown_sec / 60.0,
        "MAX_TRADES_PER_SYMBOL_PER_DAY": getattr(runtime, "max_trades_symbol_per_day", 2),
        "MAX_TRADES_GLOBAL_PER_DAY": getattr(runtime, "max_trades_global_per_day", 10),
        "STALE_MARKET_DATA_SEC": runtime.stale_market_data_sec,
        "MAX_CONCURRENT_POSITIONS": runtime.max_concurrent_positions,
        "MAX_ABS_FUNDING_RATE_PCT": runtime.max_abs_funding_rate_pct,
        "MIN_LIQUIDITY_USD": runtime.min_liquidity_usd,
        "min_volume_24h_usdt": runtime.min_liquidity_usd,
        "max_spread_pct": runtime.max_spread_pct,
        "max_abs_funding_rate_pct": runtime.max_abs_funding_rate_pct,
    })
    return cfg

def load_config_from_env() -> AlphaForgeConfig:
    bootstrap_environment()
    env = os.environ
    managed = effective_config_values(env=env)
    val = lambda name: managed[name]["value"]
    resolved_binance = resolve_binance_environment(env)
    runtime = RuntimeSettings(
        execution_mode=str(val("ALPHAFORGE_EXECUTION_MODE")).upper(),
        paper_enabled=val("ALPHAFORGE_ENABLE_PAPER_TRADING"),
        live_enabled=val("ALPHAFORGE_ENABLE_LIVE_TRADING"),
        min_signal_score=val("ALPHAFORGE_MIN_SIGNAL_SCORE"),
        scan_interval_sec=val("ALPHAFORGE_SCAN_INTERVAL_SEC"),
        heartbeat_interval_sec=val("ALPHAFORGE_HEARTBEAT_INTERVAL_SEC"),
        reject_forward_horizon_bars=val("ALPHAFORGE_REJECT_FORWARD_HORIZON_BARS"),
        reject_resolver_interval_sec=val("ALPHAFORGE_REJECT_RESOLVER_INTERVAL_SEC"),
        max_symbols_per_scan=val("ALPHAFORGE_MAX_SYMBOLS_PER_SCAN"),
        max_reject_log_entries=val("ALPHAFORGE_MAX_REJECT_LOG_ENTRIES"),
        max_concurrent_positions=val("ALPHAFORGE_MAX_CONCURRENT_POSITIONS"),
        symbol_cooldown_sec=val("ALPHAFORGE_SYMBOL_COOLDOWN_SEC"),
        max_notional_exposure=val("ALPHAFORGE_MAX_NOTIONAL_EXPOSURE"),
        max_symbol_notional=val("ALPHAFORGE_MAX_SYMBOL_NOTIONAL"),
        max_daily_loss_pct=val("ALPHAFORGE_MAX_DAILY_LOSS_PCT"),
        stale_market_data_sec=val("ALPHAFORGE_STALE_MARKET_DATA_SEC"),
        min_rr=val("ALPHAFORGE_MIN_RR"),
        min_effective_rr=val("MIN_EFFECTIVE_RR"),
        max_spread_pct=val("ALPHAFORGE_MAX_SPREAD_PCT"),
        max_expected_slippage_pct=val("ALPHAFORGE_MAX_EXPECTED_SLIPPAGE_PCT"),
        paper_fee_bps=val("ALPHAFORGE_PAPER_FEE_BPS"),
        paper_execution_latency_ms=val("ALPHAFORGE_PAPER_EXECUTION_LATENCY_MS"),
        market_data_base_url=str(val("ALPHAFORGE_BINANCE_MARKET_DATA_BASE_URL")).rstrip("/"),
        regime_timeframe=val("ALPHAFORGE_REGIME_TIMEFRAME"),
        setup_timeframe=val("ALPHAFORGE_SETUP_TIMEFRAME"),
        execution_timeframe=val("ALPHAFORGE_EXECUTION_TIMEFRAME"),
        mtf_guided_signal_generation_enabled=val("ALPHAFORGE_MTF_GUIDED_SIGNAL_GENERATION_ENABLED"),
        regime_direction_threshold=val("ALPHAFORGE_REGIME_DIRECTION_THRESHOLD"),
        setup_direction_threshold=val("ALPHAFORGE_SETUP_DIRECTION_THRESHOLD"),
        execution_direction_threshold=val("ALPHAFORGE_EXECUTION_DIRECTION_THRESHOLD"),
        paper_decision_timeframe=val("ALPHAFORGE_EXECUTION_TIMEFRAME"),
        max_abs_funding_rate_pct=val("ALPHAFORGE_MAX_ABS_FUNDING_RATE_PCT"),
        min_liquidity_usd=val("MIN_LIQUIDITY_USD"),
        max_trades_global_per_day=val("ALPHAFORGE_MAX_TRADES_GLOBAL_PER_DAY"),
        max_trades_symbol_per_day=val("ALPHAFORGE_MAX_TRADES_SYMBOL_PER_DAY"),
        min_sl_pct=val("ALPHAFORGE_MIN_SL_PCT"), max_sl_pct=val("ALPHAFORGE_MAX_SL_PCT"),
        min_atr_pct=val("ALPHAFORGE_MIN_ATR_PCT"), max_atr_pct=val("ALPHAFORGE_MAX_ATR_PCT"),
        block_unknown_expectancy=val("ALPHAFORGE_BLOCK_UNKNOWN_EXPECTANCY"), block_chop_market=val("ALPHAFORGE_BLOCK_CHOP_MARKET"),
        require_regime_alignment=val("ALPHAFORGE_REQUIRE_REGIME_ALIGNMENT"), stop_too_wide_hard_reject=val("ALPHAFORGE_STOP_TOO_WIDE_HARD_REJECT"),
        enable_orderbook_filter=val("ALPHAFORGE_ENABLE_ORDERBOOK_FILTER"),
        stop_too_wide_soft_score_min=val("ALPHAFORGE_STOP_TOO_WIDE_SOFT_SCORE_MIN"), stop_too_wide_soft_effective_rr_min=val("ALPHAFORGE_STOP_TOO_WIDE_SOFT_EFFECTIVE_RR_MIN"),
        stop_too_wide_max_risk_scale=val("ALPHAFORGE_STOP_TOO_WIDE_MAX_RISK_SCALE"), stop_too_wide_extreme_mult=val("ALPHAFORGE_STOP_TOO_WIDE_EXTREME_MULT"),
        max_latency_ms=val("ALPHAFORGE_MAX_LATENCY_MS"),
        global_kill_switch=val("ALPHAFORGE_GLOBAL_KILL_SWITCH"),
        require_live_qualification=val("ALPHAFORGE_REQUIRE_LIVE_QUALIFICATION"),
        enable_shadow_mode=val("ALPHAFORGE_ENABLE_SHADOW_MODE"),
        agent_graph_enabled=val("ALPHAFORGE_AGENT_GRAPH_ENABLED"),
        agent_graph_shadow=val("ALPHAFORGE_AGENT_GRAPH_SHADOW"),
        agent_graph_max_steps=val("ALPHAFORGE_AGENT_GRAPH_MAX_STEPS"),
        agent_graph_max_reflection_retries=val("ALPHAFORGE_AGENT_GRAPH_MAX_REFLECTION_RETRIES"),
        agent_graph_stage_timeout_seconds=val("ALPHAFORGE_AGENT_GRAPH_STAGE_TIMEOUT_SECONDS"),
        agent_graph_persist_traces=val("ALPHAFORGE_AGENT_GRAPH_PERSIST_TRACES"),
        agent_graph_max_pending_runs=val("ALPHAFORGE_AGENT_GRAPH_MAX_PENDING_RUNS"),
        agent_graph_database_url=val("ALPHAFORGE_AGENT_GRAPH_DATABASE_URL"),
        enable_canary_mode=val("ALPHAFORGE_ENABLE_CANARY_MODE"),
        operator_live_acknowledged=val("ALPHAFORGE_OPERATOR_LIVE_ACKNOWLEDGED"),
        allow_live_orders=val("ALPHAFORGE_ALLOW_LIVE_ORDERS"),
        reconciliation_interval_sec=val("ALPHAFORGE_RECONCILIATION_INTERVAL_SEC"),
        reconciliation_timeout_sec=val("ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC"),
        require_exchange_connectivity_for_live=_bool_env(env, "ALPHAFORGE_REQUIRE_EXCHANGE_CONNECTIVITY_FOR_LIVE", True),
        required_live_exchanges=_comma_list(_clean_env_value(env.get("ALPHAFORGE_REQUIRED_LIVE_EXCHANGES")), ("binance",)),
        exchange_connectivity_timeout_sec=_float_env(env, "ALPHAFORGE_EXCHANGE_CONNECTIVITY_TIMEOUT_SEC", 2.0),
        enable_binance_readonly_reconciliation=val("ALPHAFORGE_ENABLE_BINANCE_READONLY_RECONCILIATION"),
        binance_reconciliation_recv_window_ms=int(val("ALPHAFORGE_BINANCE_RECV_WINDOW_MS")),
        binance_reconciliation_trade_lookback_ms=val("ALPHAFORGE_BINANCE_RECONCILIATION_TRADE_LOOKBACK_MS"),
        reconciliation_position_epsilon=val("ALPHAFORGE_RECONCILIATION_POSITION_EPSILON"),
        reconciliation_max_fill_symbols=val("ALPHAFORGE_RECONCILIATION_MAX_FILL_SYMBOLS"),
    )
    binance = BinanceSettings(
        base_url=resolved_binance.rest_base_url,
        market_data_base_url=str(val("ALPHAFORGE_BINANCE_MARKET_DATA_BASE_URL")).rstrip("/"),
        ws_url=resolved_binance.ws_base_url,
        environment=resolved_binance.environment,
        resolution_source=resolved_binance.resolution_source,
        default_quote_asset=str(val("BINANCE_DEFAULT_QUOTE_ASSET")).upper(),
        default_market_type=normalize_binance_market_type(val("BINANCE_DEFAULT_MARKET_TYPE")),
        recv_window_ms=int(val("ALPHAFORGE_BINANCE_RECV_WINDOW_MS")),
        request_timeout_sec=float(val("BINANCE_REQUEST_TIMEOUT_SEC")),
    )
    exchange = ExchangeSettings(
        timeout_sec=float(val("BINANCE_REQUEST_TIMEOUT_SEC")),
        binance=binance,
        hyperliquid=HyperliquidSettings(api_url=val("HYPERLIQUID_API_URL"), enabled=val("HYPERLIQUID_ENABLED")),
    )
    return AlphaForgeConfig(
        runtime=runtime,
        exchange=exchange,
        binance=binance,
        backtest=BacktestSettings(
            top_n=val("ALPHAFORGE_BACKTEST_TOP_N"),
            timeframe=val("ALPHAFORGE_BACKTEST_TIMEFRAME"),
            output_dir=val("ALPHAFORGE_BACKTEST_OUTPUT_DIR"),
            initial_balance=val("ALPHAFORGE_BACKTEST_INITIAL_BALANCE"),
            risk_pct=val("ALPHAFORGE_BACKTEST_RISK_PCT"),
            max_trades=val("ALPHAFORGE_BACKTEST_MAX_TRADES"),
            max_accepted_trades_per_day=val("ALPHAFORGE_BACKTEST_MAX_ACCEPTED_TRADES_PER_DAY"),
            max_symbol_trades_per_day=val("ALPHAFORGE_BACKTEST_MAX_SYMBOL_TRADES_PER_DAY"),
            use_execution_costs=val("ALPHAFORGE_BACKTEST_USE_EXECUTION_COSTS"),
            export_config_snapshot=val("ALPHAFORGE_BACKTEST_EXPORT_CONFIG_SNAPSHOT"),
            days=val("ALPHAFORGE_BACKTEST_LAST_N_DAYS"),
            filter_switches=BacktestFilterSwitches(
                low_score_enabled=val("ALPHAFORGE_BACKTEST_FILTER_LOW_SCORE_ENABLED"),
                too_choppy_enabled=val("ALPHAFORGE_BACKTEST_FILTER_TOO_CHOPPY_ENABLED"),
                weak_trend_no_range_enabled=val("ALPHAFORGE_BACKTEST_FILTER_WEAK_TREND_NO_RANGE_ENABLED"),
                stop_too_wide_enabled=val("ALPHAFORGE_BACKTEST_FILTER_STOP_TOO_WIDE_ENABLED"),
                rr_too_low_enabled=val("ALPHAFORGE_BACKTEST_FILTER_RR_TOO_LOW_ENABLED"),
                daily_symbol_trade_limit_enabled=val("ALPHAFORGE_BACKTEST_FILTER_DAILY_SYMBOL_TRADE_LIMIT_ENABLED"),
                regime_mismatch_enabled=val("ALPHAFORGE_BACKTEST_FILTER_REGIME_MISMATCH_ENABLED"),
                panic_conditions_enabled=val("ALPHAFORGE_BACKTEST_FILTER_PANIC_CONDITIONS_ENABLED"),
            ),
        ),
        persistence=PersistenceSettings(database_url=_resolve_database_url(env), enabled=val("ALPHAFORGE_PERSISTENCE_ENABLED")),
        logging=LoggingSettings(level=val("ALPHAFORGE_LOG_LEVEL")),
        notifications=NotificationSettings(telegram_enabled=val("ALPHAFORGE_ENABLE_TELEGRAM"), telegram_bot_token=val("TELEGRAM_BOT_TOKEN"), telegram_chat_id=val("TELEGRAM_CHAT_ID")),
    )
