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
"%VENVPY%" -m backend.main
pause
