# SafeDrop one-key installer for Windows (PowerShell 5.1+).
#
# Usage (one-liner from the README):
#
#   iwr -useb https://raw.githubusercontent.com/gclinian/SafeDrop/main/scripts/install.ps1 | iex
#
# Or, after `git clone`:
#
#   .\scripts\install.ps1
#
# What this does:
#
#   * Checks for Python 3.10+ on PATH (py launcher or python.exe).
#   * Creates a venv at $env:LOCALAPPDATA\SafeDrop\venv.
#   * Installs safedrop[mcp] into it (from PyPI by default, or from
#     the latest GitHub Release wheel with -FromRelease).
#   * Drops .cmd launchers in $env:LOCALAPPDATA\SafeDrop\bin —
#     safedrop-gui.cmd, safedrop.cmd, safedrop-mcp.cmd, etc.
#   * Prints the PATH line to add for those launchers to be discoverable.
#
# What it does NOT do:
#   * Install Python — points you at https://www.python.org/downloads/windows/
#     if it's missing (the python.org build ships with tkinter).
#   * Modify the registry.
#   * Touch C:\Program Files anything.
#
# Re-running is safe — it upgrades the venv in place.

[CmdletBinding()]
param(
    # SafeDrop isn't on PyPI yet — default is to pull the latest wheel
    # from GitHub Releases. Pass -FromPyPI once that changes.
    [switch]$FromRelease = $true,
    [switch]$FromPyPI,
    [string]$ReleaseTag = "",
    [string]$Home_ = (Join-Path $env:LOCALAPPDATA "SafeDrop"),
    [string]$Python = ""
)
if ($FromPyPI.IsPresent) {
    Write-Host "warn: safedrop is not on PyPI yet — falling back to GitHub Releases." -ForegroundColor Yellow
    $FromPyPI = $false
    $FromRelease = $true
}

$ErrorActionPreference = "Stop"

function Write-Step($msg) {
    Write-Host "==> $msg" -ForegroundColor Cyan
}
function Write-Ok($msg) {
    Write-Host "ok  $msg" -ForegroundColor Green
}
function Write-Warn2($msg) {
    Write-Host "warn: $msg" -ForegroundColor Yellow
}
function Fail($msg) {
    Write-Host "error: $msg" -ForegroundColor Red
    exit 1
}

# ---- 1. python ----------------------------------------------------

Write-Step "checking Python interpreter"

function Resolve-Python {
    if ($Python -ne "") {
        if (Test-Path $Python) { return $Python }
        Fail "Python at '$Python' not found."
    }
    # Try `py -3` first (the Windows launcher), then python.exe.
    foreach ($candidate in @("py", "python", "python3")) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) {
            if ($candidate -eq "py") {
                return @($cmd.Source, "-3")
            }
            return @($cmd.Source)
        }
    }
    return $null
}

$pyArgs = Resolve-Python
if (-not $pyArgs) {
    Fail @"
No Python interpreter found on PATH.

Install Python 3.10+ from https://www.python.org/downloads/windows/
(the python.org build ships with tkinter — required for the GUI).
Then re-run this script.
"@
}

$pyExe = $pyArgs[0]
$pyPreArgs = $pyArgs[1..($pyArgs.Length-1)]

# Verify >= 3.10.
$verStr = & $pyExe @pyPreArgs -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$verParts = $verStr.Trim().Split('.')
$major = [int]$verParts[0]
$minor = [int]$verParts[1]
if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) {
    Fail "Python $verStr is too old. SafeDrop needs Python 3.10 or newer."
}
$exePath = & $pyExe @pyPreArgs -c "import sys; print(sys.executable)"
Write-Ok "Python $verStr at $($exePath.Trim())"

# ---- 2. tkinter check --------------------------------------------

Write-Step "checking tkinter (needed for the desktop GUI)"
try {
    & $pyExe @pyPreArgs -c "import tkinter" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "missing" }
    $tkVer = & $pyExe @pyPreArgs -c "import tkinter; print(tkinter.TkVersion)"
    Write-Ok "tkinter $($tkVer.Trim()) available"
} catch {
    Write-Warn2 "tkinter not available in this Python."
    Fail @"

The python.org Windows installer bundles tkinter by default. If yours
doesn't have it, the simplest fix is to install python.org's build from
https://www.python.org/downloads/windows/ and re-run this script.

CLI + MCP work without tkinter, but the desktop GUI won't launch.
"@
}

# ---- 3. venv ------------------------------------------------------

$venv = Join-Path $Home_ "venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
$venvPip = Join-Path $venv "Scripts\pip.exe"
Write-Step "creating venv at $venv"

New-Item -ItemType Directory -Force -Path $Home_ | Out-Null
if (-not (Test-Path $venvPython)) {
    & $pyExe @pyPreArgs -m venv $venv
    if ($LASTEXITCODE -ne 0) { Fail "venv creation failed" }
}
Write-Ok "venv ready"

# ---- 4. install --------------------------------------------------

Write-Step "installing safedrop into the venv"
& $venvPython -m pip install --upgrade --quiet pip
if ($LASTEXITCODE -ne 0) { Fail "pip upgrade failed" }

if ($FromRelease.IsPresent) {
    if (-not $ReleaseTag) {
        $rel = Invoke-RestMethod "https://api.github.com/repos/gclinian/SafeDrop/releases/latest"
        $ReleaseTag = $rel.tag_name
        if (-not $ReleaseTag) { Fail "could not determine latest release tag" }
    }
    $version = $ReleaseTag.TrimStart("v")
    $wheel = "safedrop-$version-py3-none-any.whl"
    $url = "https://github.com/gclinian/SafeDrop/releases/download/$ReleaseTag/$wheel"
    Write-Step "fetching $url"
    $tmpDir = Join-Path $env:TEMP ("safedrop-install-" + (Get-Random))
    New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
    Invoke-WebRequest -Uri $url -OutFile (Join-Path $tmpDir $wheel)
    & $venvPip install --quiet "$(Join-Path $tmpDir $wheel)[mcp]"
    Remove-Item -Recurse -Force $tmpDir
} else {
    & $venvPip install --quiet --upgrade 'safedrop[mcp]'
}
if ($LASTEXITCODE -ne 0) { Fail "pip install failed" }
Write-Ok "installed safedrop"

# ---- 5. launchers -------------------------------------------------

$binDir = Join-Path $Home_ "bin"
New-Item -ItemType Directory -Force -Path $binDir | Out-Null
Write-Step "creating launchers in $binDir"

$guiCmd = @"
@echo off
"$venvPython" -m safedrop %*
"@
Set-Content -Path (Join-Path $binDir "safedrop-gui.cmd") -Value $guiCmd -Encoding ASCII

foreach ($tool in @("safedrop", "safedrop-mcp", "safedrop-mcp-tokens", "safedrop-agent", "safedrop-beacon")) {
    $toolExe = Join-Path $venv "Scripts\$tool.exe"
    if (Test-Path $toolExe) {
        $stub = @"
@echo off
"$toolExe" %*
"@
        Set-Content -Path (Join-Path $binDir "$tool.cmd") -Value $stub -Encoding ASCII
    }
}
Write-Ok "launchers in $binDir"

# ---- 6. final report ---------------------------------------------

$inPath = $env:PATH -split ';' | Where-Object { $_ -eq $binDir }

Write-Host ""
Write-Host "SafeDrop is installed." -ForegroundColor Green
Write-Host ""
Write-Host "  GUI:        safedrop-gui"
Write-Host "  CLI:        safedrop ls"
Write-Host "  MCP:        safedrop-mcp        (point Claude Code / Cursor at this)"
Write-Host "  agent:      `$env:ANTHROPIC_API_KEY=`"...`"; safedrop-agent"
Write-Host "  beacon:     safedrop-beacon --bind 127.0.0.1:47900"
Write-Host ""
if (-not $inPath) {
    Write-Host "Note: $binDir is not on your PATH." -ForegroundColor Yellow
    Write-Host "      Add it for this user with:"
    Write-Host ""
    Write-Host "        [Environment]::SetEnvironmentVariable('PATH', `"$binDir;`$([Environment]::GetEnvironmentVariable('PATH','User'))`", 'User')"
    Write-Host ""
    Write-Host "      Then open a new PowerShell. Or run safedrop with the full path right now:"
    Write-Host "        & '$binDir\safedrop-gui.cmd'"
}
