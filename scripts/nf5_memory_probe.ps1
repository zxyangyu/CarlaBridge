<#
.SYNOPSIS
    NF5 verification: probe bridge memory growth over a fixed window.

.DESCRIPTION
    Assumes a bridge instance is already running on port 5000. Polls
    /healthz every 30 s for N minutes, sampling tick_fps + bridge process
    RSS. Writes CSV. Pass criterion: peak RSS - baseline RSS < 200 MB
    (spec NF5).

    Tick stability (NF1) is also recorded — note that on hardware that
    cannot sustain 30 Hz (CARLA renderer bound), NF1 is documented as
    intentionally unmet (spec D5). The CSV still records actual tick_fps
    so we can argue stability AT the achievable rate.

.PARAMETER DurationMinutes
    Probe length. Default 5.

.PARAMETER SampleIntervalSec
    Seconds between samples. Default 30.

.EXAMPLE
    .\scripts\nf5_memory_probe.ps1
    .\scripts\nf5_memory_probe.ps1 -DurationMinutes 30
#>

[CmdletBinding()]
param(
    [int]$DurationMinutes = 5,
    [int]$SampleIntervalSec = 30
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$LogDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$csv = Join-Path $LogDir "nf5_memory_$stamp.csv"

# Locate the bridge process.
$bridgePid = (Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue).OwningProcess | Select-Object -First 1
if (-not $bridgePid) {
    Write-Error "no bridge listening on 5000. Start with `python run.py` first."
    exit 2
}
Write-Host "==> probing bridge pid=$bridgePid for $DurationMinutes min @ $SampleIntervalSec s interval" -ForegroundColor Cyan

$samples = @()
$start = Get-Date
$deadline = $start.AddMinutes($DurationMinutes)
$baselineMb = $null

while ((Get-Date) -lt $deadline) {
    try {
        $hz = Invoke-RestMethod -Uri "http://127.0.0.1:5000/healthz" -TimeoutSec 5
        $proc = Get-Process -Id $bridgePid -ErrorAction Stop
        $rssMb = [math]::Round($proc.WorkingSet64 / 1MB, 2)
        $elapsed = [math]::Round(((Get-Date) - $start).TotalSeconds, 1)

        if ($null -eq $baselineMb) { $baselineMb = $rssMb }
        $deltaMb = [math]::Round($rssMb - $baselineMb, 2)

        $row = [pscustomobject]@{
            ElapsedSec = $elapsed
            TickFps = $hz.tick_fps
            SimTime = $hz.snapshot.sim_time
            RssMb = $rssMb
            DeltaMb = $deltaMb
            ScenarioState = $hz.scenario
            FrontendClients = $hz.clients.frontend
            AgentClients = $hz.clients.agent
        }
        $samples += $row
        Write-Host ("  [{0,5:N1}s] rss={1,7:N2} MB (Δ{2,+6:N2}) fps={3,5:N2} sim={4,6:N2}" -f `
            $elapsed, $rssMb, $deltaMb, $hz.tick_fps, $hz.snapshot.sim_time)
    } catch {
        Write-Host "  sample error: $_" -ForegroundColor Yellow
    }
    Start-Sleep -Seconds $SampleIntervalSec
}

$samples | Export-Csv -Path $csv -NoTypeInformation
Write-Host "csv: $csv"

# Verdict
$peakDelta = ($samples | Measure-Object -Property DeltaMb -Maximum).Maximum
$fpsValues = $samples | Where-Object { $_.TickFps -gt 0 } | Select-Object -ExpandProperty TickFps
if ($fpsValues.Count -ge 2) {
    $fpsMean = ($fpsValues | Measure-Object -Average).Average
    $fpsStd = [math]::Sqrt(($fpsValues | ForEach-Object { ($_ - $fpsMean) * ($_ - $fpsMean) } | Measure-Object -Average).Average)
} else {
    $fpsMean = 0; $fpsStd = 0
}

Write-Host ""
Write-Host "==== verdict ====" -ForegroundColor Green
Write-Host ("  peak ΔRSS  : {0:N2} MB   (NF5 threshold 200 MB)" -f $peakDelta)
Write-Host ("  tick_fps   : mean={0:N2} ±{1:N2} Hz  (NF1 target 30 Hz ±2 Hz)" -f $fpsMean, $fpsStd)

if ($peakDelta -lt 200) {
    Write-Host "  NF5 PASS" -ForegroundColor Green
} else {
    Write-Host "  NF5 FAIL — memory growth exceeded 200 MB" -ForegroundColor Red
}

# NF1 is intentionally documented as not-meetable on demo hardware (spec D5).
# We report observed stability AT the achievable rate as evidence the bridge
# itself is stable.
if ($fpsValues.Count -gt 0 -and $fpsStd / [math]::Max($fpsMean, 0.1) -lt 0.3) {
    Write-Host "  NF1-relative PASS — tick stable around its sustained rate (jitter <30%)"
} else {
    Write-Host "  NF1-relative WARN — large tick jitter (>30%)"
}
