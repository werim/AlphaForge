"""Deterministic, fail-closed remediation for the repository ``.env`` file."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Mapping

from alphaforge.config_audit import audit_config
from alphaforge.config_registry import CONFIG_REGISTRY
from alphaforge.env_contract import parse_dotenv, repository_root

AUTO_FIX_SAFE = "AUTO_FIX_SAFE"
MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
CODE_DEFECT = "CODE_DEFECT"
SECRET_REQUIRED = "SECRET_REQUIRED"
UNSUPPORTED_VALUE = "UNSUPPORTED_VALUE"
NO_ACTION = "NO_ACTION"
MAX_ITERATIONS = 5
SECRET_KEYS = {s.env_name for s in CONFIG_REGISTRY if s.secret}
ALIASES = {alias: s.env_name for s in CONFIG_REGISTRY for alias in s.deprecated_aliases}
MARKET_ALIASES = {"USDT_M": "USD_M", "USD-M": "USD_M", "USDT-M": "USD_M"}
LINE_RE = re.compile(r"^(?P<prefix>\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=.*$")


@dataclass(frozen=True)
class Change:
    setting: str
    source: str
    classification: str
    current_safe_value: object
    proposed_safe_value: object
    reason: str
    target_file: str | None
    line_number: int | None
    action: str = "NO_FILE_CHANGE"


def _safe(key: str, value: object) -> object:
    return {"is_set": bool(value)} if key in SECRET_KEYS else value


def _line_entries(text: str) -> list[tuple[int, str, str]]:
    entries = []
    for number, line in enumerate(text.splitlines(), 1):
        match = LINE_RE.match(line)
        if match and not line.lstrip().startswith("#"):
            key = match.group("key")
            # parse_dotenv is authoritative for syntax/value semantics.
            value = parse_dotenv_text(line, number)
            entries.append((number, key, value))
    return entries


def parse_dotenv_text(line: str, number: int) -> str:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(line + "\n")
        name = handle.name
    try:
        return next(iter(parse_dotenv(Path(name)).values()))
    except ValueError as exc:
        raise ValueError(f"line {number}: {str(exc).split(':', 2)[-1].strip()}") from exc
    finally:
        Path(name).unlink(missing_ok=True)


def _source(key: str, file_values: Mapping[str, str], process_env: Mapping[str, str]) -> str:
    if key in process_env:
        return "PROCESS_ENV"
    return "DOTENV" if key in file_values else "DEFAULT"


def build_plan(path: Path, *, process_env: Mapping[str, str] | None = None) -> tuple[list[Change], list[dict[str, object]]]:
    """Return ordered safe changes and unresolved findings without mutation."""
    process_env = os.environ if process_env is None else process_env
    text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    entries = _line_entries(text)
    values = parse_dotenv(path) if path.exists() else {}
    changes: list[Change] = []
    unresolved: list[dict[str, object]] = []
    by_key: dict[str, list[tuple[int, str]]] = {}
    for number, key, value in entries:
        by_key.setdefault(key, []).append((number, value))

    for key in sorted(by_key):
        definitions = by_key[key]
        if len(definitions) > 1:
            if len({value for _, value in definitions}) == 1 and key not in process_env:
                for number, value in definitions[:-1]:
                    changes.append(Change(key, "DOTENV", AUTO_FIX_SAFE, _safe(key, value), _safe(key, value),
                        "duplicate_identical_definition", ".env", number, "COMMENT_OUT"))
            else:
                unresolved.append(asdict(Change(key, _source(key, values, process_env), MANUAL_REVIEW_REQUIRED,
                    _safe(key, definitions[-1][1]), _safe(key, definitions[-1][1]),
                    "duplicate_conflicting_definitions", ".env", definitions[-1][0])))

    for alias, canonical in sorted(ALIASES.items()):
        if alias not in values or not values[alias].strip():
            continue
        number = by_key[alias][-1][0]
        if alias in process_env:
            unresolved.append(asdict(Change(alias, "PROCESS_ENV", MANUAL_REVIEW_REQUIRED, _safe(alias, process_env[alias]),
                _safe(alias, ""), "process_environment_overrides_dotenv", None, None)))
        elif canonical in values and values[canonical].strip():
            changes.append(Change(alias, "deprecated alias", AUTO_FIX_SAFE, _safe(alias, values[alias]), _safe(alias, ""),
                f"canonical_{canonical}_is_preserved", ".env", number, "COMMENT_OUT"))

    key = "BINANCE_DEFAULT_MARKET_TYPE"
    if key in values and values[key].strip().upper() in MARKET_ALIASES and key not in process_env:
        old = values[key].strip()
        proposed = MARKET_ALIASES[old.upper()]
        if old != proposed:
            changes.append(Change(key, "DOTENV", AUTO_FIX_SAFE, old, proposed,
                "supported_enum_normalization", ".env", by_key[key][-1][0], "REPLACE_VALUE"))

    key = "ALPHAFORGE_MAX_DAILY_LOSS_PCT"
    if key in values and key not in process_env:
        raw = values[key].strip()
        if raw == "5.0":
            changes.append(Change(key, "DOTENV", AUTO_FIX_SAFE, raw, "0.05",
                "documented_legacy_percentage_points_to_fraction", ".env", by_key[key][-1][0], "REPLACE_VALUE"))
        else:
            try:
                if float(raw) > 1:
                    unresolved.append(asdict(Change(key, "DOTENV", MANUAL_REVIEW_REQUIRED, raw, raw,
                        "ambiguous_risk_unit_not_auto_converted", ".env", by_key[key][-1][0])))
            except ValueError:
                pass

    # Every AlphaForge/Binance process override is reported when it conflicts
    # with the file; editing .env cannot remediate its effective value.
    for key in sorted(set(values) & set(process_env)):
        if key.startswith(("ALPHAFORGE", "BINANCE")) and str(process_env[key]) != values[key] and not any(u["setting"] == key for u in unresolved):
            unresolved.append(asdict(Change(key, "PROCESS_ENV", MANUAL_REVIEW_REQUIRED, _safe(key, process_env[key]),
                _safe(key, values[key]), "process_environment_overrides_dotenv", None, None)))
    changes.sort(key=lambda c: (c.line_number or 0, c.setting, c.action))
    unresolved.sort(key=lambda row: (str(row["setting"]), str(row["reason"])))
    return changes, unresolved


def _render(text: str, changes: list[Change]) -> str:
    newline = "\r\n" if "\r\n" in text else "\n"
    had_final = text.endswith(("\n", "\r"))
    lines = text.splitlines()
    for change in sorted(changes, key=lambda c: c.line_number or 0, reverse=True):
        if change.classification != AUTO_FIX_SAFE or change.line_number is None:
            continue
        index = change.line_number - 1
        if change.action == "COMMENT_OUT":
            lines[index] = f"# AlphaForge config_fix: {change.reason}; {lines[index]}"
        elif change.action == "REPLACE_VALUE":
            match = LINE_RE.match(lines[index])
            assert match
            lines[index] = f"{match.group('prefix')}{change.setting}={change.proposed_safe_value}"
    return newline.join(lines) + (newline if had_final else "")


def _atomic_apply(path: Path, changes: list[Change], *, now: datetime | None = None) -> Path:
    original = path.read_bytes().decode("utf-8-sig") if path.exists() else ""
    # Refuse before backup/write when syntax is invalid.
    if path.exists():
        parse_dotenv(path)
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.backup-{stamp}")
    suffix = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.backup-{stamp}-{suffix}")
        suffix += 1
    shutil.copy2(path, backup) if path.exists() else backup.write_text("", encoding="utf-8")
    rendered = _render(original, changes)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        parse_dotenv(Path(temporary))
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return backup


def _controlled_env(path: Path, process_env: Mapping[str, str]) -> dict[str, str]:
    values = parse_dotenv(path) if path.exists() else {}
    return {**values, **{k: v for k, v in process_env.items() if k.startswith(("ALPHAFORGE", "BINANCE"))}}


def _finding_hash(audit: Mapping[str, object], unresolved: list[dict[str, object]]) -> str:
    normalized = {"errors": sorted(map(str, audit.get("errors", []))), "unresolved": unresolved}
    return hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()


def _audit_unresolved(audit: Mapping[str, object], existing: list[dict[str, object]]) -> list[dict[str, object]]:
    """Represent every otherwise-unhandled audit error without guessing a fix."""
    reasons = {str(row.get("reason")) for row in existing}
    out = list(existing)
    for error in map(str, audit.get("errors", [])):
        if error in reasons:
            continue
        setting = next((name for name in sorted({s.env_name for s in CONFIG_REGISTRY} | set(ALIASES)) if name in error), "CONFIG_CONTRACT")
        classification = SECRET_REQUIRED if setting in SECRET_KEYS and "missing" in error else MANUAL_REVIEW_REQUIRED
        out.append(asdict(Change(setting, "EFFECTIVE_CONFIG", classification, {"is_set": False} if setting in SECRET_KEYS else None,
            {"is_set": True} if setting in SECRET_KEYS else None, error, None, None)))
    return sorted(out, key=lambda row: (str(row["setting"]), str(row["reason"])))


def run(*, apply: bool = False, root: Path | None = None, process_env: Mapping[str, str] | None = None,
        max_iterations: int = MAX_ITERATIONS, audit_fn: Callable[..., dict[str, object]] = audit_config) -> tuple[dict[str, object], int]:
    root = repository_root(root)
    target = root / ".env"
    process_env = dict(os.environ if process_env is None else process_env)
    try:
        before = audit_fn(env=_controlled_env(target, process_env), root=root)
        changes, unresolved = build_plan(target, process_env=process_env)
    except (OSError, UnicodeError, ValueError) as exc:
        return {"audit_before": None, "target_file": ".env", "proposed_changes": [], "applied_changes": [],
                "unresolved_findings": [{"classification": UNSUPPORTED_VALUE, "reason": str(exc)}],
                "audit_after": None, "iterations": 0, "final_status": "FAILED", "next_safe_commands": []}, 3
    proposed = [asdict(c) for c in changes]
    unresolved = _audit_unresolved(before, unresolved)
    commands = [f"Remove-Item Env:{u['setting']} -ErrorAction SilentlyContinue" for u in unresolved
                if u.get("source") == "PROCESS_ENV" and u.get("setting") not in SECRET_KEYS]
    result: dict[str, object] = {"audit_before": before, "target_file": ".env", "proposed_changes": proposed,
        "applied_changes": [], "unresolved_findings": unresolved, "audit_after": None, "iterations": 0,
        "final_status": "PASS", "next_safe_commands": commands}
    if not apply:
        if unresolved:
            result["final_status"] = "MANUAL_REVIEW_REQUIRED"
        elif changes:
            result["final_status"] = "CHANGES_PROPOSED"
        elif before["status"] == "FAIL":
            result["final_status"] = "MANUAL_REVIEW_REQUIRED"
        return result, 0 if result["final_status"] == "PASS" else 1

    seen: set[str] = set()
    all_applied: list[dict[str, object]] = []
    backup: Path | None = None
    audit = before
    for iteration in range(1, max_iterations + 1):
        changes, unresolved = build_plan(target, process_env=process_env)
        unresolved = _audit_unresolved(audit, unresolved)
        if not changes:
            if audit["status"] != "FAIL" and not unresolved:
                result["final_status"] = "APPLIED_AND_PASS" if all_applied else "PASS"
            else:
                signature = _finding_hash(audit, unresolved)
                if signature in seen:
                    result["final_status"] = "STALLED"
                else:
                    result["final_status"] = "MANUAL_REVIEW_REQUIRED" if unresolved else "APPLIED_WITH_REMAINING_ERRORS"
            break
        signature = _finding_hash(audit, unresolved)
        if signature in seen:
            result["final_status"] = "STALLED"
            break
        seen.add(signature)
        try:
            current_backup = _atomic_apply(target, changes)
            backup = backup or current_backup
        except (OSError, UnicodeError, ValueError) as exc:
            result["final_status"] = "FAILED"
            result["unresolved_findings"] = [{"classification": UNSUPPORTED_VALUE, "reason": str(exc)}]
            return result, 3
        all_applied.extend(asdict(c) for c in changes)
        result["iterations"] = iteration
        audit = audit_fn(env=_controlled_env(target, process_env), root=root)
    else:
        result["final_status"] = "STALLED"
    result["backup_path"] = str(backup.relative_to(root)) if backup else None
    result["applied_changes"] = all_applied
    result["audit_after"] = audit
    _, remaining = build_plan(target, process_env=process_env)
    result["unresolved_findings"] = _audit_unresolved(audit, remaining)
    return result, 0 if result["final_status"] in {"PASS", "APPLIED_AND_PASS"} else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="back up and atomically apply provably safe .env changes")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON (the default output is also JSON)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 4 if exc.code else 0
    report, code = run(apply=args.apply)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
