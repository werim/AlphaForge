"""Offline adapter from canonical BACKTEST decision evidence to walk-forward reports."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .walk_forward import (
    HistoricalRow,
    Membership,
    SearchSelectionLineage,
    Segment,
    SegmentRole,
    UniverseMode,
    UniverseProvenance,
    WalkForwardContract,
    build_validation_report,
)


MANIFEST_SCHEMA = "walk_forward_backtest_adapter_v1"


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"historical validation manifest requires boolean {field}")
    return value


def _number(row: Mapping[str, Any], key: str) -> float:
    value = row.get(key)
    if value in (None, ""):
        raise ValueError(f"canonical BACKTEST evidence is missing {key}")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"canonical BACKTEST evidence has invalid {key}") from exc


def _timestamp(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("canonical BACKTEST evidence is missing timestamp")
    try:
        numeric = float(raw)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("canonical BACKTEST evidence has invalid timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("canonical BACKTEST timestamp must include a timezone")
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    seconds = numeric / 1000.0 if abs(numeric) >= 100_000_000_000 else numeric
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


def _cost_semantics(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or ""))
    except json.JSONDecodeError as exc:
        raise ValueError("canonical BACKTEST execution-cost semantics are missing") from exc
    if not isinstance(parsed, Mapping):
        raise ValueError("canonical BACKTEST execution-cost semantics must be an object")
    return dict(parsed)


def historical_rows_from_backtest_decisions(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[HistoricalRow, ...]:
    """Adapt terminal canonical BACKTEST outcomes without deriving new economics."""

    terminal = [
        row
        for row in rows
        if str(row.get("lifecycle_state_after") or "").upper() == "POSITION_CLOSED"
    ]
    if not terminal:
        raise ValueError("BACKTEST evidence contains no terminal position outcomes")
    adapted: list[HistoricalRow] = []
    for row in terminal:
        if str(row.get("mode") or "").upper() != "BACKTEST":
            raise ValueError("historical adapter accepts BACKTEST evidence only")
        row_id = str(row.get("evidence_id") or "").strip()
        symbol = str(row.get("symbol") or "").strip()
        if not row_id or not symbol:
            raise ValueError("canonical BACKTEST evidence requires evidence_id and symbol")
        adapted.append(
            HistoricalRow(
                row_id=row_id,
                timestamp=_timestamp(row.get("timestamp")),
                symbol=symbol,
                regime=str(row.get("regime") or "UNKNOWN"),
                net_return=_number(row, "net_pnl_pct"),
                candidate_rr=_number(row, "candidate_raw_rr"),
                executable_rr=_number(row, "executable_raw_rr"),
                remaining_execution_penalty=_number(row, "remaining_execution_penalty"),
                effective_rr=_number(row, "effective_rr"),
                rr_basis=str(row.get("rr_basis") or ""),
                execution_cost_semantics=_cost_semantics(row.get("execution_cost_semantics")),
            )
        )
    return tuple(adapted)


def contract_from_manifest(payload: Mapping[str, Any]) -> WalkForwardContract:
    if payload.get("schema_version") != MANIFEST_SCHEMA:
        raise ValueError("unsupported historical validation manifest schema")
    contract_payload = payload.get("contract")
    if not isinstance(contract_payload, Mapping):
        raise ValueError("historical validation manifest requires a contract")
    universe_payload = contract_payload.get("universe")
    if not isinstance(universe_payload, Mapping):
        raise ValueError("historical validation manifest requires universe provenance")
    memberships = tuple(
        Membership(**membership)
        for membership in universe_payload.get("memberships", [])
    )
    universe = UniverseProvenance(
        mode=UniverseMode(universe_payload["mode"]),
        identity=str(universe_payload["identity"]),
        source_identity=str(universe_payload["source_identity"]),
        symbols=tuple(map(str, universe_payload["symbols"])),
        memberships=memberships,
        survivorship_bias_protected=_boolean(
            universe_payload["survivorship_bias_protected"],
            "survivorship_bias_protected",
        ),
    )
    raw_segments = contract_payload.get("segments")
    if not isinstance(raw_segments, list):
        raise ValueError("historical validation manifest requires segment provenance")
    segments: list[Segment] = []
    for raw in raw_segments:
        if not isinstance(raw, Mapping):
            raise ValueError("historical validation segment provenance is invalid")
        lineage_payload = raw.get("search_selection_lineage")
        lineage = None
        if isinstance(lineage_payload, Mapping):
            lineage = SearchSelectionLineage(
                lineage_id=str(lineage_payload["lineage_id"]),
                candidate_variant_ids=tuple(map(str, lineage_payload["candidate_variant_ids"])),
                evidence_evaluation_count=int(lineage_payload["evidence_evaluation_count"]),
                influenced_selection=_boolean(
                    lineage_payload["influenced_selection"], "influenced_selection"
                ),
                declared_fresh=_boolean(lineage_payload["declared_fresh"], "declared_fresh"),
            )
        segments.append(
            Segment(
                segment_id=str(raw["segment_id"]),
                window_id=str(raw["window_id"]),
                role=SegmentRole(raw["role"]),
                start=str(raw["start"]),
                end=str(raw["end"]),
                git_sha=str(raw["git_sha"]),
                strategy_config_hash=str(raw["strategy_config_hash"]),
                config_hash=str(raw["config_hash"]),
                data_hash=str(raw["data_hash"]),
                universe_identity=str(raw["universe_identity"]),
                frozen_at=str(raw["frozen_at"]),
                calibration_segment_ids=tuple(map(str, raw["calibration_segment_ids"])),
                search_selection_lineage=lineage,
            )
        )
    return WalkForwardContract(segments=tuple(segments), universe=universe)


def generate_backtest_walk_forward_report(
    manifest_path: str | Path, output_path: str | Path
) -> dict[str, Any]:
    """Generate a deterministic machine-readable report from existing artifacts."""

    manifest_file = Path(manifest_path)
    manifest = json.loads(manifest_file.read_text())
    if not isinstance(manifest, Mapping):
        raise ValueError("historical validation manifest must be an object")
    relative_csv = manifest.get("decision_evidence_csv")
    expected_sha256 = str(manifest.get("decision_evidence_sha256") or "")
    if not relative_csv or len(expected_sha256) != 64:
        raise ValueError("manifest requires decision evidence path and SHA256")
    evidence_file = manifest_file.parent / str(relative_csv)
    actual_sha256 = hashlib.sha256(evidence_file.read_bytes()).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError("decision evidence SHA256 does not match the manifest")
    with evidence_file.open(newline="") as handle:
        rows = tuple(csv.DictReader(handle))
    contract = contract_from_manifest(manifest)
    report = build_validation_report(
        contract,
        historical_rows_from_backtest_decisions(rows),
        min_effective_rr=float(manifest["min_effective_rr"]),
    )
    report["source_artifact"] = {
        "path": str(relative_csv),
        "sha256": actual_sha256,
        "mode": "BACKTEST",
    }
    Path(output_path).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    generate_backtest_walk_forward_report(args.manifest, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
