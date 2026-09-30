@echo off
setlocal
REM Tacit — start the web host.
set "TACIT_USER_HOME=%USERPROFILE%"
if not defined TACIT_HOME set "TACIT_HOME=%TACIT_USER_HOME%\.tacit"
if not defined TACIT_PORT set "TACIT_PORT=8550"
cd /d "%~dp0"
echo [tacit] http://localhost:%TACIT_PORT%   state: %TACIT_HOME%
python -m backend.main
pause
