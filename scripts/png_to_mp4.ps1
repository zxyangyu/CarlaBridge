# Convert CarlaBridge PNG sequences (from --record-dir) to H.264 MP4.
#
# Usage (from repo root):
#   .\scripts\png_to_mp4.ps1 -InputDir recordings -Fps 25
#   .\scripts\png_to_mp4.ps1 -InputDir recordings\aerial -OutFile aerial.mp4
#
# Requires ffmpeg on PATH: https://ffmpeg.org/download.html

param(
    [Parameter(Mandatory = $true)]
    [string] $InputDir,
    [string] $OutDir = "",
    [int] $Fps = 25,
    [string] $OutFile = ""
)

$ErrorActionPreference = "Stop"
$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ffmpeg) {
    Write-Error "ffmpeg not found on PATH. Install ffmpeg and retry."
}

$inputPath = Resolve-Path $InputDir
$outRoot = if ($OutDir) { (Resolve-Path -LiteralPath $OutDir -ErrorAction SilentlyContinue) } else { $inputPath }
if (-not $outRoot) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null; $outRoot = Resolve-Path $OutDir }

function Convert-OneCamera {
    param([string] $PngDir, [string] $Mp4Path)
    $pattern = Join-Path $PngDir "*.png"
    if (-not (Test-Path $pattern)) {
        Write-Warning "skip (no PNGs): $PngDir"
        return
    }
    Write-Host "==> $Mp4Path"
    & ffmpeg -y -hide_banner -loglevel error `
        -framerate $Fps -pattern_type glob -i (Join-Path $PngDir "*.png") `
        -c:v libx264 -pix_fmt yuv420p $Mp4Path
    if ($LASTEXITCODE -ne 0) { throw "ffmpeg failed for $PngDir" }
}

if ($OutFile) {
    Convert-OneCamera -PngDir $inputPath.Path -Mp4Path (Join-Path $outRoot $OutFile)
    exit 0
}

# If InputDir itself contains PNGs, one output; else each child subfolder is a camera.
$direct = Get-ChildItem -Path $inputPath -Filter "*.png" -File -ErrorAction SilentlyContinue
if ($direct.Count -gt 0) {
    $name = Split-Path $inputPath -Leaf
    Convert-OneCamera -PngDir $inputPath.Path -Mp4Path (Join-Path $outRoot "$name.mp4")
    exit 0
}

Get-ChildItem -Path $inputPath -Directory | ForEach-Object {
    Convert-OneCamera -PngDir $_.FullName -Mp4Path (Join-Path $outRoot "$($_.Name).mp4")
}

Write-Host "Done. MP4 files in $outRoot"
