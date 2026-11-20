@echo off
setlocal
cd /d "%~dp0"
if exist runtime.pid (
  for /f %%P in (runtime.pid) do (
    powershell -NoProfile -Command "if (Get-Process -Id %%P -ErrorAction SilentlyContinue) { exit 10 } else { exit 0 }"
    if not errorlevel 1 del runtime.pid
  )
)
if exist runtime.pid (
  echo Daily Signal is already running or PID file is stale. Check logs and runtime.pid.
  exit /b 1
)
if not exist .venv\Scripts\python.exe (
  echo Virtual environment missing. Run setup in this folder first.
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Start-Process -FilePath '%~dp0.venv\Scripts\python.exe' -ArgumentList @('-m','uvicorn','api:app','--host','0.0.0.0','--port','8083') -WorkingDirectory '%~dp0' -WindowStyle Hidden -RedirectStandardOutput '%~dp0logs\server.log' -RedirectStandardError '%~dp0logs\error.log' -PassThru; $p.Id | Set-Content -Encoding ascii '%~dp0runtime.pid'"
timeout /t 2 /nobreak >nul
powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8083/health -TimeoutSec 3).Content } catch { Write-Output 'Starting in background; check logs if not ready yet.' }"
endlocal
