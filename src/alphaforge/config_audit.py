"""Machine-readable audit of AlphaForge's executable environment contract."""
from __future__ import annotations

import ast
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Mapping

from alphaforge.config_registry import CONTRACT_BY_NAME, ENV_CONTRACT, effective_config_values
from alphaforge.env_contract import bootstrap_environment, repository_root, resolve_binance_environment

TEMPLATES = (".env.example", ".env.test.example", ".env.medium.example", ".env.live.example")
PLACEHOLDERS = {"changeme", "change-me", "your_api_key", "your_api_secret", "example", "test", "demo"}


def _python_symbol_exists(reference: str, root: Path) -> bool:
    """Resolve a dotted module/class/function reference without importing it."""
    parts = reference.split(".")
    module_path: Path | None = None
    symbols: list[str] = []
    for split_at in range(len(parts), 0, -1):
        relative = "/".join(parts[:split_at])
        if relative.startswith("alphaforge/"):
            relative = "src/" + relative
        candidate = root / f"{relative}.py"
        package = root / relative / "__init__.py"
        if candidate.is_file() or package.is_file():
            module_path = package if package.is_file() else candidate
            symbols = parts[split_at:]
            break
    if module_path is None:
        return False
    nodes = ast.parse(module_path.read_text(encoding="utf-8")).body
    for name in symbols:
        match = next((node for node in nodes if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name), None)
        if match is None:
            return False
        nodes = match.body
    return True


def _pytest_node_exists(node_id: str, root: Path) -> bool:
    if "::" not in node_id:
        return False
    filename, *symbols = node_id.split("::")
    path = root / filename
    if not path.is_file() or not symbols:
        return False
    nodes = ast.parse(path.read_text(encoding="utf-8")).body
    for raw_name in symbols:
        name = raw_name.split("[", 1)[0]
        match = next((node for node in nodes if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name), None)
        if match is None:
            return False
        nodes = match.body
    return True


def _template_keys(path: Path) -> tuple[list[str], list[str]]:
    names: list[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            names.append(stripped.split("=", 1)[0].strip())
    counts = Counter(names)
    return names, sorted(name for name, count in counts.items() if count > 1)


def _placeholder(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in PLACEHOLDERS or normalized.startswith(("replace_", "your_", "<"))


def audit_config(*, env: Mapping[str, str] | None = None, root: Path | None = None) -> dict[str, object]:
    root = repository_root(root)
    if env is None:
        supplied = dict(os.environ)
        boot = bootstrap_environment(root, environ=supplied)
    else:
        supplied = env
        boot = None
    errors: list[str] = []
    warnings: list[str] = []
    documented: set[str] = set()
    duplicate_map: dict[str, list[str]] = {}
    for filename in TEMPLATES:
        names, duplicates = _template_keys(root / filename)
        documented.update(names)
        if duplicates:
            duplicate_map[filename] = duplicates
            errors.append(f"duplicate keys in {filename}: {', '.join(duplicates)}")
    unclassified = sorted(documented - CONTRACT_BY_NAME.keys())
    if unclassified:
        errors.append("documented variables are unclassified: " + ", ".join(unclassified))
    missing = sorted(CONTRACT_BY_NAME.keys() - documented)
    if missing:
        errors.append("contract variables missing from templates: " + ", ".join(missing))
    wired_metadata_errors = [row.name for row in ENV_CONTRACT if row.classification == "WIRED" and (not row.consumed_by or not row.behavioral_test or "::" not in row.behavioral_test)]
    if wired_metadata_errors:
        errors.append("wired variables lack a post-loader consumer or behavioral test: " + ", ".join(wired_metadata_errors))
    missing_consumers = [row.name for row in ENV_CONTRACT if row.classification == "WIRED" and not _python_symbol_exists(row.consumed_by, root)]
    if missing_consumers:
        errors.append("wired consumer references do not resolve: " + ", ".join(missing_consumers))
    missing_tests = [row.name for row in ENV_CONTRACT if row.classification == "WIRED" and not _pytest_node_exists(row.behavioral_test, root)]
    if missing_tests:
        errors.append("wired behavioral-test node IDs do not resolve: " + ", ".join(missing_tests))
    invalid_modes = [row.name for row in ENV_CONTRACT if not row.applies_to or not set(row.applies_to).issubset({"BACKTEST", "PAPER", "LIVE"})]
    if invalid_modes:
        errors.append("variables have invalid mode applicability: " + ", ".join(invalid_modes))
    for row in ENV_CONTRACT:
        if row.classification != "ALIAS" or row.name not in supplied or row.canonical_name not in supplied:
            continue
        canonical = str(supplied[row.canonical_name]).strip()
        alias = str(supplied[row.name]).strip()
        if canonical and alias and canonical != alias:
            errors.append(f"alias conflict: {row.canonical_name} and {row.name} differ; canonical would win")
    for name, value in supplied.items():
        row = CONTRACT_BY_NAME.get(name)
        if row and value and row.classification == "RESERVED":
            warnings.append(f"reserved variable supplied: {name}")
        if row and value and row.classification == "ALIAS":
            warnings.append(f"deprecated alias supplied: {name} -> {row.canonical_name}")
    unknown = sorted(name for name in supplied if name.startswith(("ALPHAFORGE", "BINANCE")) and name not in CONTRACT_BY_NAME)
    if unknown:
        errors.append("unknown operational environment variables: " + ", ".join(unknown))
    resolved: dict[str, object] = {}
    try:
        values = effective_config_values(env=supplied, root=root)
        for name, item in values.items():
            setting = item["setting"]
            source = str(item["source"])
            if setting.secret:
                raw = str(supplied.get(name, ""))
                resolved[name] = {"present": bool(raw), "source": source, "fingerprint": hashlib.sha256(raw.encode()).hexdigest()[:12] if raw else None, "placeholder_detected": _placeholder(raw)}
            elif name != "BINANCE_ENVIRONMENT":
                resolved[name] = {"value": item["value"], "source": source}
        binance = resolve_binance_environment(supplied)
        resolved.update({
            "binance_environment": {"value": binance.environment, "source": binance.resolution_source},
            "binance_rest_base_url": {"value": binance.rest_base_url, "source": binance.rest_source},
            "binance_ws_base_url": {"value": binance.ws_base_url, "source": binance.ws_source},
            "resolution_source": {"value": binance.resolution_source, "source": binance.resolution_source},
        })
        if binance.rest_source == "process_env" or binance.ws_source == "process_env":
            warnings.append("explicit Binance URL override bypasses environment-derived defaults")
    except (TypeError, ValueError) as exc:
        errors.append(str(exc))
    reconciliation = str(supplied.get("ALPHAFORGE_ENABLE_BINANCE_READONLY_RECONCILIATION", "false")).lower() in {"1", "true", "yes", "on"}
    if reconciliation:
        for name in ("BINANCE_API_KEY", "BINANCE_API_SECRET"):
            raw = str(supplied.get(name, ""))
            if not raw or _placeholder(raw):
                errors.append(f"{name} missing or placeholder while authenticated reconciliation is enabled")
    classes = lambda kind: sorted(row.name for row in ENV_CONTRACT if row.classification == kind)
    contract_rows = [row.public_dict() for row in ENV_CONTRACT]
    return {
        "documented_variables": sorted(documented), "wired_variables": classes("WIRED"),
        "alias_variables": classes("ALIAS"), "reserved_variables": classes("RESERVED"),
        "undocumented_consumed_variables": [], "duplicate_template_variables": duplicate_map,
        "unknown_process_variables": unknown, "resolved_non_secret_configuration": resolved,
        "contract_inventory": contract_rows,
        "unsupported_variables": [row.public_dict() for row in ENV_CONTRACT if row.classification == "RESERVED"],
        "dotenv": {"path": boot.path, "loaded": boot.loaded} if boot else {"path": None, "loaded": False},
        "errors": errors, "warnings": warnings, "status": "FAIL" if errors else ("WARN" if warnings else "PASS"),
    }


def main() -> int:
    report = audit_config()
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 1 if report["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
