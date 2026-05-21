from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


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
    max_spread_pct: float = 0.0025
    max_abs_funding_rate_pct: float = 0.0010
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

@dataclass(slots=True)
class BinanceSettings:
    base_url: str = "https://api.binance.com"

@dataclass(slots=True)
class HyperliquidSettings:
    api_url: str = "https://api.hyperliquid.xyz"

@dataclass(slots=True)
class ExchangeSettings:
    timeout_sec: float = 2.0
    binance: BinanceSettings = field(default_factory=BinanceSettings)
    hyperliquid: HyperliquidSettings = field(default_factory=HyperliquidSettings)

@dataclass(slots=True)
class BacktestSettings:
    top_n: int = 100
    timeframe: str = "1m"
    output_dir: str = "data/backtest"
    initial_balance: float = 1000.0
    risk_pct: float = 1.0

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


def load_config_from_env() -> AlphaForgeConfig:
    env = os.environ
    runtime = RuntimeSettings(
        execution_mode=(_alias(env, "ALPHAFORGE_EXECUTION_MODE", "EXECUTION_MODE") or "PAPER").upper(),
        min_signal_score=float(_alias(env, "ALPHAFORGE_MIN_SIGNAL_SCORE", "ALPHAFORGE_MIN_ACCEPT_SCORE") or "0.62"),
        scan_interval_sec=_float_env(env, "ALPHAFORGE_SCAN_INTERVAL_SEC", 1.0),
        heartbeat_interval_sec=_float_env(env, "ALPHAFORGE_HEARTBEAT_INTERVAL_SEC", 30.0),
        max_symbols_per_scan=_int_env(env, "ALPHAFORGE_MAX_SYMBOLS_PER_SCAN", 5),
        max_reject_log_entries=_int_env(env, "ALPHAFORGE_MAX_REJECT_LOG_ENTRIES", 1000),
        max_concurrent_positions=int(_alias(env, "ALPHAFORGE_MAX_CONCURRENT_POSITIONS", "ALPHAFORGE_MAX_OPEN_POSITIONS") or "3"),
        symbol_cooldown_sec=_float_env(env, "ALPHAFORGE_SYMBOL_COOLDOWN_SEC", 120.0),
        max_notional_exposure=_float_env(env, "ALPHAFORGE_MAX_NOTIONAL_EXPOSURE", 100_000.0),
        max_symbol_notional=_float_env(env, "ALPHAFORGE_MAX_SYMBOL_NOTIONAL", 50_000.0),
        stale_market_data_sec=_float_env(env, "ALPHAFORGE_STALE_MARKET_DATA_SEC", 15.0),
        max_spread_pct=_float_env(env, "ALPHAFORGE_MAX_SPREAD_PCT", 0.0025),
        max_abs_funding_rate_pct=_float_env(env, "ALPHAFORGE_MAX_ABS_FUNDING_RATE_PCT", 0.0010),
        global_kill_switch=_bool_env(env, "ALPHAFORGE_GLOBAL_KILL_SWITCH", False),
        require_live_qualification=_bool_env(env, "ALPHAFORGE_REQUIRE_LIVE_QUALIFICATION", True),
        enable_shadow_mode=_bool_env(env, "ALPHAFORGE_ENABLE_SHADOW_MODE", False),
        enable_canary_mode=_bool_env(env, "ALPHAFORGE_ENABLE_CANARY_MODE", False),
        operator_live_acknowledged=_bool_env(env, "ALPHAFORGE_OPERATOR_LIVE_ACKNOWLEDGED", False),
        reconciliation_interval_sec=_float_env(env, "ALPHAFORGE_RECONCILIATION_INTERVAL_SEC", 5.0),
        reconciliation_timeout_sec=_float_env(env, "ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC", 2.0),
        require_exchange_connectivity_for_live=_bool_env(env, "ALPHAFORGE_REQUIRE_EXCHANGE_CONNECTIVITY_FOR_LIVE", True),
        required_live_exchanges=_comma_list(_clean_env_value(env.get("ALPHAFORGE_REQUIRED_LIVE_EXCHANGES")), ("binance",)),
        exchange_connectivity_timeout_sec=_float_env(env, "ALPHAFORGE_EXCHANGE_CONNECTIVITY_TIMEOUT_SEC", 2.0),
    )
    exchange = ExchangeSettings(
        timeout_sec=_float_env(env, "ALPHAFORGE_EXCHANGE_CONNECTIVITY_TIMEOUT_SEC", runtime.exchange_connectivity_timeout_sec),
        binance=BinanceSettings(base_url=_string_env(env, "BINANCE_BASE_URL", "https://api.binance.com")),
        hyperliquid=HyperliquidSettings(api_url=_string_env(env, "HYPERLIQUID_API_URL", "https://api.hyperliquid.xyz")),
    )
    return AlphaForgeConfig(
        runtime=runtime,
        exchange=exchange,
        backtest=BacktestSettings(
            top_n=_int_env(env, "ALPHAFORGE_BACKTEST_TOP_N", 100),
            timeframe=_string_env(env, "ALPHAFORGE_BACKTEST_TIMEFRAME", "1m"),
            output_dir=_string_env(env, "ALPHAFORGE_BACKTEST_OUTPUT_DIR", "data/backtest"),
            initial_balance=_float_env(env, "ALPHAFORGE_BACKTEST_INITIAL_BALANCE", 1000.0),
            risk_pct=_float_env(env, "ALPHAFORGE_BACKTEST_RISK_PCT", 1.0),
        ),
        persistence=PersistenceSettings(database_url=_resolve_database_url(env), enabled=_bool_env(env, "ALPHAFORGE_PERSISTENCE_ENABLED", True)),
        logging=LoggingSettings(level=_string_env(env, "ALPHAFORGE_LOG_LEVEL", "INFO")),
    )
