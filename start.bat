@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist logs mkdir logs

REM If PID file exists but process is dead, clear it
if exist runtime.pid (
  set /p OLD_PID=<runtime.pid
  powershell -NoProfile -Command "if (Get-Process -Id %OLD_PID% -ErrorAction SilentlyContinue) { exit 10 } else { exit 0 }"
  if not errorlevel 10 (
    del runtime.pid >nul 2>&1
  ) else (
    echo Daily Signal already running ^(PID %OLD_PID%^).
    echo Health: http://127.0.0.1:8083/health
    exit /b 0
  )
)

REM Also block if something already listens on 8083
powershell -NoProfile -Command "$c=Get-NetTCPConnection -LocalPort 8083 -State Listen -ErrorAction SilentlyContinue; if ($c) { exit 10 } else { exit 0 }"
if errorlevel 10 (
  echo Port 8083 already in use. Run stop.bat first.
  exit /b 1
)

if not exist .venv\Scripts\python.exe (
  echo Virtual environment missing.
  echo Run once:
  echo   python -m venv .venv
  echo   .venv\Scripts\pip install -r requirements.txt
  exit /b 1
)

echo Starting Daily Signal on port 8083...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Start-Process -FilePath '%~dp0.venv\Scripts\python.exe' -ArgumentList @('-m','uvicorn','api:app','--host','0.0.0.0','--port','8083') -WorkingDirectory '%~dp0' -WindowStyle Hidden -RedirectStandardOutput '%~dp0logs\server.log' -RedirectStandardError '%~dp0logs\error.log' -PassThru; Set-Content -Path '%~dp0runtime.pid' -Value $p.Id -Encoding ascii"

timeout /t 2 /nobreak >nul

powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8083/health -TimeoutSec 5).Content } catch { Write-Output 'Started in background. If health fails, check logs\error.log' }"
echo.
echo Dashboard: http://127.0.0.1:8083
endlocal
