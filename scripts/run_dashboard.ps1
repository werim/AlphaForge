[CmdletBinding()]
param(
    [string]$HostAddress = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$Port = 8000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$LogDir = Join-Path $RepoDir "logs"
$LogPath = Join-Path $LogDir "dashboard.windows.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Location $RepoDir

"[$(Get-Date -Format o)] Starting AlphaForge dashboard on http://${HostAddress}:$Port" | Out-File -FilePath $LogPath -Encoding utf8 -Append

python -m uvicorn alphaforge.dashboard.app:create_app --factory --host $HostAddress --port $Port
exit $LASTEXITCODE
