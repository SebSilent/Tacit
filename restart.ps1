# Restart Tacit by running the launcher (the same script a person runs, and the
# one the Restart button's watcher runs). There is no second mechanism here.
#
# What it used to do, and why that is gone: it enumerated processes by their
# executable name (python.exe, uvicorn.exe), then kept the ones whose command
# line merely contained the word "Tacit" or the string "uvicorn". The candidate
# list was therefore every Python on the machine, and the match was a substring
# test against a path that any script run from this folder happens to satisfy.
# backend.launcher stops only processes whose *current* command line carries a
# Tacit argv marker (the same proof the startup sweep uses) and derives its
# paths from config.ROOT instead of naming this checkout.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Write-Error "no virtualenv at $VenvPy - run install.ps1 first"
    exit 1
}
Push-Location $Root
try {
    # Stop what is provably ours, wait for the port, and say honestly if it does
    # not free. Only then start, and start it through run.bat so the console this
    # leaves behind is the console a person would have seen anyway.
    & $VenvPy -m backend.launcher prelaunch
    if ($LASTEXITCODE -ne 0) {
        Write-Error "a Tacit server still holds the port; nothing was started"
        exit 1
    }
    & (Join-Path $Root "run.bat")
} finally {
    Pop-Location
}
