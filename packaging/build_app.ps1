# Build ActionCheese Sync for Windows into dist\ActionCheese Sync\.
#
#   packaging\build_app.ps1              # bundle ffmpeg -> runs on any PC
#   packaging\build_app.ps1 -NoFfmpeg    # smaller; recipient needs ffmpeg
#
# LICENCE NOTE: most prebuilt Windows ffmpeg binaries (gyan.dev, BtbN) are
# built with GPL components, so a bundled build is subject to the GPL --
# anyone you give it to is entitled to the corresponding source. This project
# is GPL-3.0 with its source public, so that obligation is already met.
#
# The result is UNSIGNED. Windows SmartScreen will warn on first run: the user
# clicks "More info" then "Run anyway". That is a deliberate choice -- a code
# signing certificate is a yearly cost that a free app does not justify -- and
# the download page should say so plainly rather than let people be surprised.

param([switch]$NoFfmpeg)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$venv = if ($env:VENV) { $env:VENV } else { ".venv" }
$python = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "no virtualenv at $venv (run: python -m venv .venv; .venv\Scripts\pip install -e `".[dev]`")"
}

& $python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "installing pyinstaller..."
    & $python -m pip install -q pyinstaller
}

$env:ACSYNC_VERSION = & $python -c "import syncmyvids; print(syncmyvids.__version__)"

if (-not $NoFfmpeg) {
    $ff = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
    $fp = (Get-Command ffprobe -ErrorAction SilentlyContinue).Source
    if (-not $ff -or -not $fp) {
        throw "ffmpeg/ffprobe not on PATH. Install them (winget install Gyan.FFmpeg) or pass -NoFfmpeg"
    }
    $env:ACSYNC_BUNDLE_FFMPEG = "1"
    $env:ACSYNC_FFMPEG = $ff
    $env:ACSYNC_FFPROBE = $fp
    Write-Host "bundling ffmpeg from $ff"
} else {
    Remove-Item Env:\ACSYNC_BUNDLE_FFMPEG -ErrorAction SilentlyContinue
    Write-Host "not bundling ffmpeg; the recipient must have it installed"
}

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
& (Join-Path $venv "Scripts\pyinstaller.exe") --noconfirm --clean `
    --distpath dist --workpath build `
    packaging\actioncheese-sync-windows.spec

$out = "dist\ActionCheese Sync"
if (-not (Test-Path $out)) { throw "build produced nothing in $out" }

$size = "{0:N0} MB" -f ((Get-ChildItem $out -Recurse | Measure-Object Length -Sum).Sum / 1MB)
Write-Host ""
Write-Host "built $out ($size)"
Write-Host ""
Write-Host "To share it, zip the folder:"
Write-Host "  Compress-Archive -Path '$out' -DestinationPath 'dist\ActionCheese-Sync-$env:ACSYNC_VERSION-win.zip'"
Write-Host ""
Write-Host "It is unsigned, so the first run shows SmartScreen:"
Write-Host "  More info -> Run anyway"
