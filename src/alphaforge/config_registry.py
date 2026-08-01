from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from alphaforge.env_contract import EnvContractEntry, parse_bool, parse_dotenv

MODES = ("BACKTEST", "PAPER", "LIVE")

@dataclass(frozen=True, slots=True)
class ConfigSetting:
    env_name: str
    field_name: str
    value_type: str
    default: Any
    category: str
    applies_to: tuple[str, ...]
    description: str
    min_value: float | None = None
    max_value: float | None = None
    secret: bool = False
    restart_required: bool = True
    dashboard_editable: bool = True
    deprecated_aliases: tuple[str, ...] = ()
    classification: str = "WIRED"
    consumed_by: str = ""
    behavioral_test: str = ""

    def parse(self, raw: Any) -> Any:
        if raw is None:
            return self.default
        if isinstance(raw, str):
            raw = raw.split("#", 1)[0].strip()
        if raw == "":
            return self.default
        if self.value_type == "bool":
            value = parse_bool(self.env_name, raw)
        elif self.value_type == "int":
            value = int(raw)
        elif self.value_type == "float":
            value = float(raw)
        else:
            value = str(raw)
        if isinstance(value, (int, float)):
            if self.min_value is not None and value < self.min_value:
                raise ValueError(f"{self.env_name} below minimum {self.min_value}")
            if self.max_value is not None and value > self.max_value:
                raise ValueError(f"{self.env_name} above maximum {self.max_value}")
        return value


def _s(env, field, typ, default, category, applies, desc, min_value=None, max_value=None, **kw):
    consumers = {
        "Trade Quality Filters": "alphaforge.order.evaluate_trade_quality",
        "Execution Cost Filters": "alphaforge.execution.build_execution_cost_breakdown",
        "Runtime Risk Limits": "alphaforge.runtime.RuntimeOrchestrator._evaluate_runtime_risk",
        "Backtest Settings": "backtest_order.main",
        "Mode / Safety": "alphaforge.runtime._build_runtime_from_env",
        "Operations": "alphaforge.runtime._build_runtime_from_env",
        "Persistence": "alphaforge.persistence.init_db",
        "Logging": "alphaforge.runtime.main",
        "Notifications": "alphaforge.telegram_alert_delivery.telegram_alert_provider_from_env",
        "Hyperliquid": "alphaforge.exchange_market_scanner._scan_hyperliquid",
        "Binance": "alphaforge.env_contract.resolve_binance_environment",
    }
    tests = {
        "Trade Quality Filters": "tests/test_env_filters_canonical.py::test_env_score_threshold_changes_backtest_and_paper_decisions",
        "Execution Cost Filters": "tests/test_env_filters_canonical.py::test_runtime_risk_uses_canonical_spread_slippage_funding_liquidity_and_stale",
        "Runtime Risk Limits": "tests/test_runtime_env_config.py::test_runtime_env_loads_runtime_config_fields",
        "Backtest Settings": "tests/test_backtest_filter_switches.py::test_backtest_trade_quality_switches_are_real_decision_gates",
        "Mode / Safety": "tests/test_runtime_env_config.py::test_runtime_env_prefers_canonical_execution_mode",
        "Operations": "tests/test_runtime_env_config.py::test_runtime_env_loads_runtime_config_fields",
        "Persistence": "tests/test_runtime_env_config.py::test_runtime_env_db_url_prefers_alphaforge_database_url",
        "Logging": "tests/test_env_wiring_contract.py::test_every_wired_value_has_typed_observable_resolution",
        "Notifications": "tests/test_telegram_alert_delivery.py::test_telegram_send_confirmation_is_persisted_without_credentials",
        "Hyperliquid": "tests/test_env_wiring_contract.py::test_hyperliquid_enabled_controls_scanner_without_network",
        "Binance": "tests/test_env_contract.py::test_binance_explicit_override_and_testnet_backward_compatibility",
    }
    # Resolve by setting family before falling back to category.  This keeps
    # metadata tied to the function that actually reads the effective field,
    # rather than claiming that every setting in a broad category shares one
    # consumer merely because it is parsed by the same loader.
    if env.startswith("ALPHAFORGE_BACKTEST_FILTER_"):
        consumer = "alphaforge.config.BacktestFilterSwitches.disabled_filters"
        behavior = "tests/test_backtest_filter_switches.py::test_backtest_trade_quality_switches_are_real_decision_gates"
    elif env.startswith("ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_"):
        consumer = "backtest_order.main"
        behavior = "tests/test_backtest_order_scanner.py::test_short_breakdown_rescue_enabled_marks_rows_and_original_reason"
    elif env.startswith("ALPHAFORGE_BACKTEST_") and any(token in env for token in ("STRATEGY", "GUARD", "PROFILE_PASS", "CONSECUTIVE", "DIAGNOSTIC_SYMBOLS")):
        consumer = "backtest_order.strategy_guardrail_config_from_env"
        behavior = "tests/test_strategy_quality_guardrails.py::test_loss_streak_pause_rejects_after_configured_consecutive_sls"
    else:
        consumer = consumers.get(category, "alphaforge.config.load_config_from_env")
        behavior = tests.get(category, "tests/test_env_wiring_contract.py::test_every_wired_value_has_typed_observable_resolution")
    kw.setdefault("consumed_by", consumer)
    kw.setdefault("behavioral_test", behavior)
    return ConfigSetting(env, field, typ, default, category, tuple(applies), desc, min_value=min_value, max_value=max_value, **kw)

CONFIG_REGISTRY: tuple[ConfigSetting, ...] = (
    _s("ALPHAFORGE_EXECUTION_MODE", "execution_mode", "str", "PAPER", "Mode / Safety", MODES, "Canonical engine mode.", deprecated_aliases=("EXECUTION_MODE",)),
    _s("ALPHAFORGE_ENABLE_PAPER_TRADING", "paper_enabled", "bool", True, "Mode / Safety", ("PAPER",), "Allows PAPER runtime startup."),
    _s("ALPHAFORGE_ENABLE_LIVE_TRADING", "live_enabled", "bool", False, "Mode / Safety", ("LIVE",), "Requests LIVE runtime availability; readiness guard still applies."),
    _s("ALPHAFORGE_REQUIRE_LIVE_QUALIFICATION", "require_live_qualification", "bool", True, "Mode / Safety", ("LIVE",), "Require readiness qualification before LIVE."),
    _s("ALPHAFORGE_GLOBAL_KILL_SWITCH", "global_kill_switch", "bool", False, "Runtime Risk Limits", ("PAPER", "LIVE"), "Emergency stop for runtime order flow."),
    _s("ALPHAFORGE_DATABASE_URL", "database_url", "str", "sqlite:///./alphaforge.db", "Mode / Safety", MODES, "Runtime persistence database URL.", dashboard_editable=False, deprecated_aliases=("ALPHAFORGE_DB_URL", "DATABASE_URL")),
    _s("ALPHAFORGE_MIN_SIGNAL_SCORE", "min_signal_score", "float", 0.62, "Trade Quality Filters", MODES, "Minimum normalized signal score.", 0.0, 10.0, deprecated_aliases=("ALPHAFORGE_MIN_ACCEPT_SCORE",)),
    _s("ALPHAFORGE_MIN_RR", "min_rr", "float", 1.20, "Trade Quality Filters", MODES, "Minimum raw risk/reward.", 0.0, 10.0),
    _s("MIN_EFFECTIVE_RR", "min_effective_rr", "float", 1.60, "Trade Quality Filters", MODES, "Minimum execution-adjusted RR.", 0.0, 10.0, deprecated_aliases=("ALPHAFORGE_MIN_EFFECTIVE_RR",)),
    _s("ALPHAFORGE_MIN_SL_PCT", "min_sl_pct", "float", 0.15, "Trade Quality Filters", MODES, "Minimum stop distance percent.", 0.0, 100.0),
    _s("ALPHAFORGE_MAX_SL_PCT", "max_sl_pct", "float", 1.5, "Trade Quality Filters", MODES, "Maximum stop distance percent.", 0.0, 100.0),
    _s("ALPHAFORGE_MIN_ATR_PCT", "min_atr_pct", "float", 0.25, "Trade Quality Filters", MODES, "Minimum ATR percent when ATR exists.", 0.0, 100.0),
    _s("ALPHAFORGE_MAX_ATR_PCT", "max_atr_pct", "float", 3.0, "Trade Quality Filters", MODES, "Maximum ATR percent when ATR exists.", 0.0, 100.0),
    _s("ALPHAFORGE_BLOCK_UNKNOWN_EXPECTANCY", "block_unknown_expectancy", "bool", True, "Trade Quality Filters", MODES, "Reject candidates without expectancy context."),
    _s("ALPHAFORGE_BLOCK_CHOP_MARKET", "block_chop_market", "bool", True, "Trade Quality Filters", MODES, "Reject candidates marked as chop."),
    _s("ALPHAFORGE_REQUIRE_REGIME_ALIGNMENT", "require_regime_alignment", "bool", True, "Trade Quality Filters", MODES, "Require setup/regime alignment.", deprecated_aliases=("ENABLE_REGIME_FILTER",), behavioral_test="tests/test_env_safety_and_filters.py::test_regime_alias_changes_actual_decision"),
    _s("ALPHAFORGE_STOP_TOO_WIDE_HARD_REJECT", "stop_too_wide_hard_reject", "bool", True, "Trade Quality Filters", MODES, "Hard-reject wide stops unless softening applies."),
    _s("ALPHAFORGE_STOP_TOO_WIDE_SOFT_SCORE_MIN", "stop_too_wide_soft_score_min", "float", 9.0, "Trade Quality Filters", MODES, "Minimum score for wide-stop softening.", 0.0, 10.0),
    _s("ALPHAFORGE_STOP_TOO_WIDE_SOFT_EFFECTIVE_RR_MIN", "stop_too_wide_soft_effective_rr_min", "float", 1.75, "Trade Quality Filters", MODES, "Minimum effective RR for wide-stop softening.", 0.0, 10.0),
    _s("ALPHAFORGE_STOP_TOO_WIDE_MAX_RISK_SCALE", "stop_too_wide_max_risk_scale", "float", 0.50, "Trade Quality Filters", MODES, "Maximum risk scale for softened wide stops.", 0.0, 1.0),
    _s("ALPHAFORGE_STOP_TOO_WIDE_EXTREME_MULT", "stop_too_wide_extreme_mult", "float", 1.50, "Trade Quality Filters", MODES, "Extreme wide-stop multiple.", 1.0, 10.0),
    _s("ALPHAFORGE_MAX_SPREAD_PCT", "max_spread_pct", "float", 0.05, "Execution Cost Filters", MODES, "Maximum spread percent.", 0.0, 1.0, deprecated_aliases=("MAX_SPREAD_PCT",)),
    _s("ALPHAFORGE_MAX_EXPECTED_SLIPPAGE_PCT", "max_expected_slippage_pct", "float", 0.05, "Execution Cost Filters", MODES, "Maximum expected slippage percent.", 0.0, 1.0, deprecated_aliases=("MAX_EXPECTED_SLIPPAGE_PCT",)),
    _s("ALPHAFORGE_MAX_TOTAL_COST_PCT", "max_total_cost_pct", "float", 0.20, "Execution Cost Filters", MODES, "Maximum total explicit execution cost percent before rejection.", 0.0, 1.0, deprecated_aliases=("MAX_TOTAL_COST_PCT",)),
    _s("ALPHAFORGE_MIN_LIQUIDITY_SCORE", "min_liquidity_score", "float", 0.30, "Execution Cost Filters", MODES, "Minimum normalized liquidity score before rejection.", 0.0, 1.0, deprecated_aliases=("MIN_LIQUIDITY_SCORE",)),
    _s("ALPHAFORGE_MAX_VOLATILITY_PENALTY_PCT", "max_volatility_penalty_pct", "float", 0.20, "Execution Cost Filters", MODES, "Maximum RR volatility penalty before rejection.", 0.0, 10.0, deprecated_aliases=("MAX_VOLATILITY_PENALTY_PCT",)),
    _s("ALPHAFORGE_REJECT_UNKNOWN_EXECUTION_CONTEXT", "reject_unknown_execution_context", "bool", True, "Execution Cost Filters", MODES, "Reject when critical execution context is unavailable instead of treating missing values as zero.", deprecated_aliases=("REJECT_UNKNOWN_EXECUTION_CONTEXT",)),
    _s("ALPHAFORGE_MAX_LATENCY_MS", "max_latency_ms", "int", 2500, "Execution Cost Filters", ("PAPER", "LIVE"), "Maximum market-data/execution latency when available.", 0),
    _s("ALPHAFORGE_MAX_ABS_FUNDING_RATE_PCT", "max_abs_funding_rate_pct", "float", 0.0010, "Execution Cost Filters", MODES, "Maximum absolute funding-rate percent.", 0.0, 1.0),
    _s("MIN_LIQUIDITY_USD", "min_liquidity_usd", "float", 5_000_000.0, "Execution Cost Filters", MODES, "Minimum 24h liquidity.", 0.0),
    _s("ALPHAFORGE_MAX_TRADES_GLOBAL_PER_DAY", "max_trades_global_per_day", "int", 10, "Runtime Risk Limits", ("PAPER", "LIVE"), "Runtime global daily trade cap; ignored by BACKTEST by default.", 0),
    _s("ALPHAFORGE_MAX_TRADES_SYMBOL_PER_DAY", "max_trades_symbol_per_day", "int", 2, "Runtime Risk Limits", ("PAPER", "LIVE"), "Runtime per-symbol daily trade cap; ignored by BACKTEST by default.", 0),
    _s("ALPHAFORGE_SYMBOL_COOLDOWN_SEC", "symbol_cooldown_sec", "float", 120.0, "Runtime Risk Limits", ("PAPER", "LIVE"), "Runtime symbol cooldown seconds.", 0.0),
    _s("ALPHAFORGE_SYMBOL_LOSS_STREAK_LIMIT", "symbol_loss_streak_limit", "int", 3, "Runtime Risk Limits", ("PAPER", "LIVE"), "Symbol loss streak runtime block threshold.", 0),
    _s("ALPHAFORGE_GLOBAL_LOSS_STREAK_LIMIT", "global_loss_streak_limit", "int", 5, "Runtime Risk Limits", ("PAPER", "LIVE"), "Global loss streak runtime block threshold.", 0),
    _s("ALPHAFORGE_BACKTEST_LAST_N_DAYS", "backtest_days", "int", 7, "Backtest Settings", ("BACKTEST",), "Default dashboard backtest horizon.", 1),
    _s("ALPHAFORGE_BACKTEST_TIMEFRAME", "backtest_interval", "str", "1m", "Backtest Settings", ("BACKTEST",), "Default backtest candle interval."),
    _s("ALPHAFORGE_BACKTEST_TOP_N", "backtest_max_symbols", "int", 100, "Backtest Settings", ("BACKTEST",), "Backtest universe size cap.", 1),
    _s("ALPHAFORGE_BACKTEST_MAX_TRADES", "backtest_max_trades", "int", 0, "Backtest Settings", ("BACKTEST",), "Optional total backtest trade cap; 0 disables.", 0),
    _s("ALPHAFORGE_BACKTEST_MAX_ACCEPTED_TRADES_PER_DAY", "backtest_max_accepted_trades_per_day", "int", 0, "Backtest Settings", ("BACKTEST",), "Optional BACKTEST accepted/day cap; 0 disables.", 0),
    _s("ALPHAFORGE_BACKTEST_MAX_SYMBOL_TRADES_PER_DAY", "backtest_max_symbol_trades_per_day", "int", 0, "Backtest Settings", ("BACKTEST",), "Optional BACKTEST symbol/day cap; 0 disables.", 0),
    _s("ALPHAFORGE_BACKTEST_USE_EXECUTION_COSTS", "backtest_use_execution_costs", "bool", True, "Backtest Settings", ("BACKTEST",), "Use execution-cost context in backtests when available."),
    _s("ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ENABLED", "backtest_short_breakdown_rescue_enabled", "bool", False, "Backtest Settings", ("BACKTEST",), "BACKTEST-only SHORT_BREAKDOWN_RESCUE experiment; disabled by default and does not affect PAPER/LIVE."),
    _s("ALPHAFORGE_BACKTEST_EXPORT_CONFIG_SNAPSHOT", "backtest_export_config_snapshot", "bool", True, "Backtest Settings", ("BACKTEST",), "Export config_snapshot.json with backtest runs."),
    _s("ALPHAFORGE_SCAN_INTERVAL_SEC", "scan_interval_sec", "float", 1.0, "Operations", ("PAPER", "LIVE"), "Seconds between runtime scans.", 0.01),
    _s("ALPHAFORGE_HEARTBEAT_INTERVAL_SEC", "heartbeat_interval_sec", "float", 30.0, "Operations", ("PAPER", "LIVE"), "Seconds between runtime heartbeats.", 0.01),
    _s("ALPHAFORGE_MAX_SYMBOLS_PER_SCAN", "max_symbols_per_scan", "int", 5, "Operations", ("PAPER", "LIVE"), "Maximum selected symbols per scan.", 1),
    _s("ALPHAFORGE_MAX_REJECT_LOG_ENTRIES", "max_reject_log_entries", "int", 1000, "Operations", ("PAPER", "LIVE"), "In-memory reject log cap.", 1),
    _s("ALPHAFORGE_MAX_CONCURRENT_POSITIONS", "max_concurrent_positions", "int", 3, "Runtime Risk Limits", ("PAPER", "LIVE"), "Concurrent-position hard cap.", 1, deprecated_aliases=("ALPHAFORGE_MAX_OPEN_POSITIONS",)),
    _s("ALPHAFORGE_MAX_NOTIONAL_EXPOSURE", "max_notional_exposure", "float", 100000.0, "Runtime Risk Limits", ("PAPER", "LIVE"), "Portfolio notional hard cap.", 0.0),
    _s("ALPHAFORGE_MAX_SYMBOL_NOTIONAL", "max_symbol_notional", "float", 50000.0, "Runtime Risk Limits", ("PAPER", "LIVE"), "Per-symbol notional hard cap.", 0.0),
    _s("ALPHAFORGE_MAX_DAILY_LOSS_PCT", "max_daily_loss_pct", "float", 0.03, "Runtime Risk Limits", ("PAPER", "LIVE"), "Daily realized-loss fraction that blocks new risk (0.02 means 2%; percentage-point values require explicit migration).", 0.0, 1.0),
    _s("ALPHAFORGE_STALE_MARKET_DATA_SEC", "stale_market_data_sec", "float", 15.0, "Runtime Risk Limits", ("PAPER", "LIVE"), "Maximum market-data age.", 0.0),
    _s("ALPHAFORGE_ENABLE_SHADOW_MODE", "enable_shadow_mode", "bool", False, "Mode / Safety", ("PAPER", "LIVE"), "Enable shadow-only runtime behavior."),
    _s("ALPHAFORGE_AGENT_GRAPH_ENABLED", "agent_graph_enabled", "bool", False, "Mode / Safety", MODES, "Enable the Phase A non-authoritative agent graph."),
    _s("ALPHAFORGE_AGENT_GRAPH_SHADOW", "agent_graph_shadow", "bool", True, "Mode / Safety", MODES, "Require shadow-only agent graph operation."),
    _s("ALPHAFORGE_AGENT_GRAPH_MAX_STEPS", "agent_graph_max_steps", "int", 12, "Operations", MODES, "Bound total graph handler invocations.", 1),
    _s("ALPHAFORGE_AGENT_GRAPH_MAX_REFLECTION_RETRIES", "agent_graph_max_reflection_retries", "int", 1, "Operations", MODES, "Bound reflection retries.", 0),
    _s("ALPHAFORGE_AGENT_GRAPH_STAGE_TIMEOUT_SECONDS", "agent_graph_stage_timeout_seconds", "float", 5.0, "Operations", MODES, "Per-stage shadow timeout.", 0.001),
    _s("ALPHAFORGE_AGENT_GRAPH_PERSIST_TRACES", "agent_graph_persist_traces", "bool", True, "Persistence", MODES, "Persist additive agent shadow traces."),
    _s("ALPHAFORGE_AGENT_GRAPH_MAX_PENDING_RUNS", "agent_graph_max_pending_runs", "int", 64, "Operations", MODES, "Bound queued shadow decisions; overload drops newest trace.", 1),
    _s("ALPHAFORGE_AGENT_GRAPH_DATABASE_URL", "agent_graph_database_url", "str", "sqlite+pysqlite:///data/runtime/alphaforge_agent_shadow.db", "Persistence", MODES, "Isolated Phase A shadow trace database.", dashboard_editable=False),
    _s("ALPHAFORGE_ENABLE_CANARY_MODE", "enable_canary_mode", "bool", False, "Mode / Safety", ("LIVE",), "Request canary mode; readiness gates remain authoritative."),
    _s("ALPHAFORGE_OPERATOR_LIVE_ACKNOWLEDGED", "operator_live_acknowledged", "bool", False, "Mode / Safety", ("LIVE",), "Additional deny-by-default operator acknowledgement."),
    _s("ALPHAFORGE_RECONCILIATION_INTERVAL_SEC", "reconciliation_interval_sec", "float", 5.0, "Operations", ("PAPER", "LIVE"), "Runtime reconciliation interval.", 0.1),
    _s("ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC", "reconciliation_timeout_sec", "float", 2.0, "Operations", ("PAPER", "LIVE"), "Runtime reconciliation timeout.", 0.1),
    _s("ALPHAFORGE_ENABLE_BINANCE_READONLY_RECONCILIATION", "enable_binance_readonly_reconciliation", "bool", False, "Mode / Safety", ("PAPER", "LIVE"), "Enable signed read-only Binance reconciliation."),
    _s("ALPHAFORGE_ALLOW_LIVE_ORDERS", "allow_live_orders", "bool", False, "Mode / Safety", ("LIVE",), "Additional deny-by-default authorization required before any LIVE adapter call.", dashboard_editable=False, consumed_by="alphaforge.runtime.RuntimeOrchestrator._execute", behavioral_test="tests/test_runtime_live_authorization.py::test_runtime_live_authorization_is_authoritative_and_refreshed"),
    _s("ALPHAFORGE_ENABLE_ORDERBOOK_FILTER", "enable_orderbook_filter", "bool", False, "Execution Cost Filters", MODES, "Enable orderbook-context availability and extreme imbalance/spoof-risk rejection.", deprecated_aliases=("ENABLE_ORDERBOOK_FILTER",), consumed_by="alphaforge.order.evaluate_trade_quality", behavioral_test="tests/test_env_safety_and_filters.py::test_orderbook_filter_changes_decision_without_disabling_other_gates"),
    _s("ALPHAFORGE_BINANCE_RECONCILIATION_TRADE_LOOKBACK_MS", "binance_reconciliation_trade_lookback_ms", "int", 3600000, "Operations", ("PAPER", "LIVE"), "Read-only fill lookback window.", 1),
    _s("ALPHAFORGE_RECONCILIATION_POSITION_EPSILON", "reconciliation_position_epsilon", "str", "0.00000001", "Operations", ("PAPER", "LIVE"), "Exact Decimal position dust threshold; exposure equal to the threshold remains inactive."),
    _s("ALPHAFORGE_RECONCILIATION_MAX_FILL_SYMBOLS", "reconciliation_max_fill_symbols", "int", 10, "Operations", ("PAPER", "LIVE"), "Hard fill-query scope cap sized above the five-symbol PAPER scan default without permitting exchange-universe fan-out.", 1, 100),
    _s("ALPHAFORGE_BACKTEST_OUTPUT_DIR", "backtest_output_dir", "str", "data/backtest", "Backtest Settings", ("BACKTEST",), "Backtest artifact output directory."),
    _s("ALPHAFORGE_BACKTEST_INITIAL_BALANCE", "backtest_initial_balance", "float", 1000.0, "Backtest Settings", ("BACKTEST",), "Backtest starting balance.", 0.01),
    _s("ALPHAFORGE_BACKTEST_RISK_PCT", "backtest_risk_pct", "float", 1.0, "Backtest Settings", ("BACKTEST",), "Backtest risk percentage per accepted order.", 0.0, 100.0),
    _s("ALPHAFORGE_BACKTEST_STRATEGY_GUARDRAILS_ENABLED", "backtest_guardrails_enabled", "bool", True, "Backtest Settings", ("BACKTEST",), "Enable strategy-profile quality guardrails."),
    _s("ALPHAFORGE_BACKTEST_STRATEGY_PROFILE", "backtest_strategy_profile", "str", "DEFAULT_FILTERS", "Backtest Settings", ("BACKTEST",), "Backtest strategy profile name."),
    _s("ALPHAFORGE_BACKTEST_MAX_CONSECUTIVE_SL_PAUSE", "backtest_max_consecutive_sl_pause", "int", 4, "Backtest Settings", ("BACKTEST",), "Pause threshold for consecutive stop losses.", 1),
    _s("ALPHAFORGE_BACKTEST_SCORE10_SL_DOMINANCE_GUARD", "backtest_score10_sl_guard", "bool", True, "Backtest Settings", ("BACKTEST",), "Enable score-10 stop-loss dominance guard."),
    _s("ALPHAFORGE_BACKTEST_HIGH_VOL_ACCEPTANCE_GUARD", "backtest_high_vol_guard", "bool", True, "Backtest Settings", ("BACKTEST",), "Enable high-volatility acceptance guard."),
    _s("ALPHAFORGE_BACKTEST_MIN_PROFIT_FACTOR_FOR_PROFILE_PASS", "backtest_min_profit_factor", "float", 1.2, "Backtest Settings", ("BACKTEST",), "Minimum profile profit factor.", 0.0),
    _s("ALPHAFORGE_BACKTEST_MAX_LOSS_STREAK_FOR_PROFILE_PASS", "backtest_max_loss_streak", "int", 6, "Backtest Settings", ("BACKTEST",), "Maximum profile loss streak.", 0),
    _s("ALPHAFORGE_BACKTEST_MAX_DRAWDOWN_PCT_FOR_PROFILE_PASS", "backtest_max_drawdown_pct", "float", 12.0, "Backtest Settings", ("BACKTEST",), "Maximum profile drawdown percent.", 0.0, 100.0),
    _s("ALPHAFORGE_BACKTEST_SHORT_LOW_SCORE_BREAKDOWN_DIAGNOSTIC_SYMBOLS", "backtest_diagnostic_symbols", "str", "BTCUSDT,ETHUSDT", "Backtest Settings", ("BACKTEST",), "Diagnostic symbol allowlist."),
    _s("ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_SIZE_MULTIPLIER", "backtest_rescue_size_multiplier", "float", 0.25, "Backtest Settings", ("BACKTEST",), "Rescue risk-size multiplier.", 0.0, 1.0),
    _s("ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_MAX_PER_DAY", "backtest_rescue_max_per_day", "int", 1, "Backtest Settings", ("BACKTEST",), "Daily rescue cap.", 0),
    _s("ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ALLOWED_REASONS", "backtest_rescue_allowed_reasons", "str", "LOW_SCORE,STOP_TOO_WIDE,DAILY_SYMBOL_TRADE_LIMIT", "Backtest Settings", ("BACKTEST",), "Comma-separated rescue-eligible rejects."),
    _s("ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_MIN_EFFECTIVE_RR", "backtest_rescue_min_effective_rr", "float", 1.1, "Backtest Settings", ("BACKTEST",), "Minimum rescue effective RR.", 0.0),
    _s("ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_MIN_SHADOW_EXPECTANCY", "backtest_rescue_min_shadow_expectancy", "float", 0.0, "Backtest Settings", ("BACKTEST",), "Minimum rescue shadow expectancy."),
    _s("ALPHAFORGE_BACKTEST_FILTER_LOW_SCORE_ENABLED", "backtest_filter_low_score_enabled", "bool", True, "Backtest Settings", ("BACKTEST",), "Enable LOW_SCORE rejection."),
    _s("ALPHAFORGE_BACKTEST_FILTER_TOO_CHOPPY_ENABLED", "backtest_filter_too_choppy_enabled", "bool", True, "Backtest Settings", ("BACKTEST",), "Enable TOO_CHOPPY rejection."),
    _s("ALPHAFORGE_BACKTEST_FILTER_WEAK_TREND_NO_RANGE_ENABLED", "backtest_filter_weak_trend_enabled", "bool", True, "Backtest Settings", ("BACKTEST",), "Enable weak-trend rejection."),
    _s("ALPHAFORGE_BACKTEST_FILTER_STOP_TOO_WIDE_ENABLED", "backtest_filter_stop_too_wide_enabled", "bool", True, "Backtest Settings", ("BACKTEST",), "Enable STOP_TOO_WIDE rejection."),
    _s("ALPHAFORGE_BACKTEST_FILTER_RR_TOO_LOW_ENABLED", "backtest_filter_rr_too_low_enabled", "bool", True, "Backtest Settings", ("BACKTEST",), "Enable RR_TOO_LOW rejection."),
    _s("ALPHAFORGE_BACKTEST_FILTER_DAILY_SYMBOL_TRADE_LIMIT_ENABLED", "backtest_filter_daily_symbol_limit_enabled", "bool", True, "Backtest Settings", ("BACKTEST",), "Enable per-symbol daily cap rejection."),
    _s("ALPHAFORGE_BACKTEST_FILTER_REGIME_MISMATCH_ENABLED", "backtest_filter_regime_mismatch_enabled", "bool", True, "Backtest Settings", ("BACKTEST",), "Enable REGIME_MISMATCH rejection."),
    _s("ALPHAFORGE_BACKTEST_FILTER_PANIC_CONDITIONS_ENABLED", "backtest_filter_panic_enabled", "bool", True, "Backtest Settings", ("BACKTEST",), "Enable PANIC_CONDITIONS rejection."),
    _s("ALPHAFORGE_PERSISTENCE_ENABLED", "persistence_enabled", "bool", True, "Persistence", MODES, "Enable persistence writes."),
    _s("ALPHAFORGE_LOG_LEVEL", "log_level", "str", "INFO", "Logging", MODES, "Runtime logging threshold."),
    _s("HYPERLIQUID_ENABLED", "hyperliquid_enabled", "bool", True, "Hyperliquid", ("PAPER", "LIVE"), "Enable public Hyperliquid scanning and connectivity."),
    _s("HYPERLIQUID_API_URL", "hyperliquid_api_url", "str", "https://api.hyperliquid.xyz", "Hyperliquid", ("PAPER", "LIVE"), "Hyperliquid public API URL.", dashboard_editable=False),
    _s("ALPHAFORGE_ENABLE_TELEGRAM", "telegram_enabled", "bool", False, "Notifications", ("PAPER", "LIVE"), "Enable Telegram diagnostic alert delivery."),
    _s("TELEGRAM_BOT_TOKEN", "telegram_bot_token", "str", "", "Notifications", ("PAPER", "LIVE"), "Telegram bot token.", secret=True, dashboard_editable=False),
    _s("TELEGRAM_CHAT_ID", "telegram_chat_id", "str", "", "Notifications", ("PAPER", "LIVE"), "Telegram destination chat identifier.", secret=True, dashboard_editable=False),
    _s("BINANCE_ENVIRONMENT", "binance_environment", "str", "production", "Binance", ("PAPER", "LIVE"), "Binance USD-M Futures environment selector.", dashboard_editable=False, deprecated_aliases=("BINANCE_TESTNET",), consumed_by="alphaforge.env_contract.resolve_binance_environment"),
    _s("BINANCE_BASE_URL", "binance_rest_base_url", "str", "", "Binance", MODES, "Optional explicit Binance USD-M REST override.", dashboard_editable=False, consumed_by="alphaforge.exchange_market_scanner._scan_binance"),
    _s("BINANCE_WS_URL", "binance_ws_base_url", "str", "", "Binance", ("PAPER", "LIVE"), "Optional explicit Binance USD-M websocket override.", dashboard_editable=False, consumed_by="alphaforge.env_contract.resolve_binance_environment"),
    _s("BINANCE_DEFAULT_QUOTE_ASSET", "binance_default_quote_asset", "str", "USDT", "Binance", MODES, "Quote asset used by the Binance market scanner.", dashboard_editable=False, consumed_by="alphaforge.exchange_market_scanner._scan_binance"),
    _s("BINANCE_DEFAULT_MARKET_TYPE", "binance_default_market_type", "str", "USD_M", "Binance", MODES, "Canonical USD-M Futures type USD_M; USD-M, USDT_M, and USDT-M are normalized aliases.", dashboard_editable=False, consumed_by="alphaforge.exchange_market_scanner._scan_binance"),
    _s("ALPHAFORGE_BINANCE_RECV_WINDOW_MS", "binance_recv_window_ms", "int", 5000, "Binance", ("PAPER", "LIVE"), "Canonical signed reconciliation receive window; the legacy BINANCE_RECV_WINDOW_MS alias is lower precedence.", 1000, 60000, dashboard_editable=False, deprecated_aliases=("BINANCE_RECV_WINDOW_MS",), consumed_by="alphaforge.binance_reconciliation_provider"),
    _s("BINANCE_REQUEST_TIMEOUT_SEC", "binance_request_timeout_sec", "float", 2.0, "Binance", ("PAPER", "LIVE"), "Binance HTTP request timeout.", 0.1, dashboard_editable=False, consumed_by="alphaforge.binance_reconciliation_provider.BinanceReadonlyReconciliationProvider._signed_get"),
    _s("BINANCE_API_KEY", "binance_api_key", "str", "", "Binance", ("PAPER", "LIVE"), "Read-only reconciliation API key.", secret=True, dashboard_editable=False, consumed_by="alphaforge.runtime._build_runtime_from_env"),
    _s("BINANCE_API_SECRET", "binance_api_secret", "str", "", "Binance", ("PAPER", "LIVE"), "Read-only reconciliation API secret.", secret=True, dashboard_editable=False, consumed_by="alphaforge.runtime._build_runtime_from_env"),
)

REGISTRY_BY_ENV = {s.env_name: s for s in CONFIG_REGISTRY}
FIELD_BY_NAME = {s.field_name: s for s in CONFIG_REGISTRY}

# Former template settings with no canonical subsystem contract.  They remain
# documented only in a clearly non-operational section for migration/audit.
_RESERVED_CANDIDATES: tuple[str, ...] = ('ALPHAFORGE_ALLOW_LIVE_ORDERS', 'ALPHAFORGE_BACKTEST_CI', 'ALPHAFORGE_BACKTEST_FILTER_DAILY_SYMBOL_TRADE_LIMIT_ENABLED', 'ALPHAFORGE_BACKTEST_FILTER_LOW_SCORE_ENABLED', 'ALPHAFORGE_BACKTEST_FILTER_PANIC_CONDITIONS_ENABLED', 'ALPHAFORGE_BACKTEST_FILTER_REGIME_MISMATCH_ENABLED', 'ALPHAFORGE_BACKTEST_FILTER_RR_TOO_LOW_ENABLED', 'ALPHAFORGE_BACKTEST_FILTER_STOP_TOO_WIDE_ENABLED', 'ALPHAFORGE_BACKTEST_FILTER_TOO_CHOPPY_ENABLED', 'ALPHAFORGE_BACKTEST_FILTER_WEAK_TREND_NO_RANGE_ENABLED', 'ALPHAFORGE_BACKTEST_HIGH_VOL_ACCEPTANCE_GUARD', 'ALPHAFORGE_BACKTEST_INITIAL_BALANCE', 'ALPHAFORGE_BACKTEST_MAX_CONSECUTIVE_SL_PAUSE', 'ALPHAFORGE_BACKTEST_MAX_DRAWDOWN_PCT_FOR_PROFILE_PASS', 'ALPHAFORGE_BACKTEST_MAX_LOSS_STREAK_FOR_PROFILE_PASS', 'ALPHAFORGE_BACKTEST_MIN_PROFIT_FACTOR_FOR_PROFILE_PASS', 'ALPHAFORGE_BACKTEST_OFFLINE', 'ALPHAFORGE_BACKTEST_OUTPUT_DIR', 'ALPHAFORGE_BACKTEST_RISK_PCT', 'ALPHAFORGE_BACKTEST_SCORE10_SL_DOMINANCE_GUARD', 'ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ALLOWED_REASONS', 'ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_MAX_PER_DAY', 'ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_MIN_EFFECTIVE_RR', 'ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_MIN_SHADOW_EXPECTANCY', 'ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_SIZE_MULTIPLIER', 'ALPHAFORGE_BACKTEST_SHORT_LOW_SCORE_BREAKDOWN_DIAGNOSTIC_SYMBOLS', 'ALPHAFORGE_BACKTEST_STRATEGY_GUARDRAILS_ENABLED', 'ALPHAFORGE_BACKTEST_STRATEGY_PROFILE', 'ALPHAFORGE_BINANCE_RECONCILIATION_TRADE_LOOKBACK_MS', 'ALPHAFORGE_DB_ECHO', 'ALPHAFORGE_DEBUG', 'ALPHAFORGE_DRY_RUN', 'ALPHAFORGE_DUMP_EXECUTION_CTX', 'ALPHAFORGE_ENABLE_BACKTEST', 'ALPHAFORGE_ENABLE_BINANCE_READONLY_RECONCILIATION', 'ALPHAFORGE_ENABLE_CANARY_MODE', 'ALPHAFORGE_ENABLE_DISCORD', 'ALPHAFORGE_ENABLE_LIVE_READINESS', 'ALPHAFORGE_ENABLE_NOTIFICATIONS', 'ALPHAFORGE_ENABLE_RECONCILIATION', 'ALPHAFORGE_ENABLE_REJECT_SHADOW_ANALYTICS', 'ALPHAFORGE_ENABLE_SHADOW_MODE', 'ALPHAFORGE_ENABLE_TELEGRAM', 'ALPHAFORGE_ENVIRONMENT', 'ALPHAFORGE_EXPERIMENTAL_ADAPTIVE_THRESHOLDS', 'ALPHAFORGE_EXPERIMENTAL_EXCHANGE_REPAIR', 'ALPHAFORGE_EXPORT_VERIFY_INTEGRITY', 'ALPHAFORGE_HEARTBEAT_INTERVAL_SEC', 'ALPHAFORGE_LOG_FILE', 'ALPHAFORGE_LOG_FORMAT', 'ALPHAFORGE_LOG_LEVEL', 'ALPHAFORGE_MAKER_FEE_PCT', 'ALPHAFORGE_MAX_CONCURRENT_POSITIONS', 'ALPHAFORGE_MAX_DAILY_LOSS_PCT', 'ALPHAFORGE_MAX_NOTIONAL_EXPOSURE', 'ALPHAFORGE_MAX_OPEN_POSITIONS', 'ALPHAFORGE_MAX_SYMBOL_NOTIONAL', 'ALPHAFORGE_METRICS_HEARTBEAT_ENABLED', 'ALPHAFORGE_MIN_TRADE_SCORE', 'ALPHAFORGE_OPERATOR_LIVE_ACKNOWLEDGED', 'ALPHAFORGE_POSTGRES_URL', 'ALPHAFORGE_RECONCILIATION_INTERVAL_SEC', 'ALPHAFORGE_RECONCILIATION_TIMEOUT_SEC', 'ALPHAFORGE_RISK_PCT_PER_TRADE', 'ALPHAFORGE_SCAN_INTERVAL_SEC', 'ALPHAFORGE_SQLITE_PATH', 'ALPHAFORGE_STALE_MARKET_DATA_SEC', 'ALPHAFORGE_TAKER_FEE_PCT', 'ALPHAFORGE_TIMEZONE', 'ALPHAFORGE_TRACE_LIFECYCLE', 'DISCORD_WEBHOOK_URL', 'ENABLE_ABSORPTION_FILTER', 'ENABLE_ORDERBOOK_FILTER', 'ENABLE_REGIME_FILTER', 'ENABLE_SPOOF_DETECTION', 'HYPERLIQUID_API_KEY', 'HYPERLIQUID_API_SECRET', 'HYPERLIQUID_API_URL', 'HYPERLIQUID_ENABLED', 'HYPERLIQUID_TESTNET', 'HYPERLIQUID_WS_URL', 'MAX_CORRELATED_POSITIONS', 'MAX_SLIPPAGE_BPS', 'MAX_SPREAD_BPS', 'QUEUE_BACKEND', 'QUEUE_NAME', 'REDIS_ENABLED', 'REDIS_KEY_PREFIX', 'REDIS_URL', 'REJECT_UNKNOWN_EXPECTANCY', 'RESERVED_NOT_WIRED', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID')

_CANONICAL_OR_ALIAS = {s.env_name for s in CONFIG_REGISTRY} | {alias for s in CONFIG_REGISTRY for alias in s.deprecated_aliases}
RESERVED_VARIABLES = tuple(name for name in _RESERVED_CANDIDATES if name not in _CANONICAL_OR_ALIAS)

_RESERVED_REASONS = {
    "ALPHAFORGE_ALLOW_LIVE_ORDERS": "UNSAFE",
    "ALPHAFORGE_DRY_RUN": "DEPRECATED_NO_EFFECT",
    "ALPHAFORGE_ENABLE_LIVE_READINESS": "DEPRECATED_NO_EFFECT",
    "ALPHAFORGE_EXPORT_VERIFY_INTEGRITY": "DEPRECATED_NO_EFFECT",
    "RESERVED_NOT_WIRED": "REMOVED",
}

def _reserved_reason(name: str) -> str:
    if name in _RESERVED_REASONS:
        return _RESERVED_REASONS[name]
    if name.startswith(("REDIS_", "QUEUE_")):
        return "FUTURE_SUBSYSTEM"
    if name.startswith(("DISCORD_", "ALPHAFORGE_ENABLE_DISCORD", "ALPHAFORGE_ENABLE_NOTIFICATIONS")):
        return "NOT_IMPLEMENTED"
    if name.startswith("ALPHAFORGE_EXPERIMENTAL_"):
        return "UNSAFE"
    if name.startswith(("ENABLE_ORDERBOOK", "ENABLE_SPOOF", "ENABLE_ABSORPTION", "ENABLE_REGIME")):
        return "NOT_IMPLEMENTED"
    if name.startswith("HYPERLIQUID_"):
        return "NOT_IMPLEMENTED"
    return "NOT_IMPLEMENTED"


def _reserved_details(name: str) -> tuple[str, bool, str | None]:
    reason = _reserved_reason(name)
    subsystem = None
    if name.startswith("REDIS_"):
        subsystem = "distributed cache/coordination"
    elif name.startswith("QUEUE_"):
        subsystem = "detached job queue"
    elif "DISCORD" in name or "NOTIFICATION" in name:
        subsystem = "notification delivery"
    elif name.startswith("HYPERLIQUID_"):
        subsystem = "authenticated Hyperliquid client"
    elif name.startswith("ENABLE_"):
        subsystem = "market microstructure filter"
    explanations = {
        "UNSAFE": f"{name} is intentionally inactive because enabling an unqualified experimental or mutation path would weaken fail-closed safety.",
        "DEPRECATED_NO_EFFECT": f"{name} belonged to a superseded control and has no authoritative runtime meaning; migrate to the canonical safety/configuration gates.",
        "REMOVED": f"{name} represents behavior removed from the production contract and is retained only so audit can identify stale .env files.",
        "FUTURE_SUBSYSTEM": f"{name} cannot be consumed until the {subsystem or 'planned'} subsystem exists with persistence and failure semantics.",
        "NOT_IMPLEMENTED": f"{name} has no safe production consumer today; implementing it requires the {subsystem or 'corresponding runtime'} subsystem rather than a parser-only flag.",
    }
    return explanations[reason], reason not in {"FUTURE_SUBSYSTEM"}, subsystem


def env_contract_inventory() -> tuple[EnvContractEntry, ...]:
    rows: list[EnvContractEntry] = []
    for setting in CONFIG_REGISTRY:
        rows.append(EnvContractEntry(
            name=setting.env_name, canonical_name=setting.env_name,
            classification="WIRED", value_type=("secret" if setting.secret else "URL" if setting.env_name.endswith(("_URL", "_WS_URL")) else setting.value_type),
            default=setting.default, applies_to=setting.applies_to,
            consumed_by=setting.consumed_by, restart_required=setting.restart_required,
            secret=setting.secret, description=setting.description,
            behavioral_test=setting.behavioral_test,
        ))
        for alias in setting.deprecated_aliases:
            rows.append(EnvContractEntry(
                name=alias, canonical_name=setting.env_name,
                classification="ALIAS", value_type=setting.value_type,
                default=None, applies_to=setting.applies_to,
                consumed_by="alphaforge.config_registry alias resolution",
                restart_required=setting.restart_required, secret=setting.secret,
                deprecated=True, description=f"Deprecated alias for {setting.env_name}; canonical value wins within a source.",
                behavioral_test="tests/test_env_wiring_contract.py::test_alias_conflicts_fail_audit",
            ))
    for name in RESERVED_VARIABLES:
        secret = any(token in name for token in ("SECRET", "TOKEN", "KEY", "PASSWORD", "WEBHOOK"))
        explanation, remove, future = _reserved_details(name)
        rows.append(EnvContractEntry(
            name=name, canonical_name=name, classification="RESERVED",
            value_type="secret" if secret else "string", default=None,
            applies_to=MODES, consumed_by="unsupported/reserved", secret=secret,
            description=explanation,
            unsupported_reason=_reserved_reason(name),
            unsupported_explanation=explanation,
            remove_from_templates=remove,
            intended_future_subsystem=future,
        ))
    return tuple(sorted(rows, key=lambda row: row.name))


ENV_CONTRACT = env_contract_inventory()
CONTRACT_BY_NAME = {row.name: row for row in ENV_CONTRACT}

def load_dotenv_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return parse_dotenv(path)


def effective_config_subset(names: tuple[str, ...], *, env: Mapping[str, str] | None = None,
                            root: Path | None = None, fail_on_alias_conflict: bool = False,
                            include_files: bool = True) -> dict[str, dict[str, Any]]:
    """Resolve only requested registry settings using canonical source precedence."""
    root = root or Path.cwd()
    env = os.environ if env is None else env
    sources = (("dotenv", load_dotenv_file(root / ".env")),
               ("dotenv_local", load_dotenv_file(root / ".env.local"))) if include_files else ()
    override_path = root / "config" / "runtime_overrides.json"
    try:
        overrides = json.loads(override_path.read_text()) if override_path.exists() else {}
    except json.JSONDecodeError:
        overrides = {}
    sources += (("dashboard", overrides), ("process_env", env))
    out: dict[str, dict[str, Any]] = {}
    for name in names:
        setting = REGISTRY_BY_ENV[name]
        raw = None
        source = "default"
        for source_name, mapping in sources:
            present = [(candidate, str(mapping[candidate]).strip()) for candidate in (setting.env_name, *setting.deprecated_aliases)
                       if candidate in mapping and str(mapping[candidate]).strip()]
            if not present:
                continue
            canonical = next((value for candidate, value in present if candidate == setting.env_name), None)
            aliases = [(candidate, value) for candidate, value in present if candidate != setting.env_name]
            if fail_on_alias_conflict and canonical is not None and any(value != canonical for _, value in aliases):
                alias = next(candidate for candidate, value in aliases if value != canonical)
                raise ValueError(f"alias conflict: {setting.env_name} and {alias} differ")
            selected, raw = (setting.env_name, canonical) if canonical is not None else aliases[0]
            source = source_name if selected == setting.env_name else f"alias ({selected})"
        out[name] = {"setting": setting, "value": setting.parse(raw), "source": source}
    return out

def effective_config_values(*, env: Mapping[str, str] | None = None, root: Path | None = None) -> dict[str, dict[str, Any]]:
    return effective_config_subset(tuple(setting.env_name for setting in CONFIG_REGISTRY), env=env, root=root)

def decision_filter_config(mode: str, *, env: Mapping[str, str] | None = None, root: Path | None = None) -> dict[str, Any]:
    snap = effective_config_values(env=env, root=root)
    val = lambda name: snap[name]["value"]
    mode_u = str(mode or val("ALPHAFORGE_EXECUTION_MODE")).upper()
    cfg = {
        "MODE": mode_u,
        "MIN_TRADE_SCORE": val("ALPHAFORGE_MIN_SIGNAL_SCORE"),
        "MIN_RR": val("ALPHAFORGE_MIN_RR"),
        "MIN_EFFECTIVE_RR": val("MIN_EFFECTIVE_RR"),
        "MIN_EXPECTANCY": 0.0,
        "MIN_SL_PCT": val("ALPHAFORGE_MIN_SL_PCT"),
        "MAX_SL_PCT": val("ALPHAFORGE_MAX_SL_PCT"),
        "MAX_SPREAD_PCT": val("ALPHAFORGE_MAX_SPREAD_PCT"),
        "MAX_EXPECTED_SLIPPAGE_PCT": val("ALPHAFORGE_MAX_EXPECTED_SLIPPAGE_PCT"),
        "MAX_SLIPPAGE_PCT": val("ALPHAFORGE_MAX_EXPECTED_SLIPPAGE_PCT"),
        "MAX_TOTAL_COST_PCT": val("ALPHAFORGE_MAX_TOTAL_COST_PCT"),
        "MIN_LIQUIDITY_SCORE": val("ALPHAFORGE_MIN_LIQUIDITY_SCORE"),
        "MAX_VOLATILITY_PENALTY_PCT": val("ALPHAFORGE_MAX_VOLATILITY_PENALTY_PCT"),
        "REJECT_UNKNOWN_EXECUTION_CONTEXT": val("ALPHAFORGE_REJECT_UNKNOWN_EXECUTION_CONTEXT"),
        "MIN_ATR_PCT": val("ALPHAFORGE_MIN_ATR_PCT"),
        "MAX_ATR_PCT": val("ALPHAFORGE_MAX_ATR_PCT"),
        "BLOCK_UNKNOWN_EXPECTANCY": val("ALPHAFORGE_BLOCK_UNKNOWN_EXPECTANCY"),
        "BLOCK_CHOP_MARKET": val("ALPHAFORGE_BLOCK_CHOP_MARKET"),
        "REQUIRE_REGIME_ALIGNMENT": val("ALPHAFORGE_REQUIRE_REGIME_ALIGNMENT"),
        "ENABLE_ORDERBOOK_FILTER": val("ALPHAFORGE_ENABLE_ORDERBOOK_FILTER"),
        "STOP_TOO_WIDE_HARD_REJECT": val("ALPHAFORGE_STOP_TOO_WIDE_HARD_REJECT"),
        "STOP_TOO_WIDE_SOFTEN_FOR_HIGH_SCORE": True,
        "STOP_TOO_WIDE_SOFT_SCORE_MIN": val("ALPHAFORGE_STOP_TOO_WIDE_SOFT_SCORE_MIN"),
        "STOP_TOO_WIDE_SOFT_EFFECTIVE_RR_MIN": val("ALPHAFORGE_STOP_TOO_WIDE_SOFT_EFFECTIVE_RR_MIN"),
        "STOP_TOO_WIDE_MAX_RISK_SCALE": val("ALPHAFORGE_STOP_TOO_WIDE_MAX_RISK_SCALE"),
        "STOP_TOO_WIDE_EXTREME_MULT": val("ALPHAFORGE_STOP_TOO_WIDE_EXTREME_MULT"),
        "RUNTIME_LIMITS_ACTIVE": mode_u in {"PAPER", "LIVE"},
        "SYMBOL_COOLDOWN_MINUTES": val("ALPHAFORGE_SYMBOL_COOLDOWN_SEC") / 60.0,
        "MAX_TRADES_PER_SYMBOL_PER_DAY": val("ALPHAFORGE_MAX_TRADES_SYMBOL_PER_DAY"),
        "MAX_TRADES_GLOBAL_PER_DAY": val("ALPHAFORGE_MAX_TRADES_GLOBAL_PER_DAY"),
        "SYMBOL_LOSS_STREAK_LIMIT": val("ALPHAFORGE_SYMBOL_LOSS_STREAK_LIMIT"),
        "GLOBAL_LOSS_STREAK_LIMIT": val("ALPHAFORGE_GLOBAL_LOSS_STREAK_LIMIT"),
    }
    return cfg

def config_snapshot(mode: str | None = None, *, env: Mapping[str, str] | None = None, root: Path | None = None) -> list[dict[str, Any]]:
    snap = effective_config_values(env=env, root=root)
    mode_u = str(mode or snap["ALPHAFORGE_EXECUTION_MODE"]["value"]).upper()
    rows = []
    for name, item in snap.items():
        s: ConfigSetting = item["setting"]
        value = "********" if s.secret else item["value"]
        rows.append({"env_name": name, "field_name": s.field_name, "current_value": value, "default": s.default, "type": s.value_type, "applies_to": list(s.applies_to), "category": s.category, "description": s.description, "source": item["source"], "restart_required": s.restart_required, "dashboard_editable": s.dashboard_editable and not s.secret, "secret": s.secret, "active": mode_u in s.applies_to})
    return rows

def write_dashboard_overrides(updates: Mapping[str, Any], *, root: Path | None = None) -> None:
    root = root or Path.cwd()
    path = root / "config" / "runtime_overrides.json"
    current = {}
    if path.exists():
        current = json.loads(path.read_text())
    for name, raw in updates.items():
        if name not in REGISTRY_BY_ENV:
            raise ValueError(f"Unknown managed setting: {name}")
        setting = REGISTRY_BY_ENV[name]
        if setting.secret or not setting.dashboard_editable:
            raise ValueError(f"Setting is not dashboard editable: {name}")
        if name == "ALPHAFORGE_ENABLE_LIVE_TRADING" and setting.parse(raw) is True:
            raise ValueError("Dashboard Settings cannot enable LIVE; use readiness-gated runtime controls")
        current[name] = setting.parse(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")

def reset_dashboard_override(name: str, *, root: Path | None = None) -> None:
    root = root or Path.cwd()
    path = root / "config" / "runtime_overrides.json"
    if name not in REGISTRY_BY_ENV:
        raise ValueError(f"Unknown managed setting: {name}")
    if not path.exists():
        return
    current = json.loads(path.read_text())
    current.pop(name, None)
    path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
