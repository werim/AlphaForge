"""Conservative, dry-run-first remediation for AlphaForge dotenv files."""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path

from alphaforge.config_check import audit_settings
from alphaforge.env_contract import parse_dotenv, repository_root

# Remediation is deliberately narrower than the complete alias registry.  Risk,
# strategy, LIVE, and secret controls require an operator decision and are never
# rewritten by this command.
_SAFE_ALIASES = {"BINANCE_RECV_WINDOW_MS": "ALPHAFORGE_BINANCE_RECV_WINDOW_MS"}


def _plan(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    lines = path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
    parsed = parse_dotenv(path)
    actions: list[dict[str, str]] = []
    for alias, canonical in sorted(_SAFE_ALIASES.items()):
        if alias not in parsed:
            continue
        if canonical in parsed:
            if parsed[canonical].strip() != parsed[alias].strip():
                actions.append({"action": "blocked", "reason": "ambiguous_alias_conflict", "setting": canonical})
                continue
            pattern = re.compile(rf"^(\s*(?:export\s+)?){re.escape(alias)}(\s*=)")
            matches = [index for index, line in enumerate(lines) if pattern.match(line)]
            if len(matches) == 1:
                del lines[matches[0]]
                actions.append({"action": "remove_equal_alias", "setting": canonical})
            continue
        pattern = re.compile(rf"^(\s*(?:export\s+)?){re.escape(alias)}(\s*=)")
        matches = [index for index, line in enumerate(lines) if pattern.match(line)]
        if len(matches) != 1:
            actions.append({"action": "blocked", "reason": "ambiguous_duplicate", "setting": canonical})
            continue
        lines[matches[0]] = pattern.sub(rf"\1{canonical}\2", lines[matches[0]], count=1)
        actions.append({"action": "rename_alias", "setting": canonical})
    return lines, actions


def _atomic_write_with_backup(path: Path, content: str) -> Path:
    backup = path.with_name(path.name + ".bak")
    for target, data in ((backup, path.read_bytes()), (path, content.encode("utf-8"))):
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return backup


def remediate(path: Path, *, apply: bool = False) -> dict[str, object]:
    if not path.is_file():
        return {"status": "NO_CONFIG", "mode": "APPLY" if apply else "DRY_RUN", "changed": False,
                "actions": [], "backup": None, "audit": None}
    original = path.read_text(encoding="utf-8-sig")
    lines, actions = _plan(path)
    proposed = "".join(lines)
    changed = proposed != original
    blocked = any(row["action"] == "blocked" for row in actions)
    backup = None
    if apply and changed and not blocked:
        backup = _atomic_write_with_backup(path, proposed)
    audit_path = path if apply and changed and not blocked else None
    if audit_path is None:
        fd, name = tempfile.mkstemp(prefix=".alphaforge-config-audit-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(proposed if not blocked else original)
            audit = audit_settings(env=parse_dotenv(Path(name)))
        finally:
            os.unlink(name)
    else:
        audit = audit_settings(env=parse_dotenv(audit_path))
    return {"status": "BLOCKED" if blocked else "PASS", "mode": "APPLY" if apply else "DRY_RUN",
            "changed": changed and not blocked, "actions": actions,
            "backup": str(backup) if backup else None, "audit": audit}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="atomically apply the proposed safe changes")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    parser.add_argument("--path", type=Path, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    path = args.path or repository_root() / ".env"
    result = remediate(path, apply=args.apply)
    print(json.dumps(result, sort_keys=True))
    return 2 if result["status"] == "BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
