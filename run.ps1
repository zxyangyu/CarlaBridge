<#
.SYNOPSIS
    Launch CarlaBridge using the bundled conda env at D:/carla/env.

.DESCRIPTION
    Uses the absolute python at D:/carla/env/python.exe so no `conda activate`
    is required. PYTHONPATH is set to the repo root so `carlabridge` resolves
    + sys.path side-effect adds CARLA's bundled agents (see carlabridge/__init__.py).

    Note: PowerShell parameter style — use SINGLE dash (`-Scenario`, NOT
    `--scenario`). The script translates to the python `--`-style CLI
    internally.

.PARAMETER Scenario
    Override scenario.default (default: from config/default.toml, currently `s1_fire`).

.PARAMETER Config
    Extra TOML overlay path. Merges on top of default + local.toml.

.PARAMETER LogLevel
    DEBUG / INFO / WARN / ERROR (default: from config).

.PARAMETER NoCarla
    Skip CARLA connection. HTTP + Socket.IO start but no tick / no snapshot.
    Useful for frontend smoke tests without a CARLA server.

.EXAMPLE
    .\run.ps1
    .\run.ps1 -Scenario s1_fire -LogLevel DEBUG
    .\run.ps1 -NoCarla
#>

[CmdletBinding()]
param(
    [string]$Scenario,
    [string]$Config,
    [string]$LogLevel,
    [switch]$NoCarla
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = "./.venv/python.exe"
$PythonExe = "D:\carla\env\python.exe"
if (-not $env:CARLA_AGENTS_ROOT) {
    $env:CARLA_AGENTS_ROOT = "D:\carla\PythonAPI\carla"
}

if (-not (Test-Path $PythonExe)) {
    Write-Error @"
conda env python not found at $PythonExe
The bridge is pinned to this exact path (CARLA's wheel only installs into
a Py 3.12 env and we've registered all deps there). If you've moved your
env, update the $PythonExe constant in this script.
"@
    exit 1
}

# Sanity-check port 5000: if it's still in TIME_WAIT from a recent shutdown,
# warn before launching so the user understands the upcoming bind error.
$tw = Get-NetTCPConnection -LocalPort 5000 -State TimeWait -ErrorAction SilentlyContinue
if ($tw) {
    Write-Host "==> warning: port 5000 in TIME_WAIT, will clear in ~60s" -ForegroundColor Yellow
    Write-Host "    if launch fails with errno 10048, wait and retry"
}

$env:PYTHONPATH = $RepoRoot + ";" + $env:PYTHONPATH
$env:PYTHONUNBUFFERED = "1"

$argList = @("-m", "carlabridge.main")
if ($Scenario) { $argList += @("--scenario", $Scenario) }
if ($Config)   { $argList += @("--config", $Config) }
if ($LogLevel) { $argList += @("--log-level", $LogLevel) }
if ($NoCarla)  { $argList += @("--no-carla") }

Write-Host "==> launching: $PythonExe $($argList -join ' ')" -ForegroundColor Cyan
Write-Host "==> URLs:" -ForegroundColor Cyan
Write-Host "    healthz : http://localhost:5000/healthz"
Write-Host "    events  : http://localhost:5000/debug/events?n=200"
Write-Host "    mjpeg   : http://localhost:5000/video_feed?camera=city|aerial|ground"
Write-Host "    webrtc  : POST http://localhost:5000/webrtc/{camera}"
Write-Host ""
Push-Location $RepoRoot
try {
    & $PythonExe @argList
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
