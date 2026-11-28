@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist logs mkdir logs
set "PYTHON=.venv\Scripts\python.exe"

echo ==========================================
echo Daily Signal - startup diagnostics
echo ==========================================

if not exist "%PYTHON%" (
  echo ERROR: Virtual environment is missing.
  echo Run these commands once from this folder:
  echo   python -m venv .venv
  echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
  pause
  exit /b 1
)

echo [1/3] Checking required Python packages...
"%PYTHON%" -c "import fastapi, uvicorn, ccxt, websocket, pandas, numpy"
if errorlevel 1 (
  echo ERROR: One or more dependencies are missing or broken.
  echo Installing requirements now...
  "%PYTHON%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo ERROR: Dependency installation failed. See the error above.
    pause
    exit /b 1
  )
)

echo [2/3] Checking Python syntax...
"%PYTHON%" -m py_compile api.py
if errorlevel 1 (
  echo.
  echo [AUTO-FIX] api.py syntax problem detected. Trying offline repair...
  if not exist scripts\repair_api.py (
    echo ERROR: Offline repair helper is missing from this downloaded folder.
    echo Please extract the complete repository ZIP again.
    pause
    exit /b 1
  )
  "%PYTHON%" scripts\repair_api.py
  if errorlevel 1 (
    echo ERROR: This syntax issue cannot be safely auto-repaired.
    echo Original api.py has been preserved. See the error above.
    pause
    exit /b 1
  )
  "%PYTHON%" -m py_compile api.py
  if errorlevel 1 (
    echo ERROR: api.py is still invalid after offline repair.
    pause
    exit /b 1
  )
)
"%PYTHON%" -m compileall -q scanner
if errorlevel 1 (
  echo ERROR: A Python syntax problem was found under scanner.
  pause
  exit /b 1
)

echo [3/3] Checking port 8083...
powershell -NoProfile -Command "$c=Get-NetTCPConnection -LocalPort 8083 -State Listen -ErrorAction SilentlyContinue; if ($c) { Write-Host 'ERROR: Port 8083 is already in use.'; $c | Format-Table -AutoSize; exit 1 } else { exit 0 }"
if errorlevel 1 (
  echo Stop the old Daily Signal process or change the port before restarting.
  pause
  exit /b 1
)

echo.
echo Starting Daily Signal hidden in the background...
echo Waiting for the server health check. Dashboard will NOT be opened.
echo Logs: logs\server.log and logs\server-error.log

powershell -NoProfile -ExecutionPolicy Bypass -Command "$python=(Resolve-Path '%PYTHON%').Path; $root=(Get-Location).Path; $out=Join-Path $root 'logs\server.log'; $err=Join-Path $root 'logs\server-error.log'; $pidfile=Join-Path $root 'logs\server.pid'; $p=Start-Process -FilePath $python -ArgumentList '-m uvicorn api:app --host 0.0.0.0 --port 8083' -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err -PassThru; Set-Content -Path $pidfile -Value $p.Id; for($i=0;$i -lt 45;$i++){Start-Sleep -Seconds 1; $p.Refresh(); if($p.HasExited){Write-Host ('ERROR: Server process exited with code ' + $p.ExitCode); if(Test-Path $err){Get-Content $err -Tail 40}; exit 1}; try{$r=Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8083/health' -TimeoutSec 2;if($r.StatusCode -eq 200){Write-Host 'Daily Signal server is running successfully (hidden).'; exit 0}}catch{}}; Write-Host 'ERROR: Server did not become healthy within 45 seconds.'; Write-Host 'Check logs\server-error.log and logs\server.log for details.'; if(Test-Path $err){Get-Content $err -Tail 40}; exit 1"
if errorlevel 1 (
  echo.
  echo STARTUP FAILED. Error details are shown above.
  echo Full logs: logs\server-error.log and logs\server.log
  pause
  exit /b 1
)

echo Startup successful. Closing this window; server stays hidden in background.
endlocal
exit /b 0
