"""Operator read-only Binance reconciliation probe."""
from __future__ import annotations

import argparse
import json
import os

from alphaforge.binance_reconciliation_provider import (
    BinanceReadonlyReconciliationProvider,
    load_reconciliation_settings,
)
from alphaforge.env_contract import bootstrap_environment


def run(symbols: list[str]) -> dict[str, object]:
    bootstrap_environment()
    try:
        settings = load_reconciliation_settings(os.environ)
        provider = BinanceReadonlyReconciliationProvider(config=settings, tracked_symbols=lambda: set(symbols))
        snapshot = dict(provider.snapshot())
    except (TypeError, ValueError, RuntimeError) as exc:
        return {"status": "INCOMPLETE", "errors": [f"{exc.__class__.__name__}:configuration_or_authentication_failed_redacted"]}
    status = "COMPLETE" if snapshot.get("evidence_status") == "COMPLETE" else "INCOMPLETE"
    return {"status": status, "symbols": sorted(set(symbols)), "snapshot": snapshot}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", required=True)
    args = parser.parse_args(argv)
    report = run(args.symbols)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0 if report["status"] == "COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
