from __future__ import annotations

import asyncio
import json
import time

import pytest
from sqlalchemy import text

from alphaforge.config import load_config_from_env
from alphaforge.config_registry import REGISTRY_BY_ENV
from alphaforge.persistence import init_db
from alphaforge.runtime import ExecutionMode, RuntimeConfig, RuntimeOrchestrator
from alphaforge.symbol_selector import (
    EvidenceState,
    MarketCapEvidence,
    UniverseConstraints,
    bind_market_cap_evidence,
    build_selected_universe,
)
from alphaforge.universe_evidence import persist_universe_selection


DECISION_TS = 1_800_000_000.0


def candidate(symbol: str, **overrides):
    row = {
        "symbol": symbol,
        "source_exchange": "fixture",
        "market_ts": DECISION_TS,
        "volume_24h_usdt": 100_000_000.0,
        "spread_pct": 0.001,
        "expected_slippage_pct": 0.0005,
        "volatility_pct": 0.03,
        "trend_strength": 0.7,
        "chop_score": 0.2,
    }
    row.update(overrides)
    return row


def select(rows, constraints=None, mode="PAPER"):
    return build_selected_universe(
        rows,
        constraints or UniverseConstraints(),
        decision_timestamp=DECISION_TS,
        execution_mode=mode,
        git_sha="abc123",
        strategy_config_hash="strategy-hash",
    )


def by_symbol(selection, symbol):
    return next(row for row in selection.candidates if row.symbol == symbol)


def test_constraints_validation_and_hash_are_deterministic():
    left = UniverseConstraints(excluded_symbols=("ETHUSDT", "BTCUSDT"))
    right = UniverseConstraints(excluded_symbols=("BTCUSDT", "ETHUSDT", "BTCUSDT"))
    assert left.config_hash == right.config_hash
    assert left.excluded_symbols == ("BTCUSDT", "ETHUSDT")
    with pytest.raises(ValueError, match="non-negative"):
        UniverseConstraints(min_market_cap_usd=-1)
    with pytest.raises(ValueError, match="positive integer"):
        UniverseConstraints(candidate_pool_top_n_volume=0)
    with pytest.raises(ValueError, match="must not exceed"):
        UniverseConstraints(candidate_pool_top_n_volume=2, max_active_symbols=3)
    with pytest.raises(ValueError, match="Malformed"):
        UniverseConstraints(excluded_symbols=("ETH/USDT",))
    assert REGISTRY_BY_ENV["ALPHAFORGE_UNIVERSE_MAX_ACTIVE_SYMBOLS"].deprecated_aliases == (
        "ALPHAFORGE_MAX_SYMBOLS_PER_SCAN",
    )


def test_canonical_config_loads_constraints_and_rejects_cross_field_invalidity():
    config = load_config_from_env(env={
        "ALPHAFORGE_UNIVERSE_MIN_MARKET_CAP_USD": "100",
        "ALPHAFORGE_UNIVERSE_MAX_MARKET_CAP_USD": "1000",
        "ALPHAFORGE_UNIVERSE_MIN_VOLUME_24H_USD": "200",
        "ALPHAFORGE_UNIVERSE_CANDIDATE_POOL_TOP_N_VOLUME": "10",
        "ALPHAFORGE_UNIVERSE_MAX_ACTIVE_SYMBOLS": "3",
        "ALPHAFORGE_UNIVERSE_EXCLUDED_SYMBOLS": "ETHUSDT,BTCUSDT",
    })
    assert config.runtime.universe_min_market_cap_usd == 100
    assert config.runtime.universe_max_market_cap_usd == 1000
    assert config.runtime.universe_min_volume_24h_usd == 200
    assert config.runtime.universe_candidate_pool_top_n_volume == 10
    assert config.runtime.max_symbols_per_scan == 3
    with pytest.raises(ValueError, match="must not exceed"):
        load_config_from_env(env={
            "ALPHAFORGE_UNIVERSE_CANDIDATE_POOL_TOP_N_VOLUME": "2",
            "ALPHAFORGE_UNIVERSE_MAX_ACTIVE_SYMBOLS": "3",
        })


def test_market_cap_volume_top_n_exclusions_and_stable_ties():
    constraints = UniverseConstraints(
        min_market_cap_usd=1_000_000,
        max_market_cap_usd=10_000_000,
        min_volume_24h_usd=10_000,
        candidate_pool_top_n_volume=2,
        max_active_symbols=2,
        excluded_symbols=("EXCLUDEDUSDT",),
    )
    common_cap = {
        "market_cap_usd": 5_000_000,
        "market_cap_observed_at": DECISION_TS,
        "market_cap_source": "FIXTURE_SNAPSHOT",
    }
    selection = select([
        candidate("ZZZUSDT", volume_24h_usdt=20_000, **common_cap),
        candidate("AAAUSDT", volume_24h_usdt=20_000, **common_cap),
        candidate("THIRDUSDT", volume_24h_usdt=19_000, **common_cap),
        candidate("LOWCAPUSDT", volume_24h_usdt=99_000, market_cap_usd=100,
                  market_cap_observed_at=DECISION_TS, market_cap_source="FIXTURE_SNAPSHOT"),
        candidate("EXCLUDEDUSDT", volume_24h_usdt=100_000, **common_cap),
    ], constraints)
    assert selection.selected_symbols == ("AAAUSDT", "ZZZUSDT")
    assert by_symbol(selection, "THIRDUSDT").reasons == ("OUTSIDE_TOP_N_VOLUME",)
    assert "BELOW_MIN_MARKET_CAP" in by_symbol(selection, "LOWCAPUSDT").reasons
    assert by_symbol(selection, "EXCLUDEDUSDT").reasons == ("EXPLICITLY_EXCLUDED",)


def test_instrument_identity_and_funding_abnormality_are_canonical_eligibility_gates():
    selection = select([
        candidate("DATEDUSDT", contract_type="CURRENT_QUARTER"),
        candidate("WRONGBUSD", quote_asset="BUSD"),
        candidate("FUNDUSDT", funding_rate_pct=0.01, funding_status="MEASURED"),
    ])
    assert "INELIGIBLE_INSTRUMENT_TYPE" in by_symbol(selection, "DATEDUSDT").reasons
    assert "QUOTE_ASSET_MISMATCH" in by_symbol(selection, "WRONGBUSD").reasons
    assert "FUNDING_ANOMALY" in by_symbol(selection, "FUNDUSDT").reasons
    assert selection.selected_symbols == ()


def test_market_cap_constraint_fails_closed_without_snapshot_provenance():
    selection = select(
        [candidate("BTCUSDT", market_cap_usd=1_000_000)],
        UniverseConstraints(min_market_cap_usd=100, max_active_symbols=1),
    )
    decision = by_symbol(selection, "BTCUSDT")
    assert decision.state is EvidenceState.UNAVAILABLE
    assert "MARKET_CAP_UNAVAILABLE" in decision.reasons
    assert selection.selected_symbols == ()


def test_market_cap_provider_boundary_preserves_unavailable_instead_of_zero():
    bound = bind_market_cap_evidence(
        [candidate("BTCUSDT"), candidate("ETHUSDT")],
        {"BTCUSDT": MarketCapEvidence(1_000_000, DECISION_TS, "FIXTURE", "MEASURED")},
    )
    assert bound[0]["market_cap_usd"] == 1_000_000
    assert bound[1]["market_cap_usd"] is None
    assert bound[1]["market_cap_status"] == "UNAVAILABLE"


@pytest.mark.parametrize(
    ("overrides", "state", "reason"),
    [
        ({"volume_24h_usdt": None}, EvidenceState.UNAVAILABLE, "VOLUME_24H_USD_UNAVAILABLE"),
        ({"spread_pct": float("nan")}, EvidenceState.INVALID, "SPREAD_PCT_INVALID"),
        ({"volatility_pct": float("inf")}, EvidenceState.INVALID, "VOLATILITY_PCT_INVALID"),
        ({"market_ts": DECISION_TS - 121}, EvidenceState.STALE, "MARKET_EVIDENCE_STALE"),
    ],
)
def test_required_missing_stale_and_nonfinite_evidence_fails_closed(overrides, state, reason):
    selection = select([candidate("BTCUSDT", **overrides)])
    decision = by_symbol(selection, "BTCUSDT")
    assert decision.state is state
    assert reason in decision.reasons
    assert not decision.selected


def test_liquidity_and_execution_quality_defeat_raw_volatility():
    constraints = UniverseConstraints(
        min_volume_24h_usd=1_000_000,
        candidate_pool_top_n_volume=3,
        max_active_symbols=1,
        max_spread_pct=0.01,
        max_expected_slippage_pct=0.01,
    )
    selection = select([
        candidate("LIQUIDUSDT", volume_24h_usdt=500_000_000, volatility_pct=0.04,
                  spread_pct=0.0002, expected_slippage_pct=0.0002),
        candidate("WILDUSDT", volume_24h_usdt=2_000_000, volatility_pct=5.0,
                  spread_pct=0.009, expected_slippage_pct=0.009),
        candidate("UNSAFEUSDT", volume_24h_usdt=900_000_000, volatility_pct=50.0,
                  spread_pct=0.02, expected_slippage_pct=0.02),
    ], constraints)
    assert selection.selected_symbols == ("LIQUIDUSDT",)
    assert "UNSAFE_SPREAD" in by_symbol(selection, "UNSAFEUSDT").reasons
    assert "UNSAFE_EXPECTED_SLIPPAGE" in by_symbol(selection, "UNSAFEUSDT").reasons
    assert by_symbol(selection, "WILDUSDT").score < by_symbol(selection, "LIQUIDUSDT").score


def test_optional_unavailable_slippage_is_an_explicit_penalty_not_zero_good():
    complete = select([candidate("BTCUSDT")])
    unavailable = select([candidate("BTCUSDT", expected_slippage_pct=None)])
    complete_row = by_symbol(complete, "BTCUSDT")
    unavailable_row = by_symbol(unavailable, "BTCUSDT")
    assert unavailable_row.evidence_availability["expected_slippage_pct"] == "UNAVAILABLE_OPTIONAL"
    assert unavailable_row.ranking_components["evidence_uncertainty_penalty"] > 0
    assert unavailable_row.score < complete_row.score


def test_provider_order_replay_and_cross_mode_protected_semantics_are_identical():
    rows = [candidate("ETHUSDT"), candidate("BTCUSDT")]
    outputs = [select(rows, mode=mode) for mode in ("BACKTEST", "PAPER", "LIVE_PRECHECK")]
    reversed_output = select(list(reversed(rows)), mode="PAPER")
    assert {output.selected_symbols for output in outputs} == {outputs[0].selected_symbols}
    assert {output.evidence_hash for output in outputs} == {outputs[0].evidence_hash}
    assert reversed_output.selected_symbols == outputs[0].selected_symbols
    assert reversed_output.evidence_hash == outputs[0].evidence_hash
    assert outputs[0].selected_symbols == ("BTCUSDT", "ETHUSDT")


def test_selection_evidence_is_complete_immutable_and_retry_idempotent(tmp_path):
    engine = init_db(f"sqlite+pysqlite:///{tmp_path / 'universe.db'}")
    selection = select([
        candidate("BTCUSDT"),
        candidate("ETHUSDT", spread_pct=0.5),
    ], UniverseConstraints(max_active_symbols=1))
    with engine.begin() as conn:
        assert persist_universe_selection(conn, selection)
        assert persist_universe_selection(conn, selection)
    with engine.connect() as conn:
        cycle = conn.execute(text("SELECT * FROM universe_selection_cycles")).mappings().one()
        rows = conn.execute(text(
            "SELECT symbol, eligibility_state, exclusion_reasons_json, ranking_components_json, selected "
            "FROM universe_selection_candidates ORDER BY candidate_index"
        )).mappings().all()
        assert cycle["git_sha"] == "abc123"
        assert cycle["config_hash"] == selection.config_hash
        assert cycle["strategy_config_hash"] == "strategy-hash"
        assert cycle["evidence_hash"] == selection.evidence_hash
        assert json.loads(cycle["selected_symbols_json"]) == ["BTCUSDT"]
        assert {row["symbol"] for row in rows} == {"BTCUSDT", "ETHUSDT"}
        rejected = next(row for row in rows if row["symbol"] == "ETHUSDT")
        assert rejected["eligibility_state"] == "INELIGIBLE"
        assert "UNSAFE_SPREAD" in json.loads(rejected["exclusion_reasons_json"])
        with pytest.raises(Exception, match="immutable"):
            conn.execute(text("UPDATE universe_selection_cycles SET git_sha='different'"))


def test_paper_runtime_consumes_ranked_snapshot_not_a_hardcoded_symbol_list(monkeypatch):
    processed = []
    runtime_ts = time.time()
    rows = [
        candidate("SOLUSDT", market_ts=runtime_ts, volume_24h_usdt=20_000_000, spread_pct=0.003),
        candidate("XRPUSDT", market_ts=runtime_ts, volume_24h_usdt=500_000_000, spread_pct=0.0001),
    ]

    async def process(_self, result):
        processed.append(result.symbol)

    monkeypatch.setattr(RuntimeOrchestrator, "_process_symbol", process)
    runtime = RuntimeOrchestrator(
        config=RuntimeConfig(
            execution_mode=ExecutionMode.PAPER,
            max_symbols_per_scan=1,
            universe_min_volume_24h_usd=1,
        ),
        ai_brain=None,
        market_scanner=lambda: asyncio.sleep(0, result=rows),
    )
    asyncio.run(runtime._scan_once())
    assert processed == ["XRPUSDT"]
