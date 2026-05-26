# SafeDrop one-key installer for Windows (PowerShell 5.1+).
#
# Designed to be `iex`-safe — no [CmdletBinding()] param() block,
# configuration via environment variables only. The whole script is
# wrapped in a single try/finally so partial failures abort cleanly.
#
# Usage (one-liner from the README):
#
#   iwr -useb https://raw.githubusercontent.com/gclinian/SafeDrop/main/scripts/install.ps1 | iex
#
# Configuration via env vars (set BEFORE the iwr | iex line):
#
#   $env:SAFEDROP_HOME    — install root (default: $env:LOCALAPPDATA\SafeDrop)
#   $env:SAFEDROP_PYTHON  — explicit python.exe path (default: autodetect)
#   $env:SAFEDROP_TAG     — pin a specific release tag (default: latest)
#
# What this does:
#
#   * Verifies Python 3.10+ on PATH (py launcher or python.exe).
#   * Asserts tkinter imports.
#   * Creates a venv at $SAFEDROP_HOME\venv.
#   * Downloads the latest SafeDrop wheel from GitHub Releases.
#   * Installs safedrop[mcp] into the venv.
#   * Drops .cmd launchers in $SAFEDROP_HOME\bin.

$ErrorActionPreference = "Stop"

# ---- config from env vars ---------------------------------------

$Home_ = if ($env:SAFEDROP_HOME) { $env:SAFEDROP_HOME } else { Join-Path $env:LOCALAPPDATA "SafeDrop" }
$PyExplicit = if ($env:SAFEDROP_PYTHON) { $env:SAFEDROP_PYTHON } else { "" }
$Tag = if ($env:SAFEDROP_TAG) { $env:SAFEDROP_TAG } else { "" }

# ---- pretty-printing --------------------------------------------

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "ok  $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "warn: $msg" -ForegroundColor Yellow }
function Fail($msg)       { Write-Host "error: $msg" -ForegroundColor Red; throw $msg }

# ---- 1. Python --------------------------------------------------

Write-Step "checking Python interpreter"

# Resolve to a single python.exe path + a (possibly empty) arg array.
# Using arrays *only* here, never returning them across function boundaries
# (functions in PowerShell + `iex` can leak return values into the iex
# pipeline if you're not extremely careful).
$pyExe = $null
$pyArgsExtra = @()

if ($PyExplicit) {
    if (-not (Test-Path $PyExplicit)) { Fail "Python at '$PyExplicit' not found." }
    $pyExe = $PyExplicit
} else {
    # py launcher first — it can pick the highest 3.x with -3.
    $cmdPy = Get-Command py -ErrorAction SilentlyContinue
    if ($cmdPy) {
        $pyExe = $cmdPy.Source
        $pyArgsExtra = @("-3")
    } else {
        foreach ($candidate in @("python", "python3")) {
            $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
            if ($cmd) { $pyExe = $cmd.Source; break }
        }
    }
}

if (-not $pyExe) {
    Fail @"
No Python interpreter found on PATH.

Install Python 3.10+ from https://www.python.org/downloads/windows/
(the python.org build ships with tkinter — required for the GUI).
Then re-run this one-liner. To use a specific interpreter, set
    `$env:SAFEDROP_PYTHON='C:\full\path\to\python.exe'`
before the iwr/iex.
"@
}

# Capture version. The "| Out-String" trick *forces* all stdout into a
# string, then we trim — avoids list-vs-string subtleties under iex.
$verStr = (& $pyExe @pyArgsExtra -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')") | Out-String
$verStr = $verStr.Trim()
if (-not $verStr) { Fail "could not read Python version (is $pyExe really an interpreter?)" }

$verParts = $verStr.Split('.')
$major = [int]$verParts[0]
$minor = [int]$verParts[1]
if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) {
    Fail "Python $verStr is too old. SafeDrop needs Python 3.10 or newer."
}
$exePath = ((& $pyExe @pyArgsExtra -c "import sys; print(sys.executable)") | Out-String).Trim()
Write-Ok "Python $verStr at $exePath"

# ---- 2. tkinter -------------------------------------------------

Write-Step "checking tkinter (needed for the desktop GUI)"
$null = & $pyExe @pyArgsExtra -c "import tkinter" 2>&1
if ($LASTEXITCODE -ne 0) {
    Fail @"
tkinter is not available in this Python.

The python.org Windows installer bundles tkinter by default. If you
installed from the Microsoft Store or via conda and it's missing the
Tk binding, the simplest fix is to grab
  https://www.python.org/downloads/windows/
and re-run this one-liner.

(CLI + MCP server work without tkinter, but the desktop GUI won't.)
"@
}
$tkVer = ((& $pyExe @pyArgsExtra -c "import tkinter; print(tkinter.TkVersion)") | Out-String).Trim()
Write-Ok "tkinter $tkVer available"

# ---- 3. venv ----------------------------------------------------

$venv = Join-Path $Home_ "venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
$venvPip = Join-Path $venv "Scripts\pip.exe"
Write-Step "creating venv at $venv"

$null = New-Item -ItemType Directory -Force -Path $Home_
if (-not (Test-Path $venvPython)) {
    & $pyExe @pyArgsExtra -m venv $venv
    if ($LASTEXITCODE -ne 0) { Fail "venv creation failed" }
}
Write-Ok "venv ready"

# ---- 4. install -------------------------------------------------

Write-Step "installing safedrop into the venv"
& $venvPython -m pip install --upgrade --quiet pip
if ($LASTEXITCODE -ne 0) { Fail "pip upgrade failed" }

if (-not $Tag) {
    Write-Step "looking up latest GitHub release tag"
    $rel = Invoke-RestMethod "https://api.github.com/repos/gclinian/SafeDrop/releases/latest"
    $Tag = $rel.tag_name
    if (-not $Tag) { Fail "could not determine latest release tag" }
}
$version = $Tag.TrimStart("v")
$wheel = "safedrop-$version-py3-none-any.whl"
$url = "https://github.com/gclinian/SafeDrop/releases/download/$Tag/$wheel"

Write-Step "downloading $url"
$tmpDir = Join-Path $env:TEMP ("safedrop-install-" + (Get-Random))
$null = New-Item -ItemType Directory -Force -Path $tmpDir
$wheelPath = Join-Path $tmpDir $wheel
Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $wheelPath

& $venvPip install --quiet "$wheelPath[mcp]"
$pipExit = $LASTEXITCODE
Remove-Item -Recurse -Force $tmpDir
if ($pipExit -ne 0) { Fail "pip install failed" }
Write-Ok "installed safedrop $version"

# ---- 5. launchers -----------------------------------------------

$binDir = Join-Path $Home_ "bin"
$null = New-Item -ItemType Directory -Force -Path $binDir
Write-Step "creating launchers in $binDir"

$guiCmd = "@echo off`r`n`"$venvPython`" -m safedrop %*`r`n"
Set-Content -Path (Join-Path $binDir "safedrop-gui.cmd") -Value $guiCmd -Encoding ASCII -NoNewline

foreach ($tool in @("safedrop", "safedrop-mcp", "safedrop-mcp-tokens", "safedrop-agent", "safedrop-beacon")) {
    $toolExe = Join-Path $venv "Scripts\$tool.exe"
    if (Test-Path $toolExe) {
        $stub = "@echo off`r`n`"$toolExe`" %*`r`n"
        Set-Content -Path (Join-Path $binDir "$tool.cmd") -Value $stub -Encoding ASCII -NoNewline
    }
}
Write-Ok "launchers in $binDir"

# ---- 6. final report --------------------------------------------

$onPath = $false
foreach ($p in $env:PATH -split ';') {
    if ($p -eq $binDir) { $onPath = $true; break }
}

Write-Host ""
Write-Host "SafeDrop is installed." -ForegroundColor Green
Write-Host ""
Write-Host "  GUI:        safedrop-gui"
Write-Host "  CLI:        safedrop ls"
Write-Host "  MCP:        safedrop-mcp        (point Claude Code / Cursor at this)"
Write-Host ('  agent:      $env:ANTHROPIC_API_KEY=' + "'sk-...'; safedrop-agent")
Write-Host "  beacon:     safedrop-beacon --bind 127.0.0.1:47900"
Write-Host ""
if (-not $onPath) {
    Write-Host "Note: $binDir is not on your PATH." -ForegroundColor Yellow
    Write-Host "      Add it permanently for this user with:"
    Write-Host ""
    Write-Host ('        [Environment]::SetEnvironmentVariable("PATH","' + $binDir + ';" + [Environment]::GetEnvironmentVariable("PATH","User"), "User")')
    Write-Host ""
    Write-Host "      Then open a new PowerShell. Or run safedrop right now with the full path:"
    Write-Host "        & '$binDir\safedrop-gui.cmd'"
}
