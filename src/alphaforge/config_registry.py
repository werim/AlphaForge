from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

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

    def parse(self, raw: Any) -> Any:
        if raw is None:
            return self.default
        if isinstance(raw, str):
            raw = raw.split("#", 1)[0].strip()
        if raw == "":
            return self.default
        if self.value_type == "bool":
            if isinstance(raw, bool):
                value = raw
            else:
                lowered = str(raw).lower()
                if lowered in {"1", "true", "yes", "on"}:
                    value = True
                elif lowered in {"0", "false", "no", "off"}:
                    value = False
                else:
                    raise ValueError(f"{self.env_name} invalid bool: {raw}")
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
    return ConfigSetting(env, field, typ, default, category, tuple(applies), desc, min_value=min_value, max_value=max_value, **kw)

CONFIG_REGISTRY: tuple[ConfigSetting, ...] = (
    _s("ALPHAFORGE_EXECUTION_MODE", "execution_mode", "str", "PAPER", "Mode / Safety", MODES, "Canonical engine mode.", deprecated_aliases=("EXECUTION_MODE",)),
    _s("ALPHAFORGE_ENABLE_PAPER_TRADING", "paper_enabled", "bool", True, "Mode / Safety", ("PAPER",), "Allows PAPER runtime startup."),
    _s("ALPHAFORGE_ENABLE_LIVE_TRADING", "live_enabled", "bool", False, "Mode / Safety", ("LIVE",), "Requests LIVE runtime availability; readiness guard still applies."),
    _s("ALPHAFORGE_REQUIRE_LIVE_QUALIFICATION", "require_live_qualification", "bool", True, "Mode / Safety", ("LIVE",), "Require readiness qualification before LIVE."),
    _s("ALPHAFORGE_GLOBAL_KILL_SWITCH", "global_kill_switch", "bool", False, "Runtime Risk Limits", ("PAPER", "LIVE"), "Emergency stop for runtime order flow."),
    _s("ALPHAFORGE_DATABASE_URL", "database_url", "str", "sqlite:///./alphaforge.db", "Mode / Safety", MODES, "Runtime persistence database URL.", dashboard_editable=False, deprecated_aliases=("ALPHAFORGE_DB_URL", "DATABASE_URL")),
    _s("ALPHAFORGE_MIN_SIGNAL_SCORE", "min_signal_score", "float", 0.62, "Trade Quality Filters", MODES, "Minimum normalized signal score.", 0.0, 10.0, deprecated_aliases=("ALPHAFORGE_MIN_ACCEPT_SCORE",)),
    _s("ALPHAFORGE_MIN_RR", "min_rr", "float", 1.70, "Trade Quality Filters", MODES, "Minimum raw risk/reward.", 0.0, 10.0),
    _s("ALPHAFORGE_MIN_EFFECTIVE_RR", "min_effective_rr", "float", 1.60, "Trade Quality Filters", MODES, "Minimum execution-adjusted RR survival default; deprecated alias MIN_EFFECTIVE_RR remains supported.", 0.0, 10.0, deprecated_aliases=("MIN_EFFECTIVE_RR",)),
    _s("ALPHAFORGE_MIN_SL_PCT", "min_sl_pct", "float", 0.15, "Trade Quality Filters", MODES, "Minimum stop distance percent.", 0.0, 100.0),
    _s("ALPHAFORGE_MAX_SL_PCT", "max_sl_pct", "float", 1.5, "Trade Quality Filters", MODES, "Maximum stop distance percent.", 0.0, 100.0),
    _s("ALPHAFORGE_MIN_ATR_PCT", "min_atr_pct", "float", 0.25, "Trade Quality Filters", MODES, "Minimum ATR percent when ATR exists.", 0.0, 100.0),
    _s("ALPHAFORGE_MAX_ATR_PCT", "max_atr_pct", "float", 3.0, "Trade Quality Filters", MODES, "Maximum ATR percent when ATR exists.", 0.0, 100.0),
    _s("ALPHAFORGE_BLOCK_UNKNOWN_EXPECTANCY", "block_unknown_expectancy", "bool", True, "Trade Quality Filters", MODES, "Reject candidates without expectancy context."),
    _s("ALPHAFORGE_BLOCK_CHOP_MARKET", "block_chop_market", "bool", True, "Trade Quality Filters", MODES, "Reject candidates marked as chop."),
    _s("ALPHAFORGE_REQUIRE_REGIME_ALIGNMENT", "require_regime_alignment", "bool", True, "Trade Quality Filters", MODES, "Require setup/regime alignment."),
    _s("ALPHAFORGE_STOP_TOO_WIDE_HARD_REJECT", "stop_too_wide_hard_reject", "bool", True, "Trade Quality Filters", MODES, "Hard-reject wide stops unless softening applies."),
    _s("ALPHAFORGE_STOP_TOO_WIDE_SOFT_SCORE_MIN", "stop_too_wide_soft_score_min", "float", 9.0, "Trade Quality Filters", MODES, "Minimum score for wide-stop softening.", 0.0, 10.0),
    _s("ALPHAFORGE_STOP_TOO_WIDE_SOFT_EFFECTIVE_RR_MIN", "stop_too_wide_soft_effective_rr_min", "float", 1.75, "Trade Quality Filters", MODES, "Minimum effective RR for wide-stop softening.", 0.0, 10.0),
    _s("ALPHAFORGE_STOP_TOO_WIDE_MAX_RISK_SCALE", "stop_too_wide_max_risk_scale", "float", 0.50, "Trade Quality Filters", MODES, "Maximum risk scale for softened wide stops.", 0.0, 1.0),
    _s("ALPHAFORGE_STOP_TOO_WIDE_EXTREME_MULT", "stop_too_wide_extreme_mult", "float", 1.50, "Trade Quality Filters", MODES, "Extreme wide-stop multiple.", 1.0, 10.0),
    _s("ALPHAFORGE_MAX_SPREAD_PCT", "max_spread_pct", "float", 0.05, "Execution Cost Filters", MODES, "Maximum spread in percent units; 0.05 means 0.05% and 0.0025 means 0.0025%.", 0.0, 1.0, deprecated_aliases=("MAX_SPREAD_PCT",)),
    _s("ALPHAFORGE_MAX_EXPECTED_SLIPPAGE_PCT", "max_expected_slippage_pct", "float", 0.05, "Execution Cost Filters", MODES, "Maximum expected slippage in percent units; 0.05 means 0.05% and 0.0020 means 0.0020%.", 0.0, 1.0, deprecated_aliases=("MAX_EXPECTED_SLIPPAGE_PCT",)),
    _s("ALPHAFORGE_MAX_LATENCY_MS", "max_latency_ms", "int", 2500, "Execution Cost Filters", ("PAPER", "LIVE"), "Maximum market-data/execution latency when available.", 0),
    _s("ALPHAFORGE_MAX_ABS_FUNDING_RATE_PCT", "max_abs_funding_rate_pct", "float", 0.0010, "Execution Cost Filters", MODES, "Maximum absolute funding-rate percent.", 0.0, 1.0),
    _s("ALPHAFORGE_MIN_LIQUIDITY_USD", "min_liquidity_usd", "float", 5_000_000.0, "Execution Cost Filters", MODES, "Minimum 24h liquidity; deprecated alias MIN_LIQUIDITY_USD remains supported.", 0.0, deprecated_aliases=("MIN_LIQUIDITY_USD",)),
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
    _s("ALPHAFORGE_BACKTEST_EXPORT_CONFIG_SNAPSHOT", "backtest_export_config_snapshot", "bool", True, "Backtest Settings", ("BACKTEST",), "Export config_snapshot.json with backtest runs."),
)

REGISTRY_BY_ENV = {s.env_name: s for s in CONFIG_REGISTRY}
FIELD_BY_NAME = {s.field_name: s for s in CONFIG_REGISTRY}

def load_dotenv_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, v = stripped.split("=", 1)
        values[k.strip()] = v.strip()
    return values

def effective_config_values(*, env: Mapping[str, str] | None = None, root: Path | None = None) -> dict[str, dict[str, Any]]:
    root = root or Path.cwd()
    env = env or os.environ
    file_values = load_dotenv_file(root / ".env")
    local_values = load_dotenv_file(root / ".env.local")
    override_values = {}
    override_path = root / "config" / "runtime_overrides.json"
    if override_path.exists():
        try:
            override_values = json.loads(override_path.read_text())
        except json.JSONDecodeError:
            override_values = {}
    out: dict[str, dict[str, Any]] = {}
    for setting in CONFIG_REGISTRY:
        source = "default"
        raw = None
        for source_name, mapping in ((".env", file_values), (".env.local", local_values), ("dashboard override", override_values), ("environment", env)):
            for name in (setting.env_name, *setting.deprecated_aliases):
                if name in mapping:
                    raw = mapping[name]
                    source = source_name if name == setting.env_name else f"{source_name} ({name})"
                    break
            if raw is not None:
                break
        value = setting.parse(raw)
        out[setting.env_name] = {"setting": setting, "value": value, "source": source}
    return out

def decision_filter_config(mode: str, *, env: Mapping[str, str] | None = None, root: Path | None = None) -> dict[str, Any]:
    snap = effective_config_values(env=env, root=root)
    val = lambda name: snap[name]["value"]
    mode_u = str(mode or val("ALPHAFORGE_EXECUTION_MODE")).upper()
    cfg = {
        "MODE": mode_u,
        "MIN_TRADE_SCORE": val("ALPHAFORGE_MIN_SIGNAL_SCORE"),
        "MIN_RR": val("ALPHAFORGE_MIN_RR"),
        "MIN_EFFECTIVE_RR": val("ALPHAFORGE_MIN_EFFECTIVE_RR"),
        "MIN_EXPECTANCY": 0.0,
        "MIN_SL_PCT": val("ALPHAFORGE_MIN_SL_PCT"),
        "MAX_SL_PCT": val("ALPHAFORGE_MAX_SL_PCT"),
        "MAX_SPREAD_PCT": val("ALPHAFORGE_MAX_SPREAD_PCT"),
        "MAX_EXPECTED_SLIPPAGE_PCT": val("ALPHAFORGE_MAX_EXPECTED_SLIPPAGE_PCT"),
        "MIN_ATR_PCT": val("ALPHAFORGE_MIN_ATR_PCT"),
        "MAX_ATR_PCT": val("ALPHAFORGE_MAX_ATR_PCT"),
        "BLOCK_UNKNOWN_EXPECTANCY": val("ALPHAFORGE_BLOCK_UNKNOWN_EXPECTANCY"),
        "BLOCK_CHOP_MARKET": val("ALPHAFORGE_BLOCK_CHOP_MARKET"),
        "REQUIRE_REGIME_ALIGNMENT": val("ALPHAFORGE_REQUIRE_REGIME_ALIGNMENT"),
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
        rows.append({"env_name": name, "field_name": s.field_name, "current_value": value, "default": s.default, "type": s.value_type, "applies_to": list(s.applies_to), "category": s.category, "description": s.description, "source": item["source"], "restart_required": s.restart_required, "dashboard_editable": s.dashboard_editable and not s.secret and not str(item["source"]).startswith("environment"), "env_locked": str(item["source"]).startswith("environment"), "secret": s.secret, "active_in_current_mode": mode_u in s.applies_to, "active": mode_u in s.applies_to})
    return rows

def write_dashboard_overrides(updates: Mapping[str, Any], *, root: Path | None = None, env: Mapping[str, str] | None = None, live_readiness_pass: bool = False) -> None:
    root = root or Path.cwd()
    env = env or os.environ
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
        if any(env_name in env for env_name in (setting.env_name, *setting.deprecated_aliases)):
            raise ValueError(f"Setting is environment-locked and cannot be overridden from dashboard: {name}")
        if name == "ALPHAFORGE_ENABLE_LIVE_TRADING" and setting.parse(raw) is True and not live_readiness_pass:
            raise ValueError("Dashboard Settings cannot enable LIVE unless readiness evidence is PASS")
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
