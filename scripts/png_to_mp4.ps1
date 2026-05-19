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
    $pngs = @(Get-ChildItem -Path $PngDir -Filter "*.png" -File | Sort-Object Name)
    if ($pngs.Count -eq 0) {
        Write-Warning "skip (no PNGs): $PngDir"
        return
    }

    Write-Host "==> $Mp4Path"
    $firstName = $pngs[0].BaseName
    if ($firstName -match '^\d+$') {
        # Numeric sequence (%08d.png + -start_number). Avoids glob, unsupported on many Windows ffmpeg builds.
        $padWidth = $firstName.Length
        $startNumber = [int]$firstName
        $pattern = Join-Path $PngDir ("%0{0}d.png" -f $padWidth)
        & ffmpeg -y -hide_banner -loglevel error `
            -framerate $Fps -start_number $startNumber -i $pattern `
            -c:v libx264 -pix_fmt yuv420p $Mp4Path
    } else {
        # Non-numeric names: concat demuxer (sorted file list).
        $listFile = Join-Path ([System.IO.Path]::GetTempPath()) ("png_to_mp4_{0}.txt" -f [guid]::NewGuid().ToString('N'))
        try {
            $lines = $pngs | ForEach-Object {
                $path = $_.FullName -replace '\\', '/' -replace "'", "''"
                "file '$path'"
            }
            [System.IO.File]::WriteAllLines($listFile, $lines)
            & ffmpeg -y -hide_banner -loglevel error `
                -f concat -safe 0 -r $Fps -i $listFile `
                -c:v libx264 -pix_fmt yuv420p $Mp4Path
        } finally {
            Remove-Item -LiteralPath $listFile -ErrorAction SilentlyContinue
        }
    }
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
