# Tacit bootstrap installer (Windows).
#
#   irm https://raw.githubusercontent.com/SebSilent/Tacit/HEAD/install.ps1 | iex
#
# Clones the source, builds a private virtualenv, and writes a `tacit` launcher.
# Safe to re-run: it updates the checkout instead of replacing it.
#
# NOTE: this script never calls `exit` — piped into iex, that would close your
# shell. Failures throw instead.

$ErrorActionPreference = 'Stop'

$RepoUrl  = if ($env:TACIT_REPO_URL)  { $env:TACIT_REPO_URL }  else { 'https://github.com/SebSilent/Tacit.git' }
$Branch   = if ($env:TACIT_BRANCH)    { $env:TACIT_BRANCH }    else { '' }
$TacitHome = if ($env:TACIT_HOME)     { $env:TACIT_HOME }      else { Join-Path $env:USERPROFILE '.tacit' }
$AppDir   = if ($env:TACIT_APP_DIR)   { $env:TACIT_APP_DIR }   else { Join-Path $env:LOCALAPPDATA 'Tacit\app' }
$BinDir   = if ($env:TACIT_BIN_DIR)   { $env:TACIT_BIN_DIR }   else { Join-Path $env:LOCALAPPDATA 'Tacit\bin' }
$WantBrowser = $env:TACIT_NO_BROWSER -ne '1'

foreach ($a in $args) {
    switch -Regex ($a) {
        '^--no-browser$|^-SkipBrowser$' { $WantBrowser = $false }
        '^--branch$'                    { $Branch = $args[$args.IndexOf($a) + 1] }
    }
}

$script:UseColor = $true
try { if ([Console]::IsOutputRedirected) { $script:UseColor = $false } } catch { }
function Say  ($m) { if ($script:UseColor) { Write-Host "-> $m" -ForegroundColor Cyan }    else { Write-Host "-> $m" } }
function Ok   ($m) { if ($script:UseColor) { Write-Host " v $m" -ForegroundColor Green }   else { Write-Host " v $m" } }
function Warn ($m) { if ($script:UseColor) { Write-Host " ! $m" -ForegroundColor Yellow }  else { Write-Host " ! $m" } }
function Die  ($m) { if ($script:UseColor) { Write-Host " x $m" -ForegroundColor Red }     else { Write-Host " x $m" }; throw $m }

# Native commands (git, npm, npx) write progress to stderr, and with
# ErrorActionPreference=Stop PowerShell turns that into a terminating error.
# This runs them without that trap and returns the exit code.
function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $Exe @Arguments 2>&1
        return [pscustomobject]@{ Code = $LASTEXITCODE; Output = ($out | Out-String) }
    } finally { $ErrorActionPreference = $prev }
}

Write-Host ''
Write-Host ' Tacit - the Silent Harness' -ForegroundColor White
Write-Host ''

# Older Windows defaults to TLS 1.0, which makes the download fail with a
# message that says nothing useful. Ask for 1.2 before anything is fetched.
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

if (-not $IsWindows -and $PSVersionTable.PSVersion.Major -ge 6) {
    Die 'this installer is for Windows. On macOS/Linux run install.sh instead.'
}

# ── Python ────────────────────────────────────────────────────────────────
$Python = $null
foreach ($cand in @('py', 'python', 'python3')) {
    $exe = Get-Command $cand -ErrorAction SilentlyContinue
    if (-not $exe) { continue }
    $probeArgs = if ($cand -eq 'py') { @('-3', '-c') } else { @('-c') }
    try {
        & $exe.Source @probeArgs 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { $Python = @($exe.Source) + $probeArgs[0..($probeArgs.Length-2)]; break }
    } catch { }
}
if (-not $Python) {
    Die 'Python 3.10 or newer is required. Install it from https://python.org and re-run.'
}
$PyExe = $Python[0]
$PyPre = @()
if ($Python.Length -gt 1) { $PyPre = @($Python[1]) }
Ok "prerequisites present ($(& $PyExe @PyPre --version 2>&1))"

# ── source checkout ───────────────────────────────────────────────────────
$HasGit = [bool](Get-Command git -ErrorAction SilentlyContinue)
if ((Test-Path (Join-Path $AppDir 'backend\main.py')) -and -not (Test-Path (Join-Path $AppDir '.git'))) {
    Warn "$AppDir already holds the source but is not a checkout - using it as-is"
} elseif (Test-Path (Join-Path $AppDir '.git')) {
    if ($HasGit) {
        Say "updating $AppDir"
        if ($Branch) {
            [void](Invoke-Native 'git' @('-C', $AppDir, 'fetch', '--depth', '1', 'origin', $Branch))
            [void](Invoke-Native 'git' @('-C', $AppDir, 'checkout', '-q', $Branch))
            [void](Invoke-Native 'git' @('-C', $AppDir, 'reset', '-q', '--hard', "origin/$Branch"))
        } else {
            [void](Invoke-Native 'git' @('-C', $AppDir, 'fetch', '--depth', '1'))
            [void](Invoke-Native 'git' @('-C', $AppDir, 'reset', '-q', '--hard', 'FETCH_HEAD'))
        }
    } else {
        Warn 'git not found; leaving the existing checkout in place'
    }
} else {
    New-Item -ItemType Directory -Force -Path (Split-Path $AppDir -Parent) | Out-Null
    if ($HasGit) {
        Say "downloading into $AppDir"
        $cloneArgs = @('clone', '--depth', '1')
        if ($Branch) { $cloneArgs += @('--branch', $Branch) }
        $cloneArgs += @($RepoUrl, $AppDir)
        $clone = Invoke-Native 'git' $cloneArgs
        if ($clone.Code -ne 0) { Die "could not clone $RepoUrl`n$($clone.Output)" }
    } else {
        Say "git not found - downloading the source archive instead"
        # $Branch is empty unless --branch was given, and an empty name builds a
        # URL that cannot resolve (/refs/heads/.zip). Try the real default names
        # in order rather than trusting one.
        $refs = if ($Branch) { @($Branch) } else { @('main', 'master') }
        $base = $RepoUrl -replace '\.git$', ''
        $tmp = Join-Path $env:TEMP ("tacit-" + [Guid]::NewGuid().ToString('N') + '.zip')
        $got = $false
        foreach ($ref in $refs) {
            try {
                Invoke-WebRequest -Uri "$base/archive/refs/heads/$ref.zip" -OutFile $tmp -UseBasicParsing
                $got = $true
                break
            } catch { }
        }
        if (-not $got) {
            Die "could not download the source archive. Install git, or pass --branch NAME."
        }
        try {
            $extract = Join-Path $env:TEMP ("tacit-" + [Guid]::NewGuid().ToString('N'))
            Expand-Archive -Path $tmp -DestinationPath $extract -Force
            $inner = Get-ChildItem $extract -Directory | Select-Object -First 1
            if (-not $inner) { Die 'the downloaded archive looked empty' }
            Move-Item $inner.FullName $AppDir
        } finally {
            Remove-Item $tmp -ErrorAction SilentlyContinue
        }
    }
}
if (-not (Test-Path (Join-Path $AppDir 'backend\main.py'))) { Die "$AppDir does not look like Tacit" }
Ok 'source ready'

# ── virtualenv ────────────────────────────────────────────────────────────
$Venv = Join-Path $AppDir '.venv'
$VenvPy = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path $VenvPy)) {
    Say 'creating a virtual environment'
    & $PyExe @PyPre -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Die 'could not create the virtualenv' }
}
Say 'installing Python dependencies'
& $VenvPy -m pip install --quiet --upgrade pip 2>$null | Out-Null
& $VenvPy -m pip install --quiet -r (Join-Path $AppDir 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Die 'pip install failed' }
Ok 'dependencies installed'

# ── optional browser tool ─────────────────────────────────────────────────
if ($WantBrowser -and (Get-Command npm -ErrorAction SilentlyContinue)) {
    Say 'installing the browser driver (Playwright, ~130 MB - skip with TACIT_NO_BROWSER=1)'
    Push-Location $AppDir
    try {
        $n1 = Invoke-Native 'npm' @('install', '--silent', '--no-audit', '--no-fund')
        $n2 = Invoke-Native 'npx' @('--yes', 'playwright', 'install', 'chromium')
        if ($n1.Code -eq 0 -and $n2.Code -eq 0) { Ok 'browser automation available' }
        else { Warn "browser install failed - everything else works; run 'npm install' then 'npx playwright install chromium' in $AppDir later" }
    } catch {
        Warn "browser install failed: $($_.Exception.Message)"
    } finally { Pop-Location }
} elseif ($WantBrowser) {
    Warn 'node/npm not found - skipping the browser tool (everything else works)'
}

# ── launcher ──────────────────────────────────────────────────────────────
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$Launcher = Join-Path $BinDir 'tacit.cmd'
@"
@echo off
set "TACIT_HOME=$TacitHome"
cd /d "$AppDir"
"$VenvPy" -m backend.main %*
"@ | Set-Content -Path $Launcher -Encoding ASCII
Ok "launcher written to $Launcher"

$UserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($env:TACIT_NO_PATH -ne '1' -and $UserPath -notlike "*$BinDir*") {
    [Environment]::SetEnvironmentVariable('Path', "$UserPath;$BinDir", 'User')
    Ok 'added the launcher folder to your user PATH (open a new terminal to use it)'
}

$Port = if ($env:TACIT_PORT) { $env:TACIT_PORT } else { '8550' }
Write-Host ''
Write-Host 'done.' -ForegroundColor White
Write-Host ''
Write-Host "  start:  $Launcher"
Write-Host "  or:     cd $AppDir; & '$VenvPy' -m backend.main"
Write-Host "  open:   http://localhost:$Port"
Write-Host "  state:  $TacitHome"
Write-Host ''
Write-Host 'Then add a provider in Settings > Providers.'
Write-Host ''
