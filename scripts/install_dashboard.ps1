[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,

    [switch]$NoScheduledTask,

    [switch]$DoNotStart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$TaskName = "AlphaForge Dashboard"
$RepoDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvDir = Join-Path $RepoDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$Runner = Join-Path $RepoDir "scripts\run_dashboard.ps1"
$LogDir = Join-Path $RepoDir "logs"

function Resolve-PythonCommand {
    $PyLauncher = Get-Command "py" -ErrorAction SilentlyContinue
    if ($null -ne $PyLauncher) {
        return @($PyLauncher.Source, "-3")
    }

    $Python = Get-Command "python" -ErrorAction SilentlyContinue
    if ($null -ne $Python) {
        return @($Python.Source)
    }

    throw "Python 3 was not found. Install Python 3.11 or later, then rerun this installer."
}

if (-not (Test-Path $Runner)) {
    throw "Dashboard runner script is missing: $Runner"
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if (-not (Test-Path $VenvPython)) {
    $PythonCommand = Resolve-PythonCommand
    $PythonExecutable = $PythonCommand[0]
    $PythonArguments = @()
    if ($PythonCommand.Length -gt 1) {
        $PythonArguments += $PythonCommand[1..($PythonCommand.Length - 1)]
    }
    $PythonArguments += @("-m", "venv", $VenvDir)
    & $PythonExecutable @PythonArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python virtual environment creation failed."
    }
}

& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "pip upgrade failed."
}

$EditableTarget = "$RepoDir[dev]"
& $VenvPython -m pip install -e $EditableTarget
if ($LASTEXITCODE -ne 0) {
    throw "AlphaForge dashboard installation failed."
}

if (-not $NoScheduledTask) {
    $PowerShellPath = (Get-Command "powershell.exe").Source
    $ActionArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`" -Port $Port"
    $Action = New-ScheduledTaskAction -Execute $PowerShellPath -Argument $ActionArgs -WorkingDirectory $RepoDir
    $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $Settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Days 0) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "Read-only AlphaForge dashboard on localhost. Does not start trading runtime." -Force | Out-Null
}

if (-not $DoNotStart) {
    if (-not $NoScheduledTask) {
        Start-ScheduledTask -TaskName $TaskName
    } else {
        Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $Runner, "-Port", "$Port") -WorkingDirectory $RepoDir -WindowStyle Hidden
    }
}

Write-Host "AlphaForge Dashboard installed for Windows."
Write-Host "Local URL: http://127.0.0.1:$Port"
if (-not $NoScheduledTask) {
    Write-Host "Auto-start task: $TaskName (current user logon)"
}
Write-Host "Logs: $LogDir\dashboard.windows.log"
Write-Host "Runtime remains separate and was not started by this installer."
