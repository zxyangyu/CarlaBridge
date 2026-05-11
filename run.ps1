<#
.SYNOPSIS
    Launch CarlaBridge using the bundled conda env at D:/carla/env.

.PARAMETER Scenario
    Override scenario.default (default: from config).

.PARAMETER Config
    Extra TOML overlay path.

.PARAMETER LogLevel
    DEBUG/INFO/WARN/ERROR (default: from config).

.EXAMPLE
    .\run.ps1
    .\run.ps1 -Scenario s1_fire -LogLevel DEBUG
#>

[CmdletBinding()]
param(
    [string]$Scenario,
    [string]$Config,
    [string]$LogLevel
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = "D:/carla/env/python.exe"

if (-not (Test-Path $PythonExe)) {
    Write-Error "conda env python not found at $PythonExe"
    exit 1
}

$env:PYTHONPATH = $RepoRoot + ";" + $env:PYTHONPATH
$env:PYTHONUNBUFFERED = "1"

$argList = @("-m", "carlabridge.main")
if ($Scenario) { $argList += @("--scenario", $Scenario) }
if ($Config)   { $argList += @("--config", $Config) }
if ($LogLevel) { $argList += @("--log-level", $LogLevel) }

Write-Host "==> launching: $PythonExe $($argList -join ' ')" -ForegroundColor Cyan
Push-Location $RepoRoot
try {
    & $PythonExe @argList
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
