[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Uvicorn = Join-Path $RepoDir ".venv\Scripts\uvicorn.exe"
$LogDir = Join-Path $RepoDir "logs"
$LogPath = Join-Path $LogDir "dashboard.windows.log"

if (-not (Test-Path $Uvicorn)) {
    throw "Dashboard virtual environment is not installed. Run scripts\install_dashboard_windows.ps1 first."
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Location $RepoDir

"[$(Get-Date -Format o)] Starting AlphaForge dashboard on http://127.0.0.1:$Port" | Out-File -FilePath $LogPath -Encoding utf8 -Append

& $Uvicorn "alphaforge.dashboard.app:create_app" "--factory" "--host" "127.0.0.1" "--port" "$Port" >> $LogPath 2>&1
exit $LASTEXITCODE
