@echo off
setlocal
REM Tacit — start the web host.
set "TACIT_USER_HOME=%USERPROFILE%"
if not defined TACIT_HOME set "TACIT_HOME=%TACIT_USER_HOME%\.tacit"
if not defined TACIT_PORT set "TACIT_PORT=8550"
cd /d "%~dp0"
set "VENVPY=%~dp0.venv\Scripts\python.exe"
REM Tacit runs on its own interpreter or not at all. Falling back to whatever
REM `python` happens to be first on PATH means importing another tool's
REM packages, and possibly a version of them Tacit cannot use.
if not exist "%VENVPY%" (
  echo.
  echo Tacit needs its own Python environment before it can start.
  echo.
  echo   run install.ps1 - it does both and writes a launcher.
  echo.
  echo Or do it by hand with a BASE Python, not another project's virtualenv:
  echo a venv built by one records that interpreter in pyvenv.cfg and ties Tacit
  echo to a tool you did not choose. install.ps1 checks this for you.
  echo.
  echo   ^<base-python^> -m venv .venv
  echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)
echo [tacit] http://localhost:%TACIT_PORT%   state: %TACIT_HOME%
rem Stop what is provably Tacit's and wait for the port, before anything is
rem started. That step is Python, not batch: proving a process is ours means
rem reading its command line, and the two lines that used to do it here matched
rem python.exe and uvicorn.exe by name, which is how a stop step could hit an
rem unrelated Python. The Restart button runs this same script, so there is one
rem launch path to read, fix and trust.
"%VENVPY%" -m backend.launcher prelaunch
if errorlevel 1 (
  echo.
  echo Tacit did not start: a Tacit server is still holding the port.
  echo   Nothing was launched, so there is nothing to lose to a bind race.
  echo   See the output above, and restart.log in the Tacit state folder.
  echo.
  pause
  exit /b 1
)
rem The marker token is what lets any later stop step prove this process is
rem Tacit's. It sits where the interpreter ignores it, so a server started here
rem looks like one started by the watcher.
"%VENVPY%" -m backend.main tacit-server
pause
