"""Canonical environment contract, dotenv bootstrap, and Binance resolution.

This module deliberately contains metadata as well as parsing.  Templates and the
audit CLI consume the same registry, so a documented key cannot become decorative.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping
import os
from urllib.parse import urlparse

MODES = ("BACKTEST", "PAPER", "LIVE")
PRODUCTION_REST_URL = "https://fapi.binance.com"
PRODUCTION_WS_URL = "wss://fstream.binance.com"
TESTNET_REST_URL = "https://testnet.binancefuture.com"
TESTNET_WS_URL = "wss://stream.binancefuture.com"
DEMO_REST_URL = "https://demo-fapi.binance.com"


@dataclass(frozen=True, slots=True)
class EnvContractEntry:
    name: str
    canonical_name: str
    classification: str
    value_type: str
    default: object
    applies_to: tuple[str, ...]
    consumed_by: str
    restart_required: bool = True
    secret: bool = False
    deprecated: bool = False
    description: str = ""
    behavioral_test: str = ""
    unsupported_reason: str | None = None
    unsupported_explanation: str | None = None
    remove_from_templates: bool = False
    intended_future_subsystem: str | None = None

    def public_dict(self) -> dict[str, object]:
        row = asdict(self)
        row["type"] = row.pop("value_type")
        return row


@dataclass(frozen=True, slots=True)
class DotenvBootstrap:
    path: str | None
    loaded: bool
    keys_loaded: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BinanceEnvironment:
    environment: str
    rest_base_url: str
    ws_base_url: str
    rest_source: str
    ws_source: str

    @property
    def resolution_source(self) -> str:
        return self.rest_source if self.rest_source == self.ws_source else f"rest={self.rest_source},ws={self.ws_source}"


_last_bootstrap = DotenvBootstrap(None, False, ())


def repository_root(start: Path | None = None) -> Path:
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "src" / "alphaforge").is_dir():
            return candidate
    return here


def bootstrap_environment(root: Path | None = None, *, environ: dict[str, str] | None = None) -> DotenvBootstrap:
    """Load the repository ``.env`` once, without overriding process values."""
    global _last_bootstrap
    target = repository_root(root) / ".env"
    destination = os.environ if environ is None else environ
    if not target.is_file():
        result = DotenvBootstrap(str(target), False, ())
    else:
        parsed = parse_dotenv(target)
        loaded: list[str] = []
        for key, value in parsed.items():
            if value is not None and key not in destination:
                destination[key] = value
                loaded.append(key)
        result = DotenvBootstrap(str(target), True, tuple(sorted(loaded)))
    if environ is None:
        _last_bootstrap = result
    return result


def dotenv_status() -> DotenvBootstrap:
    return _last_bootstrap


def parse_dotenv(path: Path) -> dict[str, str]:
    """Parse the supported dotenv subset, preserving hashes inside quotes."""
    values: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        if "=" not in stripped:
            raise ValueError(f"{path}:{number}: expected KEY=VALUE")
        key, raw = stripped.split("=", 1)
        key = key.strip()
        if not key or not (key[0].isalpha() or key[0] == "_") or not key.replace("_", "a").isalnum():
            raise ValueError(f"{path}:{number}: invalid environment key")
        raw = raw.strip()
        if raw[:1] in {"'", '"'}:
            quote = raw[0]
            end = raw.find(quote, 1)
            tail = raw[end + 1 :].strip() if end >= 0 else ""
            if end < 0 or (tail and not tail.startswith("#")):
                raise ValueError(f"{path}:{number}: malformed quoted value")
            value = raw[1:end]
        else:
            value = raw.split("#", 1)[0].rstrip()
        values[key] = value
    return values


def parse_bool(name: str, raw: str | bool) -> bool:
    if isinstance(raw, bool):
        return raw
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _valid_url(name: str, value: str, schemes: set[str]) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in schemes or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError(f"{name} must be an absolute {sorted(schemes)} URL without credentials")
    return value.rstrip("/")


def resolve_binance_environment(
    env: Mapping[str, str], *, require_websocket: bool = True
) -> BinanceEnvironment:
    """Resolve a fail-closed Binance endpoint identity.

    Demo Trading has a canonical REST endpoint but this project does not claim a
    canonical demo websocket endpoint.  Read-only REST consumers may therefore
    opt out of websocket resolution; streaming/runtime consumers may not.
    """
    explicit_environment = str(env.get("BINANCE_ENVIRONMENT", "")).strip().lower()
    legacy_present = str(env.get("BINANCE_TESTNET", "")).strip() != ""
    legacy_testnet = parse_bool("BINANCE_TESTNET", env["BINANCE_TESTNET"]) if legacy_present else False
    if explicit_environment:
        if explicit_environment not in {"production", "testnet", "demo"}:
            raise ValueError("BINANCE_ENVIRONMENT must be production, testnet, or demo")
        if legacy_present and legacy_testnet != (explicit_environment == "testnet"):
            raise ValueError("BINANCE_ENVIRONMENT contradicts deprecated BINANCE_TESTNET")
        environment = explicit_environment
        source = "process_env" if isinstance(env, os._Environ) else "environment"
    else:
        environment = "testnet" if legacy_testnet else "production"
        source = "alias" if legacy_present else "default"
    if environment == "demo":
        derived_rest, derived_ws = DEMO_REST_URL, ""
    elif environment == "testnet":
        derived_rest, derived_ws = TESTNET_REST_URL, TESTNET_WS_URL
    else:
        derived_rest, derived_ws = PRODUCTION_REST_URL, PRODUCTION_WS_URL
    rest = _valid_url("BINANCE_BASE_URL", str(env.get("BINANCE_BASE_URL") or derived_rest), {"http", "https"})
    ws_raw = str(env.get("BINANCE_WS_URL") or derived_ws)
    if require_websocket and not ws_raw:
        raise ValueError("BINANCE_WS_URL is required for websocket consumers in demo environment")
    ws = _valid_url("BINANCE_WS_URL", ws_raw, {"ws", "wss"}) if ws_raw else ""
    rest_source = "process_env" if env.get("BINANCE_BASE_URL") else source
    ws_source = "process_env" if env.get("BINANCE_WS_URL") else source
    # Known endpoints may not be crossed. Custom paired overrides are allowed and audited.
    known_rest = {PRODUCTION_REST_URL: "production", TESTNET_REST_URL: "testnet", DEMO_REST_URL: "demo"}
    known_ws = {PRODUCTION_WS_URL: "production", TESTNET_WS_URL: "testnet"}
    for url, known, label in ((rest, known_rest, "REST"), (ws, known_ws, "websocket")):
        if url in known and known[url] != environment:
            raise ValueError(f"Binance {label} endpoint is {known[url]} but environment is {environment}")
    if rest in known_rest and ws in known_ws and known_rest[rest] != known_ws[ws]:
        raise ValueError("Binance REST and websocket endpoints select different environments")
    return BinanceEnvironment(environment, rest, ws, rest_source, ws_source)
