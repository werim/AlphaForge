from pathlib import Path
import re

from alphaforge.config_audit import audit_config
from alphaforge.config_registry import CONTRACT_BY_NAME, decision_filter_config

ROOT = Path(__file__).resolve().parents[1]
PROFILES = [
    ROOT / ".env.example",
    ROOT / ".env.test.example",
    ROOT / ".env.medium.example",
    ROOT / ".env.live.example",
]
RUNTIME_PROFILES = [ROOT / ".env.example", ROOT / ".env.medium.example", ROOT / ".env.live.example"]
REQUIRED_CORE_VARIABLES = {
    "ALPHAFORGE_EXECUTION_MODE",
    "EXECUTION_MODE",
    "ALPHAFORGE_ENABLE_LIVE_TRADING",
    "ALPHAFORGE_ALLOW_LIVE_ORDERS",
    "ALPHAFORGE_REQUIRE_LIVE_QUALIFICATION",
    "ALPHAFORGE_OPERATOR_LIVE_ACKNOWLEDGED",
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "ALPHAFORGE_MAX_DAILY_LOSS_PCT",
    "ALPHAFORGE_MAX_CONCURRENT_POSITIONS",
    "ALPHAFORGE_MIN_SIGNAL_SCORE",
    "ALPHAFORGE_MIN_RR",
    "MIN_EFFECTIVE_RR",
    "ALPHAFORGE_STALE_MARKET_DATA_SEC",
    "ALPHAFORGE_MAX_SPREAD_PCT",
    "ALPHAFORGE_MAX_EXPECTED_SLIPPAGE_PCT",
    "ALPHAFORGE_MAX_ABS_FUNDING_RATE_PCT",
    "MIN_LIQUIDITY_USD",
    "ALPHAFORGE_BACKTEST_TOP_N",
    "ALPHAFORGE_DATABASE_URL",
}
SECRET_KEYS = ("API_KEY", "API_SECRET", "BOT_TOKEN", "WEBHOOK_URL")
ALLOWED_PLACEHOLDER_PATTERNS = (
    "your_",
    "replace_me",
    "https://discord.com/api/webhooks/replace_me",
)


def parse_env(path: Path) -> dict[str, str]:
    values = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.split(" #", 1)[0].strip()
    return values


def as_float(values: dict[str, str], key: str) -> float:
    return float(values[key])


def as_int(values: dict[str, str], key: str) -> int:
    return int(float(values[key]))


def test_all_environment_example_profiles_exist():
    for profile in PROFILES:
        assert profile.exists(), f"missing environment profile {profile.name}"


def test_environment_profiles_contain_required_core_variables():
    for profile in PROFILES:
        values = parse_env(profile)
        missing = REQUIRED_CORE_VARIABLES - values.keys()
        assert not missing, f"{profile.name} missing {sorted(missing)}"


def test_environment_profiles_do_not_contain_real_looking_secrets():
    suspicious = re.compile(r"(AKIA[0-9A-Z]{16}|[A-Za-z0-9_-]{32,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}|xox[baprs]-[A-Za-z0-9-]{10,})")
    for profile in PROFILES:
        values = parse_env(profile)
        for key, value in values.items():
            if key.endswith("URL") and not any(token in key for token in SECRET_KEYS):
                continue
            if any(token in key for token in SECRET_KEYS):
                assert not value or any(p in value for p in ALLOWED_PLACEHOLDER_PATTERNS), f"{profile.name} has non-placeholder {key}"
            assert not suspicious.search(value), f"{profile.name} has real-looking secret in {key}"


def test_live_profile_is_stricter_than_test_profile_for_core_safety_thresholds():
    test = parse_env(ROOT / ".env.test.example")
    live = parse_env(ROOT / ".env.live.example")

    assert as_float(live, "ALPHAFORGE_BACKTEST_RISK_PCT") <= as_float(test, "ALPHAFORGE_BACKTEST_RISK_PCT")
    assert as_float(live, "ALPHAFORGE_MAX_DAILY_LOSS_PCT") <= as_float(test, "ALPHAFORGE_MAX_DAILY_LOSS_PCT")
    assert as_int(live, "ALPHAFORGE_MAX_CONCURRENT_POSITIONS") <= as_int(test, "ALPHAFORGE_MAX_CONCURRENT_POSITIONS")
    assert as_float(live, "ALPHAFORGE_MIN_SIGNAL_SCORE") >= as_float(test, "ALPHAFORGE_MIN_SIGNAL_SCORE")
    assert live["ALPHAFORGE_BLOCK_UNKNOWN_EXPECTANCY"].lower() == "true"
    assert live["ALPHAFORGE_ENABLE_LIVE_TRADING"].lower() == "false"
    assert live["ALPHAFORGE_ALLOW_LIVE_ORDERS"].lower() == "false"
    assert live["ALPHAFORGE_OPERATOR_LIVE_ACKNOWLEDGED"].lower() == "false"


def test_test_profile_is_clearly_marked_non_live_diagnostic():
    text = (ROOT / ".env.test.example").read_text().upper()
    assert "NOT FOR LIVE" in text
    assert "DIAGNOSTIC" in text
    values = parse_env(ROOT / ".env.test.example")
    assert values["ALPHAFORGE_EXECUTION_MODE"] == "BACKTEST"
    assert values["ALPHAFORGE_ENABLE_BINANCE_READONLY_RECONCILIATION"].lower() == "false"
    assert values["ALPHAFORGE_ENABLE_PAPER_TRADING"].lower() == "false"
    assert values["ALPHAFORGE_ENABLE_LIVE_TRADING"].lower() == "false"
    assert values["ALPHAFORGE_ALLOW_LIVE_ORDERS"].lower() == "false"


def test_default_template_is_canonical_paper_burnin_profile():
    values = parse_env(ROOT / ".env.example")
    decision = decision_filter_config(values["ALPHAFORGE_EXECUTION_MODE"], env=values)

    assert values["ALPHAFORGE_EXECUTION_MODE"] == "PAPER"
    assert decision["MODE"] == "PAPER"
    assert decision["RUNTIME_LIMITS_ACTIVE"] is True
    assert values["ALPHAFORGE_ENABLE_PAPER_TRADING"].lower() == "true"
    assert values["ALPHAFORGE_ENABLE_BINANCE_READONLY_RECONCILIATION"].lower() == "true"
    assert values["ALPHAFORGE_ENABLE_LIVE_TRADING"].lower() == "false"
    assert values["ALPHAFORGE_ALLOW_LIVE_ORDERS"].lower() == "false"
    assert values["BINANCE_ENVIRONMENT"] == "production"
    assert values["BINANCE_BASE_URL"] == values["BINANCE_WS_URL"] == ""


def test_default_paper_template_only_fails_audit_for_required_credentials():
    values = parse_env(ROOT / ".env.example")
    report = audit_config(env=values, root=ROOT)

    assert report["warnings"] == []
    assert report["errors"] == [
        "BINANCE_API_KEY missing or placeholder while authenticated reconciliation is enabled",
        "BINANCE_API_SECRET missing or placeholder while authenticated reconciliation is enabled",
    ]


def test_copyable_profiles_do_not_supply_reserved_values():
    for profile in PROFILES:
        values = parse_env(profile)
        supplied = sorted(
            name for name, value in values.items()
            if value and CONTRACT_BY_NAME[name].classification == "RESERVED"
        )
        assert supplied == [], f"{profile.name} supplies reserved values: {supplied}"


def test_runtime_profiles_do_not_contain_reserved_inventory():
    for profile in RUNTIME_PROFILES:
        values = parse_env(profile)
        reserved = sorted(
            name for name in values
            if CONTRACT_BY_NAME[name].classification == "RESERVED"
        )
        assert reserved == [], f"{profile.name} contains reserved variables: {reserved}"


def test_readme_mentions_all_environment_profiles():
    readme = (ROOT / "README.md").read_text()
    for name in [".env.test.example", ".env.medium.example", ".env.live.example"]:
        assert name in readme
