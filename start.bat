@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

if not exist logs mkdir logs
set "DASHBOARD=http://127.0.0.1:8083"

REM If a recorded server process is already alive, open its dashboard.
if exist runtime.pid (
  set /p OLD_PID=<runtime.pid
  powershell -NoProfile -Command "if (Get-Process -Id !OLD_PID! -ErrorAction SilentlyContinue) { exit 10 } else { exit 0 }"
  if errorlevel 10 (
    echo Daily Signal is already running ^(PID !OLD_PID!^).
    start "" "%DASHBOARD%"
    exit /b 0
  ) else (
    del runtime.pid >nul 2>&1
  )
)

REM If another process owns port 8083, open the existing local dashboard.
powershell -NoProfile -Command "$c=Get-NetTCPConnection -LocalPort 8083 -State Listen -ErrorAction SilentlyContinue; if ($c) { exit 10 } else { exit 0 }"
if errorlevel 10 (
  echo Port 8083 is already in use. Opening the existing dashboard...
  start "" "%DASHBOARD%"
  exit /b 0
)

if not exist .venv\Scripts\python.exe (
  echo Virtual environment missing.
  echo Run once:
  echo   python -m venv .venv
  echo   .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)

echo Checking Python startup syntax...
".venv\Scripts\python.exe" -m py_compile api.py
if errorlevel 1 (
  echo Python startup check failed. Fix the error shown above before launching.
  pause
  exit /b 1
)

echo Starting Daily Signal on port 8083...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Start-Process -FilePath '%~dp0.venv\Scripts\python.exe' -ArgumentList @('-m','uvicorn','api:app','--host','0.0.0.0','--port','8083') -WorkingDirectory '%~dp0' -WindowStyle Hidden -RedirectStandardOutput '%~dp0logs\server.log' -RedirectStandardError '%~dp0logs\error.log' -PassThru; Set-Content -Path '%~dp0runtime.pid' -Value $p.Id -Encoding ascii"

REM Wait up to 30 seconds for the API to become healthy before opening the browser.
set "READY=0"
for /L %%i in (1,1,30) do (
  powershell -NoProfile -Command "try { $r=Invoke-WebRequest -UseBasicParsing '%DASHBOARD%/health' -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
  if not errorlevel 1 (
    set "READY=1"
    goto :OPEN_DASHBOARD
  )
  timeout /t 1 /nobreak >nul
)

echo Daily Signal did not become healthy within 30 seconds.
echo Check logs\error.log and logs\server.log for the startup error.
if exist logs\error.log type logs\error.log
pause
exit /b 1

:OPEN_DASHBOARD
echo Daily Signal is ready.
echo Local dashboard: %DASHBOARD%
echo Remote dashboard: http://YOUR-VPS-IP:8083
echo Allow inbound TCP 8083 in the VPS firewall, restricted to your own IP.
echo WARNING: Dashboard has no login protection; do NOT expose port 8083 to everyone.
start "" "%DASHBOARD%"
endlocal
