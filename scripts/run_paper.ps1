param(
    [switch]$SafeScanner
)

$ErrorActionPreference = "Stop"
$env:ALPHAFORGE_MODE = "PAPER"

if ($SafeScanner) {
    $env:ALPHAFORGE_RUNTIME_SAFE_SCANNER = "1"
}

python -m alphaforge.runtime
