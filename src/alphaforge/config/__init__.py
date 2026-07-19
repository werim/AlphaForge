from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from alphaforge.config_registry import decision_filter_config, effective_config_values
from alphaforge.env_contract import bootstrap_environment, resolve_binance_environment


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
    return f"sqlite+pysqlite:///{(Path.cwd() / 'data' / 'runtime' / 'alphaforge_runtime.db').resolve()}"


def _resolve_database_url(env: Mapping[str, str]) -> str:
    database_url = _alias(env, "ALPHAFORGE_DATABASE_URL", "ALPHAFORGE_DB_URL", "DATABASE_URL") or _default_runtime_database_url()
    if not database_url.startswith("sqlite") or ":memory:" in database_url:
        return database_url
    prefix = "sqlite+pysqlite:///" if database_url.startswith("sqlite+pysqlite:///") else "sqlite:///"
    raw_path = database_url.removeprefix(prefix)
    return f"{prefix}{Path(raw_path).expanduser().resolve()}"

@dataclass(slots=True)
class RuntimeSettings:
    execution_mode: str = "PAPER"
    min_signal_score: float = 0.62
    scan_interval_sec: float = 1.0
    heartbeat_interval_sec: float = 30.0
    max_symbols_per_scan: int = 5
    max_reject_log_entries: int = 1000
    max_concurrent_positions: int = 3
    symbol_cooldown_sec: float = 120.0
    max_notional_exposure: float = 100_000.0
    max_symbol_notional: float = 50_000.0
    stale_market_data_sec: float = 15.0
    min_rr: float = 1.20
    min_effective_rr: float = 1.10
    max_spread_pct: float = 0.0025
    max_expected_slippage_pct: float = 0.0020
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
    stop_too_wide_hard_reject: bool = True
    stop_too_wide_soft_score_min: float = 9.0
    stop_too_wide_soft_effective_rr_min: float = 1.75
    stop_too_wide_max_risk_scale: float = 0.50
    stop_too_wide_extreme_mult: float = 1.50
    max_latency_ms: int = 2500
    global_kill_switch: bool = False
    require_live_qualification: bool = True
    enable_shadow_mode: bool = False
    enable_canary_mode: bool = False
    operator_live_acknowledged: bool = False
    reconciliation_interval_sec: float = 5.0
    reconciliation_timeout_sec: float = 2.0
    require_exchange_connectivity_for_live: bool = True
    required_live_exchanges: tuple[str, ...] = ("binance",)
    exchange_connectivity_timeout_sec: float = 2.0
    enable_binance_readonly_reconciliation: bool = False
    binance_reconciliation_recv_window_ms: int = 5000
    binance_reconciliation_trade_lookback_ms: int = 3_600_000

@dataclass(slots=True)
class BinanceSettings:
    base_url: str = "https://fapi.binance.com"
    ws_url: str = "wss://fstream.binance.com"
    environment: str = "production"
    resolution_source: str = "default"
    default_quote_asset: str = "USDT"
    default_market_type: str = "USD_M"
    recv_window_ms: int = 5000
    request_timeout_sec: float = 2.0

@dataclass(slots=True)
class HyperliquidSettings:
    api_url: str = "https://api.hyperliquid.xyz"

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
        min_signal_score=val("ALPHAFORGE_MIN_SIGNAL_SCORE"),
        scan_interval_sec=_float_env(env, "ALPHAFORGE_SCAN_INTERVAL_SEC", 1.0),
        heartbeat_interval_sec=_float_env(env, "ALPHAFORGE_HEARTBEAT_INTERVAL_SEC", 30.0),
        max_symbols_per_scan=_int_env(env, "ALPHAFORGE_MAX_SYMBOLS_PER_SCAN", 5),
        max_reject_log_entries=_int_env(env, "ALPHAFORGE_MAX_REJECT_LOG_ENTRIES", 1000),
        max_concurrent_positions=int(_alias(env, "ALPHAFORGE_MAX_CONCURRENT_POSITIONS", "ALPHAFORGE_MAX_OPEN_POSITIONS") or "3"),
        symbol_cooldown_sec=val("ALPHAFORGE_SYMBOL_COOLDOWN_SEC"),
        max_notional_exposure=_float_env(env, "ALPHAFORGE_MAX_NOTIONAL_EXPOSURE", 100_000.0),
        max_symbol_notional=_float_env(env, "ALPHAFORGE_MAX_SYMBOL_NOTIONAL", 50_000.0),
        stale_market_data_sec=_float_env(env, "ALPHAFORGE_STALE_MARKET_DATA_SEC", 15.0),
        min_rr=val("ALPHAFORGE_MIN_RR"),
        min_effective_rr=val("MIN_EFFECTIVE_RR"),
        max_spread_pct=val("ALPHAFORGE_MAX_SPREAD_PCT"),
        max_expected_slippage_pct=val("ALPHAFORGE_MAX_EXPECTED_SLIPPAGE_PCT"),
        max_abs_funding_rate_pct=val("ALPHAFORGE_MAX_ABS_FUNDING_RATE_PCT"),
        min_liquidity_usd=val("MIN_LIQUIDITY_USD"),
        max_trades_global_per_day=val("ALPHAFORGE_MAX_TRADES_GLOBAL_PER_DAY"),
        max_trades_symbol_per_day=val("ALPHAFORGE_MAX_TRADES_SYMBOL_PER_DAY"),
        min_sl_pct=val("ALPHAFORGE_MIN_SL_PCT"), max_sl_pct=val("ALPHAFORGE_MAX_SL_PCT"),
        min_atr_pct=val("ALPHAFORGE_MIN_ATR_PCT"), max_atr_pct=val("ALPHAFORGE_MAX_ATR_PCT"),
        block_unknown_expectancy=val("ALPHAFORGE_BLOCK_UNKNOWN_EXPECTANCY"), block_chop_market=val("ALPHAFORGE_BLOCK_CHOP_MARKET"),
        require_regime_alignment=val("ALPHAFORGE_REQUIRE_REGIME_ALIGNMENT"), stop_too_wide_hard_reject=val("ALPHAFORGE_STOP_TOO_WIDE_HARD_REJECT"),
        stop_too_wide_soft_score_min=val("ALPHAFORGE_STOP_TOO_WIDE_SOFT_SCORE_MIN"), stop_too_wide_soft_effective_rr_min=val("ALPHAFORGE_STOP_TOO_WIDE_SOFT_EFFECTIVE_RR_MIN"),
        stop_too_wide_max_risk_scale=val("ALPHAFORGE_STOP_TOO_WIDE_MAX_RISK_SCALE"), stop_too_wide_extreme_mult=val("ALPHAFORGE_STOP_TOO_WIDE_EXTREME_MULT"),
        max_latency_ms=val("ALPHAFORGE_MAX_LATENCY_MS"),
        global_kill_switch=val("ALPHAFORGE_GLOBAL_KILL_SWITCH"),
        require_live_qualification=val("ALPHAFORGE_REQUIRE_LIVE_QUALIFICATION"),
        enable_shadow_mode=_bool_env(env, "ALPHAFORGE_ENABLE_SHADOW_MODE", False),
        enable_canary_mode=_bool_env(env, "ALPHAFORGE_ENABLE_CANARY_MODE", False),
        operator_live_acknowledged=_bool_env(env, "ALPHAFORGE_OPERATOR_LIVE_ACKNOWLEDGED", False),
        reconciliation_interval_sec=_float_env(env, "ALPHAFORGE_RECONCILIATION_INTERVAL_SEC", 5.0),
        reconciliation_timeout_sec=_float_env(env, "ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC", 2.0),
        require_exchange_connectivity_for_live=_bool_env(env, "ALPHAFORGE_REQUIRE_EXCHANGE_CONNECTIVITY_FOR_LIVE", True),
        required_live_exchanges=_comma_list(_clean_env_value(env.get("ALPHAFORGE_REQUIRED_LIVE_EXCHANGES")), ("binance",)),
        exchange_connectivity_timeout_sec=_float_env(env, "ALPHAFORGE_EXCHANGE_CONNECTIVITY_TIMEOUT_SEC", 2.0),
        enable_binance_readonly_reconciliation=_bool_env(env, "ALPHAFORGE_ENABLE_BINANCE_READONLY_RECONCILIATION", False),
        binance_reconciliation_recv_window_ms=int(val("BINANCE_RECV_WINDOW_MS")),
        binance_reconciliation_trade_lookback_ms=_int_env(env, "ALPHAFORGE_BINANCE_RECONCILIATION_TRADE_LOOKBACK_MS", 3_600_000),
    )
    binance = BinanceSettings(
        base_url=resolved_binance.rest_base_url,
        ws_url=resolved_binance.ws_base_url,
        environment=resolved_binance.environment,
        resolution_source=resolved_binance.resolution_source,
        default_quote_asset=str(val("BINANCE_DEFAULT_QUOTE_ASSET")).upper(),
        default_market_type=str(val("BINANCE_DEFAULT_MARKET_TYPE")).upper(),
        recv_window_ms=int(val("BINANCE_RECV_WINDOW_MS")),
        request_timeout_sec=float(val("BINANCE_REQUEST_TIMEOUT_SEC")),
    )
    if binance.default_market_type != "USD_M":
        raise ValueError("BINANCE_DEFAULT_MARKET_TYPE must be USD_M")
    exchange = ExchangeSettings(
        timeout_sec=float(val("BINANCE_REQUEST_TIMEOUT_SEC")),
        binance=binance,
        hyperliquid=HyperliquidSettings(api_url=_string_env(env, "HYPERLIQUID_API_URL", "https://api.hyperliquid.xyz")),
    )
    return AlphaForgeConfig(
        runtime=runtime,
        exchange=exchange,
        binance=binance,
        backtest=BacktestSettings(
            top_n=val("ALPHAFORGE_BACKTEST_TOP_N"),
            timeframe=val("ALPHAFORGE_BACKTEST_TIMEFRAME"),
            output_dir=_string_env(env, "ALPHAFORGE_BACKTEST_OUTPUT_DIR", "data/backtest"),
            initial_balance=_float_env(env, "ALPHAFORGE_BACKTEST_INITIAL_BALANCE", 1000.0),
            risk_pct=_float_env(env, "ALPHAFORGE_BACKTEST_RISK_PCT", 1.0),
            max_trades=val("ALPHAFORGE_BACKTEST_MAX_TRADES"),
            max_accepted_trades_per_day=val("ALPHAFORGE_BACKTEST_MAX_ACCEPTED_TRADES_PER_DAY"),
            max_symbol_trades_per_day=val("ALPHAFORGE_BACKTEST_MAX_SYMBOL_TRADES_PER_DAY"),
            use_execution_costs=val("ALPHAFORGE_BACKTEST_USE_EXECUTION_COSTS"),
            export_config_snapshot=val("ALPHAFORGE_BACKTEST_EXPORT_CONFIG_SNAPSHOT"),
            days=val("ALPHAFORGE_BACKTEST_LAST_N_DAYS"),
            filter_switches=BacktestFilterSwitches(
                low_score_enabled=_bool_env(env, "ALPHAFORGE_BACKTEST_FILTER_LOW_SCORE_ENABLED", True),
                too_choppy_enabled=_bool_env(env, "ALPHAFORGE_BACKTEST_FILTER_TOO_CHOPPY_ENABLED", True),
                weak_trend_no_range_enabled=_bool_env(env, "ALPHAFORGE_BACKTEST_FILTER_WEAK_TREND_NO_RANGE_ENABLED", True),
                stop_too_wide_enabled=_bool_env(env, "ALPHAFORGE_BACKTEST_FILTER_STOP_TOO_WIDE_ENABLED", True),
                rr_too_low_enabled=_bool_env(env, "ALPHAFORGE_BACKTEST_FILTER_RR_TOO_LOW_ENABLED", True),
                daily_symbol_trade_limit_enabled=_bool_env(env, "ALPHAFORGE_BACKTEST_FILTER_DAILY_SYMBOL_TRADE_LIMIT_ENABLED", True),
                regime_mismatch_enabled=_bool_env(env, "ALPHAFORGE_BACKTEST_FILTER_REGIME_MISMATCH_ENABLED", True),
                panic_conditions_enabled=_bool_env(env, "ALPHAFORGE_BACKTEST_FILTER_PANIC_CONDITIONS_ENABLED", True),
            ),
        ),
        persistence=PersistenceSettings(database_url=_resolve_database_url(env), enabled=_bool_env(env, "ALPHAFORGE_PERSISTENCE_ENABLED", True)),
        logging=LoggingSettings(level=_string_env(env, "ALPHAFORGE_LOG_LEVEL", "INFO")),
    )
