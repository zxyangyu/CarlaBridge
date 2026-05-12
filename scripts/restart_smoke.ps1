<#
.SYNOPSIS
    NF7 + AC-8 verification harness: launch + run S1 briefly + shutdown,
    repeat N times. After each cycle, query CARLA for any leftover
    bridge-spawned actors and confirm port 5000 is released.

.PARAMETER Iterations
    Number of start/shutdown cycles. Default 5.

.PARAMETER ScenarioRunSeconds
    How many wall seconds to let the scenario run before shutdown. Default 20.

.EXAMPLE
    .\scripts\restart_smoke.ps1
    .\scripts\restart_smoke.ps1 -Iterations 5 -ScenarioRunSeconds 30
#>

[CmdletBinding()]
param(
    [int]$Iterations = 5,
    [int]$ScenarioRunSeconds = 20
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$PythonExe = "D:/carla/env/python.exe"
$LogDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$results = @()

function Wait-PortFree {
    param([int]$Port, [int]$TimeoutSec = 120)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $conns = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
        if (-not $conns) { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Stop-StaleBridge {
    # If something is currently listening on 5000, POST shutdown and wait.
    $listening = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue
    if ($listening) {
        Write-Host "  pre-clean: stale bridge listening on 5000, posting /admin/shutdown" -ForegroundColor Yellow
        try { Invoke-RestMethod -Uri "http://127.0.0.1:5000/admin/shutdown" -Method Post -TimeoutSec 5 | Out-Null } catch {}
        $owners = $listening | Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($p in $owners) {
            Wait-Process -Id $p -Timeout 30 -ErrorAction SilentlyContinue
            Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
        }
    }
}

function Wait-HealthzReady {
    # Wait until BOTH scenario state == 'running' AND a snapshot is available
    # (meaning the tick loop has produced at least one frame). This avoids the
    # earlier 'starting'-state false positive that shortened the run window.
    param(
        [int]$TimeoutSec = 90,
        [int]$ExpectedPid = 0
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        # Confirm the listener on 5000 is the process we just launched.
        if ($ExpectedPid -gt 0) {
            $listen = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue
            if ($listen -and ($listen.OwningProcess -notcontains $ExpectedPid)) {
                Start-Sleep -Milliseconds 500
                continue
            }
        }
        try {
            $r = Invoke-RestMethod -Uri "http://127.0.0.1:5000/healthz" -TimeoutSec 2 -ErrorAction Stop
            if ($r.scenario -eq "s1_fire/running" -and $r.snapshot.available -eq $true -and $r.tick_fps -gt 0) {
                return $r
            }
        } catch {}
        Start-Sleep -Milliseconds 500
    }
    return $null
}

function Count-CarlaResidual {
    & $PythonExe -c @"
import carla
c = carla.Client('127.0.0.1', 2000)
c.set_timeout(10.0)
w = c.get_world()
vehicles = list(w.get_actors().filter('vehicle.*'))
sensors = list(w.get_actors().filter('sensor.camera.rgb'))
props = list(w.get_actors().filter('static.prop.streetbarrier'))
print(f'{len(vehicles)},{len(sensors)},{len(props)}')
"@
}

# 0. Pre-flight: kill any stale bridge + wait for port to be fully free
#    (TIME_WAIT included). Without this, iteration 1 sees the old bridge.
Stop-StaleBridge
if (-not (Wait-PortFree -Port 5000 -TimeoutSec 120)) {
    Write-Host "FATAL: port 5000 never freed before run start" -ForegroundColor Red
    exit 2
}

for ($i = 1; $i -le $Iterations; $i++) {
    Write-Host "==== iteration $i / $Iterations ====" -ForegroundColor Cyan
    $logFile = Join-Path $LogDir "restart_smoke_$i.log"

    # 1. Launch bridge in the background.
    $env:PYTHONPATH = "$RepoRoot;$($env:PYTHONPATH)"
    $env:PYTHONUNBUFFERED = "1"
    $proc = Start-Process -FilePath $PythonExe `
        -ArgumentList "-m", "carlabridge.main", "--scenario", "s1_fire", "--log-level", "INFO" `
        -WorkingDirectory $RepoRoot `
        -RedirectStandardOutput $logFile `
        -RedirectStandardError "$logFile.err" `
        -PassThru -NoNewWindow
    Write-Host "  pid=$($proc.Id) log=$logFile"

    # 2. Wait until scenario is FULLY running with tick + snapshot. Confirm the
    #    healthz responder is our launched PID, not a stale bridge somewhere.
    $hz = Wait-HealthzReady -TimeoutSec 90 -ExpectedPid $proc.Id
    if (-not $hz) {
        Write-Host "  FAIL: scenario never reported running" -ForegroundColor Red
        # Diagnostic: dump first 30 log lines + check if process is alive
        if (Test-Path $logFile) {
            Write-Host "  --- log tail ---" -ForegroundColor DarkGray
            Get-Content $logFile -Tail 15 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        }
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        $results += [pscustomobject]@{Iter=$i; Started=$false}
        Wait-PortFree -Port 5000 -TimeoutSec 120 | Out-Null
        continue
    }
    $simAtReady = $hz.snapshot.sim_time
    Write-Host ("  ok: scenario={0} sim_at_ready={1:N2} tick_fps={2:N2}" -f `
        $hz.scenario, $simAtReady, $hz.tick_fps)

    # 3. Let scenario run for N seconds.
    Start-Sleep -Seconds $ScenarioRunSeconds

    # 4. Mid-run health check.
    $mid = Invoke-RestMethod -Uri "http://127.0.0.1:5000/healthz" -TimeoutSec 3
    $simRun = $mid.snapshot.sim_time - $simAtReady
    Write-Host ("  mid: sim={0:N2} (+{1:N2} since ready) tick_fps={2:N2}" -f `
        $mid.snapshot.sim_time, $simRun, $mid.tick_fps)

    # 5. Graceful shutdown.
    try {
        $sd = Invoke-RestMethod -Uri "http://127.0.0.1:5000/admin/shutdown" -Method Post -TimeoutSec 5
        Write-Host "  shutdown: $($sd.status)"
    } catch {
        Write-Host "  shutdown POST failed: $_" -ForegroundColor Yellow
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    }

    # 6. Wait for process exit. Shutdown can take >15s under real CARLA load
    #    (GRP destroy, camera detach, sync mode restore) — be patient.
    if (-not $proc.WaitForExit(45000)) {
        Write-Host "  process did not exit in 45s, killing" -ForegroundColor Yellow
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        $cleanExit = $false
    } else {
        $cleanExit = ($proc.ExitCode -eq 0)
    }
    $exitCode = $proc.ExitCode

    # 7. Verify CARLA has no residual bridge actors.
    Start-Sleep -Seconds 2
    $counts = (Count-CarlaResidual).Trim() -split ","
    $vehicles, $sensors, $props = [int]$counts[0], [int]$counts[1], [int]$counts[2]
    Write-Host "  residual: vehicles=$vehicles sensors=$sensors props=$props"

    # 8. Wait for port to fully release before next iteration.
    $portFree = Wait-PortFree -Port 5000 -TimeoutSec 120
    Write-Host "  port_5000_free=$portFree"

    $results += [pscustomobject]@{
        Iter=$i
        Started=$true
        SimAtReady=[math]::Round($simAtReady, 2)
        SimRun=[math]::Round($simRun, 2)
        TickFps=$mid.tick_fps
        ExitCode=$exitCode
        CleanExit=$cleanExit
        ResidualVehicles=$vehicles
        ResidualSensors=$sensors
        ResidualProps=$props
        PortFreed=$portFree
    }
}

Write-Host ""
Write-Host "===== summary =====" -ForegroundColor Green
$results | Format-Table -AutoSize

$csv = Join-Path $LogDir "restart_smoke_summary.csv"
$results | Export-Csv -Path $csv -NoTypeInformation
Write-Host "csv: $csv"

# Pass criteria. AC-8 specifically demands "no residual actor"; NF7 demands
# the loop itself can repeat without the bridge accumulating state. We accept
# CleanExit=False (killed at 45s) if everything else is clean — that's a
# performance issue, not a correctness one.
$correctness = ($results | Where-Object { $_.Started -and `
    $_.ResidualVehicles -eq 0 -and $_.ResidualSensors -eq 0 -and `
    $_.ResidualProps -eq 0 -and $_.PortFreed }).Count

$cleanShutdowns = ($results | Where-Object { $_.CleanExit }).Count

Write-Host ""
Write-Host "  clean-correctness (AC-8 / no residual + port released): $correctness / $Iterations"
Write-Host "  clean-shutdown (process exited <= 45s with code 0):     $cleanShutdowns / $Iterations"

if ($correctness -eq $Iterations) {
    Write-Host "PASS: AC-8 + NF7 — no residual actors, all ports released" -ForegroundColor Green
    if ($cleanShutdowns -lt $Iterations) {
        Write-Host "  (note: $($Iterations - $cleanShutdowns) iter(s) needed force-kill at 45s — see logs/*.log for slow shutdown phase)" -ForegroundColor Yellow
    }
    exit 0
} else {
    Write-Host "FAIL: only $correctness / $Iterations iterations clean" -ForegroundColor Red
    exit 1
}
