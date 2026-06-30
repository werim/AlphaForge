param(
    [string]$Interval = "1h",
    [int]$Days = 30,
    [string]$Symbols = "BTCUSDT,ETHUSDT",
    [string]$OutputDir = "data/backtests/manual",
    [switch]$ForceRefresh,
    [switch]$ShortBreakdownRescue
)

$ErrorActionPreference = "Stop"

$argsList = @(
    "backtest_order.py",
    "--interval", $Interval,
    "--last-n-days", "$Days",
    "--symbols", $Symbols,
    "--output-dir", $OutputDir
)

if ($ForceRefresh) {
    $argsList += "--force-refresh"
}

if ($ShortBreakdownRescue) {
    $env:ALPHAFORGE_BACKTEST_SHORT_BREAKDOWN_RESCUE_ENABLED = "true"
}

python @argsList
