@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "DAILY_SIGNAL_ROOT=%~dp0"

if not exist logs mkdir logs
set "DASHBOARD=http://127.0.0.1:8083"

REM Trust the PID file only when it belongs to this project's Uvicorn process.
if exist runtime.pid (
  set /p OLD_PID=<runtime.pid
  powershell -NoProfile -Command "$id=0; if(-not [int]::TryParse('!OLD_PID!',[ref]$id)){exit 0}; $p=Get-CimInstance Win32_Process -Filter ('ProcessId = '+$id) -ErrorAction SilentlyContinue; if(-not $p){exit 0}; $expected=[IO.Path]::GetFullPath((Join-Path $env:DAILY_SIGNAL_ROOT '.venv\Scripts\python.exe')); $actual=''; try{$actual=[IO.Path]::GetFullPath($p.ExecutablePath)}catch{}; if($actual -ieq $expected -and [string]$p.CommandLine -match '(?i)-m\s+uvicorn\s+api:app'){exit 10}; exit 11"
  if errorlevel 10 (
    echo Daily Signal is already running ^(PID !OLD_PID!^).
    start "" "%DASHBOARD%"
    exit /b 0
  )
  del runtime.pid >nul 2>&1
)

REM If port 8083 is occupied, open only a healthy Daily Signal instance.
powershell -NoProfile -Command "$c=Get-NetTCPConnection -LocalPort 8083 -State Listen -ErrorAction SilentlyContinue; if($c){try{$r=Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8083/health' -TimeoutSec 3; if($r.StatusCode -eq 200 -and $r.Content -match '"status"\s*:\s*"ok"'){exit 10}}catch{}; exit 11}; exit 0"
if errorlevel 11 (
  echo Port 8083 is occupied by another or unhealthy process.
  echo Stop that process manually or choose another port before starting Daily Signal.
  pause
  exit /b 1
)
if errorlevel 10 (
  echo Daily Signal is already listening on port 8083. Opening the dashboard...
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

echo Checking Python syntax across the project...
".venv\Scripts\python.exe" -m compileall -q .
if errorlevel 1 (
  echo Python startup check failed. Fix the error shown above before launching.
  pause
  exit /b 1
)

echo Starting Daily Signal on port 8083...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$python=Join-Path $env:DAILY_SIGNAL_ROOT '.venv\Scripts\python.exe'; $p=Start-Process -FilePath $python -ArgumentList @('-m','uvicorn','api:app','--host','0.0.0.0','--port','8083') -WorkingDirectory $env:DAILY_SIGNAL_ROOT -WindowStyle Hidden -RedirectStandardOutput (Join-Path $env:DAILY_SIGNAL_ROOT 'logs\server.log') -RedirectStandardError (Join-Path $env:DAILY_SIGNAL_ROOT 'logs\error.log') -PassThru -ErrorAction Stop; if(-not $p){exit 1}; Set-Content -Path (Join-Path $env:DAILY_SIGNAL_ROOT 'runtime.pid') -Value $p.Id -Encoding ascii"
if errorlevel 1 (
  echo Failed to launch Daily Signal. Check logs\error.log.
  pause
  exit /b 1
)

REM Wait up to 30 seconds for the API to become healthy before opening the browser.
set "READY=0"
for /L %%i in (1,1,30) do (
  powershell -NoProfile -Command "try { $r=Invoke-WebRequest -UseBasicParsing '%DASHBOARD%/health' -TimeoutSec 2; if ($r.StatusCode -eq 200 -and $r.Content -match '"status"\s*:\s*"ok"') { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
  if not errorlevel 1 (
    set "READY=1"
    goto :OPEN_DASHBOARD
  )
  timeout /t 1 /nobreak >nul
)

echo Daily Signal did not become healthy within 30 seconds.
echo Check logs\error.log and logs\server.log for the startup error.
if exist logs\error.log type logs\error.log
call "%~dp0stop.bat"
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
