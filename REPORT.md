# AlphaForge Surgery Report — BACKTEST Real Filter Switch Truthfulness

## Why this patch was needed

`.env.example` needed to document only filter variables that are loaded by configuration and can alter real BACKTEST reject decisions before final accept/reject. Active cosmetic variables would create false confidence and violate execution-aware research integrity.

## Root cause

The repository had real filter logic split across the symbol selector and order quality gates, but no explicit BACKTEST-only switch layer proving each documented experimental variable was a real decision changer. Dashboard control also needed to pass switches into the backtest command rather than only changing presentation.

## Files changed

- `.env.example`
- `src/alphaforge/config/__init__.py`
- `src/alphaforge/config.py`
- `src/alphaforge/order.py`
- `src/alphaforge/symbol_selector.py`
- `backtest_order.py`
- `src/alphaforge/dashboard/backtest_control.py`
- `src/alphaforge/dashboard/app.py`
- `src/alphaforge/dashboard/templates/overview.html`
- `tests/test_backtest_filter_switches.py`
- `CHANGELOG.md`
- `VERSION.md`

## Runtime behavior changes

- Added BACKTEST-only real filter switches for:
  - LOW_SCORE
  - TOO_CHOPPY
  - WEAK_TREND_AND_NO_RANGE_EDGE
  - STOP_TOO_WIDE
  - RR_TOO_LOW
  - DAILY_SYMBOL_TRADE_LIMIT
  - REGIME_MISMATCH
  - PANIC_CONDITIONS
- Defaults are `true`, preserving conservative baseline behavior.
- When a switch is false in BACKTEST, the specific gate is bypassed at the producer branch and the candidate proceeds to later gates.
- PAPER and LIVE ignore these BACKTEST switches; passing disabled filters outside BACKTEST does not loosen PAPER/LIVE quality decisions.

## Lifecycle changes

- Accepted lifecycle rows can carry disabled-filter evidence when a BACKTEST candidate reaches execution after a bypass.
- Rejected lifecycle persistence includes execution context evidence for disabled filters where available.

## Persistence changes

- `order_backtest_summary.csv` now includes:
  - `disabled_filters`
  - `filter_switch_experiment_active`
  - `disabled_filter_bypass_count`
- `lifecycle_calibration_summary.json` now includes disabled-filter metadata and bypass counts.
- Rejected and lifecycle diagnostics preserve bypassed reasons rather than erasing original would-have-rejected evidence.

## Export/schema changes

- CSV exports gain additive metadata columns only. Existing columns are not removed.
- No migration is required for SQLite runtime tables; metadata is carried in execution context/payload-style fields and CSV artifacts.

## Env filter audit table

| env variable | documented purpose | default value | loaded by file/function | used by decision file/function | affects BACKTEST decisions? | affects PAPER decisions? | affects LIVE decisions? | status |
|---|---|---:|---|---|---|---|---|---|
| ALPHAFORGE_MIN_SIGNAL_SCORE | Canonical runtime AI accept threshold | 0.62 | `src/alphaforge/config/__init__.py::load_config_from_env` | runtime AI paths, not this BACKTEST scanner switch layer | no | yes | yes | REAL |
| ALPHAFORGE_MIN_ACCEPT_SCORE | Legacy alias for runtime signal threshold | 0.62 | `src/alphaforge/config/__init__.py::load_config_from_env` | runtime AI paths via canonical setting | no | yes | yes | REAL |
| ALPHAFORGE_MIN_TRADE_SCORE | Strategy score threshold legacy documentation | 0.50 | not loaded by current config object | no current decision branch found | no | no | no | TODO_COMMENT_ONLY risk remains outside BACKTEST switch set |
| ALPHAFORGE_MIN_RR | Minimum raw RR before execution penalties | 1.20 | order quality config defaults and runtime env conventions | `src/alphaforge/order.py::evaluate_trade_quality` when provided by runtime config | yes | yes | yes | REAL |
| MIN_EFFECTIVE_RR | Minimum execution-adjusted RR | 1.10 | execution/backtest conventions | `backtest_order.py::_execution_reject_flags` uses effective RR guard | yes | no | no | REAL |
| MAX_SPREAD_BPS | Max spread threshold in bps | 25 | legacy/env convention | no direct current BACKTEST decision branch found | no | no | no | TODO_COMMENT_ONLY risk remains legacy |
| MAX_SLIPPAGE_BPS | Max slippage threshold in bps | 20 | legacy/env convention | no direct current BACKTEST decision branch found | no | no | no | TODO_COMMENT_ONLY risk remains legacy |
| ALPHAFORGE_MAX_SPREAD_PCT | Runtime spread limit percent | 0.0025 | `src/alphaforge/config/__init__.py::load_config_from_env` | runtime/symbol/order spread gates | yes | yes | yes | REAL |
| ALPHAFORGE_MAX_EXPECTED_SLIPPAGE_PCT | Runtime slippage limit percent | 0.0020 | env/config conventions | order/execution quality branches when provided | yes | yes | yes | REAL |
| ALPHAFORGE_MAX_ABS_FUNDING_RATE_PCT | Funding-risk guard | 0.0010 | `src/alphaforge/config/__init__.py::load_config_from_env` | symbol selector funding anomaly gate via config | yes | no | no | REAL |
| MIN_LIQUIDITY_USD | Minimum 24h quote liquidity | 5000000 | symbol selector config conventions | `src/alphaforge/symbol_selector.py::select_symbol` volume/liquidity gates when passed | yes | no | no | REAL |
| ENABLE_ORDERBOOK_FILTER | Orderbook quality gating | true | legacy/env convention | no direct current BACKTEST decision branch found | no | no | no | TODO_COMMENT_ONLY risk remains legacy |
| ENABLE_SPOOF_DETECTION | Spoof-risk detection | false | symbol selector config conventions | `src/alphaforge/symbol_selector.py::select_symbol` if spoof data/config enabled | yes | no | no | REAL |
| ENABLE_ABSORPTION_FILTER | Absorption/flow filter | false | legacy/env convention | no direct current BACKTEST decision branch found | no | no | no | TODO_COMMENT_ONLY risk remains legacy |
| ENABLE_REGIME_FILTER | Regime-aware gating | true | order/symbol config conventions | `src/alphaforge/order.py::evaluate_trade_quality` regime gate | yes | yes | yes | REAL |
| ALPHAFORGE_BACKTEST_FILTER_LOW_SCORE_ENABLED | BACKTEST-only LOW_SCORE rejection gate switch | true | `src/alphaforge/config/__init__.py::load_config_from_env` | `src/alphaforge/order.py::evaluate_trade_quality` | yes | no | no | BACKTEST_ONLY_REAL |
| ALPHAFORGE_BACKTEST_FILTER_TOO_CHOPPY_ENABLED | BACKTEST-only TOO_CHOPPY symbol gate switch | true | `src/alphaforge/config/__init__.py::load_config_from_env` | `src/alphaforge/symbol_selector.py::select_symbol` | yes | no | no | BACKTEST_ONLY_REAL |
| ALPHAFORGE_BACKTEST_FILTER_WEAK_TREND_NO_RANGE_ENABLED | BACKTEST-only weak trend/no range edge symbol gate switch | true | `src/alphaforge/config/__init__.py::load_config_from_env` | `src/alphaforge/symbol_selector.py::select_symbol` | yes | no | no | BACKTEST_ONLY_REAL |
| ALPHAFORGE_BACKTEST_FILTER_STOP_TOO_WIDE_ENABLED | BACKTEST-only STOP_TOO_WIDE order gate switch | true | `src/alphaforge/config/__init__.py::load_config_from_env` | `src/alphaforge/order.py::evaluate_trade_quality` | yes | no | no | BACKTEST_ONLY_REAL |
| ALPHAFORGE_BACKTEST_FILTER_RR_TOO_LOW_ENABLED | BACKTEST-only RR_TOO_LOW order gate switch | true | `src/alphaforge/config/__init__.py::load_config_from_env` | `src/alphaforge/order.py::evaluate_trade_quality` | yes | no | no | BACKTEST_ONLY_REAL |
| ALPHAFORGE_BACKTEST_FILTER_DAILY_SYMBOL_TRADE_LIMIT_ENABLED | BACKTEST-only daily symbol limit switch | true | `src/alphaforge/config/__init__.py::load_config_from_env` | `src/alphaforge/order.py::evaluate_trade_quality` | yes | no | no | BACKTEST_ONLY_REAL |
| ALPHAFORGE_BACKTEST_FILTER_REGIME_MISMATCH_ENABLED | BACKTEST-only regime mismatch order gate switch | true | `src/alphaforge/config/__init__.py::load_config_from_env` | `src/alphaforge/order.py::evaluate_trade_quality` | yes | no | no | BACKTEST_ONLY_REAL |
| ALPHAFORGE_BACKTEST_FILTER_PANIC_CONDITIONS_ENABLED | BACKTEST-only panic condition symbol gate switch | true | `src/alphaforge/config/__init__.py::load_config_from_env` | `src/alphaforge/symbol_selector.py::select_symbol` | yes | no | no | BACKTEST_ONLY_REAL |

## Real decision-changing filters

- The eight `ALPHAFORGE_BACKTEST_FILTER_*` variables above are real BACKTEST decision changers.
- They are loaded into `BacktestFilterSwitches`, passed into the scanner/order path, and checked at the actual reject source.

## TODO/comment-only filters

- No additional active `ALPHAFORGE_BACKTEST_FILTER_*` variables were added.
- Legacy non-`ALPHAFORGE_BACKTEST_FILTER_*` variables with unclear or indirect wiring remain documented as legacy risks in the audit table; they were not expanded into new active experimental switches.

## Tests added

- Added `tests/test_backtest_filter_switches.py` to verify:
  - `.env.example` active BACKTEST filter variables map to config fields.
  - LOW_SCORE bypass changes BACKTEST decision behavior and does not loosen PAPER.
  - RR_TOO_LOW, REGIME_MISMATCH, STOP_TOO_WIDE, and DAILY_SYMBOL_TRADE_LIMIT bypass at the order quality source.
  - TOO_CHOPPY, WEAK_TREND_AND_NO_RANGE_EDGE, and PANIC_CONDITIONS bypass at the symbol selector source.

## Tests executed

- `python -m py_compile backtest_order.py`
- `python -m py_compile src/alphaforge/dashboard/app.py`
- `python -m py_compile src/alphaforge/dashboard/backtest_control.py`
- `pytest tests/test_backtest_filter_switches.py -q`
- `pytest tests/test_backtest_order_scanner.py -q`
- `pytest tests/test_dashboard_app.py -q` skipped because dashboard optional dependencies are unavailable in this environment.

## Risks

- Disabling filters is experimental and can increase accepted trades while worsening expectancy.
- A bypassed gate is not LIVE-readiness evidence.
- Legacy env variables outside the new BACKTEST switch set may still need a future cleanup pass if the project wants every historical environment key to be strictly audited.

## Remaining limitations

- This patch intentionally does not loosen defaults.
- This patch does not add rescue/quality gates by default.
- This patch does not make LIVE trading ready.

## Migration concerns

- No database migration is required.
- CSV consumers should tolerate additive columns in BACKTEST artifacts.

## Push recommendation

- Safe to push after the full suite is reviewed in an environment with optional dashboard dependencies installed.
