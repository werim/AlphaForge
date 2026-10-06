"""PAPER-only counterfactual position-management proposal evidence.

Issue #612 deliberately keeps this path separate from authoritative position
management.  It observes closed-candle PAPER evidence and persists deterministic
proposal/denial records; it never returns executable management actions.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import text

from alphaforge.burnin import canonical_hash, utc_now

SHADOW_SCHEMA_VERSION = "issue612_position_management_shadow_v1"
SHADOW_ACTIONS = (
    "WOULD_TIGHTEN_STOP",
    "WOULD_PARTIAL_EXIT",
    "WOULD_ENABLE_TRAILING",
    "WOULD_PROTECTIVE_EXIT",
)

SHADOW_DDL = """
CREATE TABLE IF NOT EXISTS burnin_position_management_shadow_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL UNIQUE,
    proposal_hash TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    burnin_run_id TEXT NOT NULL,
    position_entry_run_id TEXT NOT NULL,
    release_id TEXT NOT NULL,
    git_commit TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    strategy_config_hash TEXT NOT NULL,
    trade_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    proposal_time TEXT NOT NULL,
    closed_candle_time TEXT,
    proposed_action TEXT NOT NULL,
    eligibility_status TEXT NOT NULL,
    reason TEXT NOT NULL,
    observed_price REAL,
    mfe_r REAL,
    mae_r REAL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    schema_version TEXT NOT NULL
)
"""


def _exec(conn: Any, sql: str, params: Mapping[str, Any] | None = None):
    return conn.execute(sql if isinstance(conn, sqlite3.Connection) else text(sql), params or {})


def bootstrap_position_management_shadow_schema(conn: Any) -> None:
    _exec(conn, SHADOW_DDL)


def shadow_table_exists(conn: Any) -> bool:
    row = _exec(
        conn,
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='burnin_position_management_shadow_proposals'",
    ).fetchone()
    return row is not None


def _finite(value: Any, *, positive: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    if positive and parsed <= 0:
        return None
    return parsed


def _dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _position_provenance(position: Mapping[str, Any]) -> dict[str, Any]:
    raw = position.get("source_provenance")
    if isinstance(raw, Mapping):
        return dict(raw)
    raw = position.get("source_provenance_json")
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _normalize_candles(
    candles: Sequence[Mapping[str, Any]] | None,
    *,
    entry_time: datetime,
    proposal_time: datetime,
) -> tuple[list[dict[str, Any]], list[str]]:
    unique: dict[datetime, dict[str, Any]] = {}
    errors: list[str] = []
    for index, raw in enumerate(candles or ()):
        if not isinstance(raw, Mapping):
            errors.append(f"CANDLE_{index}_NOT_MAPPING")
            continue
        if raw.get("is_closed") is False or raw.get("closed") is False:
            # Explicitly open candles are ignored; they never participate in
            # proposal evidence or MFE/MAE.
            continue
        timestamp = _dt(raw.get("timestamp") or raw.get("open_time") or raw.get("time"))
        if timestamp is None:
            errors.append(f"CANDLE_{index}_TIMESTAMP_MALFORMED")
            continue
        if timestamp > proposal_time:
            errors.append("FUTURE_CANDLE_PRESENT")
            continue
        if timestamp <= entry_time:
            continue
        high = _finite(raw.get("high"), positive=True)
        low = _finite(raw.get("low"), positive=True)
        close = _finite(raw.get("close"), positive=True)
        if high is None or low is None or close is None or high < low or not (low <= close <= high):
            errors.append(f"CANDLE_{index}_OHLC_MALFORMED")
            continue
        source = raw.get("source_provenance")
        source_dict = dict(source) if isinstance(source, Mapping) else {}
        status = str(source_dict.get("evidence_status") or raw.get("evidence_status") or "").upper()
        if status in {"STALE", "UNAVAILABLE", "MALFORMED", "INCOMPLETE"} or raw.get("stale") is True:
            errors.append("STALE_OR_UNAVAILABLE_MARKET_EVIDENCE")
        candle = {
            "timestamp": _iso(timestamp),
            "high": high,
            "low": low,
            "close": close,
            "source_provenance": source_dict,
        }
        existing = unique.get(timestamp)
        if existing is not None and any(existing[key] != candle[key] for key in ("high", "low", "close")):
            errors.append("DUPLICATE_CANDLE_CONFLICT")
            continue
        unique[timestamp] = candle
    return [unique[key] for key in sorted(unique)], sorted(set(errors))


def _scope_error(
    position: Mapping[str, Any],
    campaign: Mapping[str, Any],
    candles: Sequence[Mapping[str, Any]],
) -> str | None:
    if str(position.get("campaign_id") or "") != str(campaign.get("campaign_id") or ""):
        return "CROSS_SCOPED_POSITION_CAMPAIGN"
    expected = {
        "campaign_id": str(campaign.get("campaign_id") or ""),
        "release_id": str(campaign.get("release_id") or ""),
        "git_commit": str(campaign.get("git_commit") or ""),
        "config_hash": str(campaign.get("config_hash") or ""),
    }
    for candle in candles:
        provenance = candle.get("source_provenance")
        if not isinstance(provenance, Mapping):
            continue
        for key, expected_value in expected.items():
            observed = provenance.get(key)
            if observed not in (None, "") and str(observed) != expected_value:
                return f"CROSS_SCOPED_MARKET_EVIDENCE:{key}"
    return None


def _execution_evidence(position: Mapping[str, Any], provenance: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    model = provenance.get("execution_cost_model")
    model = dict(model) if isinstance(model, Mapping) else {}
    required = (
        "spread_penalty",
        "slippage_penalty",
        "fee_penalty",
        "funding_penalty",
        "latency_penalty",
        "volatility_penalty",
        "liquidity_penalty",
    )
    normalized: dict[str, float] = {}
    complete = True
    for key in required:
        value = _finite(model.get(key))
        if value is None or value < 0:
            complete = False
        else:
            normalized[key] = value
    entry_costs = {
        key: _finite(position.get(key))
        for key in ("entry_spread", "entry_slippage", "entry_fee")
    }
    if any(value is None or value < 0 for value in entry_costs.values()):
        complete = False
    return {
        "status": "COMPLETE" if complete else "INCOMPLETE",
        "cost_model_unit": provenance.get("execution_cost_model_unit"),
        "execution_cost_unit": provenance.get("execution_cost_unit"),
        "model": normalized,
        "entry_costs": entry_costs,
    }, complete


def _excursions(
    position: Mapping[str, Any],
    candles: Sequence[Mapping[str, Any]],
) -> tuple[float | None, float | None]:
    entry = _finite(position.get("simulated_fill") or position.get("planned_entry"), positive=True)
    stop = _finite(position.get("stop"), positive=True)
    side = str(position.get("side") or "").upper()
    if entry is None or stop is None or side not in {"LONG", "SHORT"}:
        return None, None
    risk = abs(entry - stop)
    if risk <= 0:
        return None, None
    favorable: list[float] = []
    adverse: list[float] = []
    for candle in candles:
        if side == "LONG":
            favorable.append((float(candle["high"]) - entry) / risk)
            adverse.append((entry - float(candle["low"])) / risk)
        else:
            favorable.append((entry - float(candle["low"])) / risk)
            adverse.append((float(candle["high"]) - entry) / risk)
    return max(0.0, max(favorable, default=0.0)), max(0.0, max(adverse, default=0.0))


def _first_terminal_candle_time(
    *,
    side: str,
    current_stop: float | None,
    target: float | None,
    candles: Sequence[Mapping[str, Any]],
) -> str | None:
    """Return the first candle where the authoritative static geometry terminates.

    Shadow decisions happen only after a candle closes. A candle that already
    touched stop/target cannot be used to propose a management action at its
    close, because the canonical position is terminal intra-candle.
    """
    if side not in {"LONG", "SHORT"} or current_stop is None or target is None:
        return None
    for candle in candles:
        high = float(candle["high"])
        low = float(candle["low"])
        if side == "LONG":
            terminal = low <= current_stop or high >= target
        else:
            terminal = high >= current_stop or low <= target
        if terminal:
            return str(candle["timestamp"])
    return None


def _raw_before_boundary(
    candles: Sequence[Mapping[str, Any]] | None,
    terminal_time: str | None,
) -> list[Mapping[str, Any]]:
    if terminal_time is None:
        return list(candles or ())
    boundary = _dt(terminal_time)
    if boundary is None:
        return list(candles or ())
    causal: list[Mapping[str, Any]] = []
    for raw in candles or ():
        if not isinstance(raw, Mapping):
            # Unknown placement cannot be proven post-terminal; retain it so
            # normalization fails closed.
            causal.append(raw)
            continue
        timestamp = _dt(raw.get("timestamp") or raw.get("open_time") or raw.get("time"))
        if timestamp is None or timestamp < boundary:
            causal.append(raw)
    return causal


def _candle_gap_errors(
    candles: Sequence[Mapping[str, Any]],
    *,
    entry_time: datetime | None,
) -> list[str]:
    if entry_time is None or not candles:
        return []
    previous = entry_time
    for index, candle in enumerate(candles):
        current = _dt(candle.get("timestamp"))
        if current is None:
            return ["MARKET_CANDLE_GAP_OR_TIME_INVALID"]
        delta = (current - previous).total_seconds()
        max_gap = 60.0 if index == 0 else 90.0
        if delta <= 0 or delta > max_gap:
            return ["MARKET_CANDLE_GAP_OR_TIME_INVALID"]
        previous = current
    return []


class PaperPositionManagementShadowProvider:
    """Generate deterministic PAPER-only counterfactual proposal evidence.

    The provider does not invent management thresholds.  Until a policy contains
    explicit calibrated parameters, it records a denial reason alongside causal
    market snapshots.  That is intentional evidence for later calibration.
    """

    def __init__(self, *, max_evidence_age_seconds: float) -> None:
        self.max_evidence_age_seconds = max(0.0, float(max_evidence_age_seconds))

    def __call__(
        self,
        position: Mapping[str, Any],
        candles: Sequence[Mapping[str, Any]],
        proposal_time: str,
        campaign: Mapping[str, Any],
    ) -> dict[str, Any]:
        if str(campaign.get("execution_mode") or "PAPER").upper() != "PAPER":
            raise ValueError("POSITION_MANAGEMENT_SHADOW_IS_PAPER_ONLY")

        now = _dt(proposal_time)
        entry_time = _dt(position.get("entry_time"))
        entry = _finite(position.get("simulated_fill") or position.get("planned_entry"), positive=True)
        initial_stop = _finite(position.get("stop"), positive=True)
        current_stop = _finite(
            position.get("current_stop") if position.get("current_stop") is not None else position.get("stop"),
            positive=True,
        )
        target = _finite(
            position.get("current_target") if position.get("current_target") is not None else position.get("target"),
            positive=True,
        )
        remaining_quantity = _finite(
            position.get("remaining_quantity")
            if position.get("remaining_quantity") is not None
            else position.get("quantity"),
            positive=True,
        )
        side = str(position.get("side") or "").upper()

        if now is None or entry_time is None or now <= entry_time:
            normalized_all: list[dict[str, Any]] = []
            errors = ["PROPOSAL_TIME_INVALID"]
        else:
            normalized_all, errors = _normalize_candles(
                candles, entry_time=entry_time, proposal_time=now
            )

        terminal_time = _first_terminal_candle_time(
            side=side,
            current_stop=current_stop,
            target=target,
            candles=normalized_all,
        )
        # Re-normalize only raw observations strictly before the first terminal
        # candle. This prevents terminal-candle and post-terminal OHLC, close,
        # provenance, or malformed/stale flags from influencing a decision that
        # could only have existed before canonical closure.
        if terminal_time is not None and now is not None and entry_time is not None:
            normalized, causal_errors = _normalize_candles(
                _raw_before_boundary(candles, terminal_time),
                entry_time=entry_time,
                proposal_time=now,
            )
            errors = causal_errors
        else:
            normalized = normalized_all

        errors.extend(_candle_gap_errors(normalized, entry_time=entry_time))
        scope_error = _scope_error(position, campaign, normalized)
        if scope_error:
            errors.append(scope_error)

        closed_boundary = normalized[-1]["timestamp"] if normalized else None
        observed_price = float(normalized[-1]["close"]) if normalized else None
        if terminal_time is not None and not normalized:
            errors.append("NO_PRE_TERMINAL_CLOSED_CANDLE")
        elif not normalized:
            errors.append("CLOSED_CANDLE_EVIDENCE_UNAVAILABLE")
        elif now is not None:
            boundary_dt = _dt(closed_boundary)
            # Candle timestamps are open-boundary timestamps. Permit one full 1m
            # candle plus the canonical market-data staleness allowance.
            max_age = self.max_evidence_age_seconds + 60.0
            if boundary_dt is None or (now - boundary_dt).total_seconds() > max_age:
                errors.append("STALE_MARKET_EVIDENCE")

        if None in {entry, initial_stop, current_stop, target, remaining_quantity} or side not in {"LONG", "SHORT"}:
            errors.append("POSITION_GEOMETRY_OR_SIZE_INCOMPLETE")

        provenance = _position_provenance(position)
        target_policy = provenance.get("target_policy")
        target_policy = dict(target_policy) if isinstance(target_policy, Mapping) else {}
        execution_evidence, costs_complete = _execution_evidence(position, provenance)
        if not costs_complete:
            errors.append("EXECUTION_COST_EVIDENCE_INCOMPLETE")

        mfe_r, mae_r = _excursions(position, normalized)
        if mfe_r is None or mae_r is None:
            errors.append("MFE_MAE_UNAVAILABLE")

        errors = sorted(set(errors))
        evidence_block = errors[0] if errors else None
        active_run_id = str(campaign.get("active_run_id") or "")
        position_entry_run_id = str(position.get("burnin_run_id") or "")
        if not active_run_id:
            raise ValueError("POSITION_MANAGEMENT_SHADOW_ACTIVE_RUN_ID_MISSING")

        market_evidence = {
            "status": "COMPLETE" if not errors else "INCOMPLETE",
            "closed_candle_time": closed_boundary,
            "observed_price": observed_price,
            "candle_count": len(normalized),
            "mfe_r_as_of_proposal": mfe_r,
            "mae_r_as_of_proposal": mae_r,
            "first_terminal_candle_time": terminal_time,
            "terminal_and_post_terminal_candles_excluded": terminal_time is not None,
            "errors": errors,
            "latest_candle": normalized[-1] if normalized else None,
        }
        regime_evidence = {
            "regime": position.get("regime"),
            "setup_type": position.get("setup_type"),
            "target_policy": target_policy,
        }

        proposals: list[dict[str, Any]] = []
        boundary_key = closed_boundary or "NO_CLOSED_CANDLE"
        for action in SHADOW_ACTIONS:
            status = "DENIED"
            parameters: dict[str, Any] = {}
            if evidence_block:
                reason = evidence_block
            elif action == "WOULD_ENABLE_TRAILING":
                if not bool(target_policy.get("trailing_allowed")):
                    reason = "AUTHORITATIVE_POLICY_DENIES_TRAILING"
                else:
                    distance = _finite(target_policy.get("trailing_distance"), positive=True)
                    if distance is None:
                        reason = "TRAILING_DISTANCE_UNSET_REQUIRES_CALIBRATION"
                    else:
                        status = "ELIGIBLE"
                        reason = "EXPLICIT_CALIBRATED_TRAILING_POLICY_AVAILABLE"
                        parameters = {"trailing_distance": distance}
            elif action == "WOULD_TIGHTEN_STOP":
                reason = "TIGHTEN_STOP_POLICY_UNCALIBRATED"
            elif action == "WOULD_PARTIAL_EXIT":
                reason = "PARTIAL_EXIT_POLICY_UNCALIBRATED"
            else:
                reason = "PROTECTIVE_EXIT_POLICY_UNCALIBRATED"

            cost_semantics = (
                {
                    "status": "NOT_APPLICABLE_AT_ENABLE_TIME",
                    "immediate_execution_cost_usd": 0.0,
                    "future_exit_costs": "UNRESOLVED",
                }
                if status == "ELIGIBLE" and action == "WOULD_ENABLE_TRAILING"
                else {
                    "status": "NOT_COMPUTED",
                    "reason": "PROPOSAL_NOT_ELIGIBLE",
                }
            )
            counterfactual = (
                {
                    "status": "PATH_DEPENDENT",
                    "net_r": None,
                    "realized_evidence": False,
                    "reason": "NO_REALIZED_EXIT_AT_ENABLE_TIME",
                }
                if status == "ELIGIBLE"
                else {
                    "status": "NOT_COMPUTED",
                    "net_r": None,
                    "realized_evidence": False,
                    "reason": "PROPOSAL_NOT_ELIGIBLE",
                }
            )
            evidence_signature = canonical_hash({
                "closed_candle_time": boundary_key,
                "errors": errors,
                "observed_price": observed_price,
                "mfe_r": mfe_r,
                "mae_r": mae_r,
                "target_policy": target_policy,
                "execution_status": execution_evidence["status"],
                "eligibility_status": status,
                "reason": reason,
                "parameters": parameters,
            })
            identity = {
                "campaign_id": campaign.get("campaign_id"),
                "burnin_run_id": active_run_id,
                "release_id": campaign.get("release_id"),
                "git_commit": campaign.get("git_commit"),
                "config_hash": campaign.get("config_hash"),
                "trade_id": position.get("trade_id"),
                "closed_candle_time": boundary_key,
                "proposed_action": action,
                "evidence_signature": evidence_signature,
            }
            proposal_id = "pmshadow_" + canonical_hash(identity)[:24]
            payload = {
                **identity,
                "proposal_id": proposal_id,
                "strategy_config_hash": campaign.get("strategy_config_hash"),
                "position_entry_run_id": position_entry_run_id,
                "proposal_time": closed_boundary or position.get("decision_time") or position.get("entry_time"),
                "decision_time": position.get("decision_time") or position.get("entry_time"),
                "symbol": position.get("symbol"),
                "side": side,
                "entry": entry,
                "initial_stop": initial_stop,
                "current_stop": current_stop,
                "target": target,
                "remaining_quantity": remaining_quantity,
                "observed_price": observed_price,
                "mfe_r": mfe_r,
                "mae_r": mae_r,
                "proposed_action": action,
                "parameters": parameters,
                "eligibility_status": status,
                "reason": reason,
                "market_evidence": market_evidence,
                "regime_evidence": regime_evidence,
                "execution_evidence": execution_evidence,
                "estimated_execution_costs": cost_semantics,
                "counterfactual_net_r": counterfactual,
                "authoritative_state_mutation": False,
                "authoritative_realized_pnl": False,
            }
            proposal_hash = canonical_hash(payload)
            proposals.append({**payload, "proposal_hash": proposal_hash})
        return {"proposals": proposals}


def persist_position_management_shadow_proposals(
    conn: Any,
    proposals: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    """Persist counterfactual rows idempotently without touching canonical state."""
    bootstrap_position_management_shadow_schema(conn)
    counts = {"inserted": 0, "idempotent": 0}
    for raw in proposals:
        proposal = dict(raw)
        action = str(proposal.get("proposed_action") or "")
        if action not in SHADOW_ACTIONS:
            raise ValueError("POSITION_MANAGEMENT_SHADOW_ACTION_INVALID")
        if str(proposal.get("eligibility_status") or "") not in {"ELIGIBLE", "DENIED"}:
            raise ValueError("POSITION_MANAGEMENT_SHADOW_STATUS_INVALID")
        proposal_id = str(proposal.get("proposal_id") or "")
        proposal_hash = str(proposal.get("proposal_hash") or "")
        if not proposal_id or not proposal_hash:
            raise ValueError("POSITION_MANAGEMENT_SHADOW_IDENTITY_MISSING")
        existing = _exec(
            conn,
            "SELECT proposal_hash FROM burnin_position_management_shadow_proposals "
            "WHERE proposal_id=:proposal_id",
            {"proposal_id": proposal_id},
        ).fetchone()
        if existing is not None:
            if str(existing[0]) != proposal_hash:
                raise ValueError("POSITION_MANAGEMENT_SHADOW_PROPOSAL_ID_CONFLICT")
            counts["idempotent"] += 1
            continue
        _exec(
            conn,
            """INSERT INTO burnin_position_management_shadow_proposals(
                proposal_id,proposal_hash,campaign_id,burnin_run_id,position_entry_run_id,
                release_id,git_commit,config_hash,strategy_config_hash,trade_id,symbol,
                proposal_time,closed_candle_time,proposed_action,eligibility_status,reason,
                observed_price,mfe_r,mae_r,payload_json,created_at,schema_version
            ) VALUES (
                :proposal_id,:proposal_hash,:campaign_id,:burnin_run_id,:position_entry_run_id,
                :release_id,:git_commit,:config_hash,:strategy_config_hash,:trade_id,:symbol,
                :proposal_time,:closed_candle_time,:proposed_action,:eligibility_status,:reason,
                :observed_price,:mfe_r,:mae_r,:payload_json,:created_at,:schema_version
            )""",
            {
                "proposal_id": proposal_id,
                "proposal_hash": proposal_hash,
                "campaign_id": proposal.get("campaign_id"),
                "burnin_run_id": proposal.get("burnin_run_id"),
                "position_entry_run_id": proposal.get("position_entry_run_id"),
                "release_id": proposal.get("release_id"),
                "git_commit": proposal.get("git_commit"),
                "config_hash": proposal.get("config_hash"),
                "strategy_config_hash": proposal.get("strategy_config_hash"),
                "trade_id": proposal.get("trade_id"),
                "symbol": proposal.get("symbol"),
                "proposal_time": proposal.get("proposal_time"),
                "closed_candle_time": proposal.get("closed_candle_time"),
                "proposed_action": action,
                "eligibility_status": proposal.get("eligibility_status"),
                "reason": proposal.get("reason"),
                "observed_price": proposal.get("observed_price"),
                "mfe_r": proposal.get("mfe_r"),
                "mae_r": proposal.get("mae_r"),
                "payload_json": json.dumps(proposal, sort_keys=True, default=str),
                "created_at": utc_now(),
                "schema_version": SHADOW_SCHEMA_VERSION,
            },
        )
        counts["inserted"] += 1
    return counts
