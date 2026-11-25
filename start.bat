@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist logs mkdir logs
set "DASHBOARD=http://127.0.0.1:8083"
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
"%PYTHON%" -m compileall -q api.py scanner
if errorlevel 1 (
  echo ERROR: Python syntax check failed. See the error above.
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
echo Starting Daily Signal in the background...
echo Server logs: logs\server.log and logs\server-error.log

powershell -NoProfile -ExecutionPolicy Bypass -Command "$python=(Resolve-Path '%PYTHON%').Path; $root=(Get-Location).Path; $out=Join-Path $root 'logs\server.log'; $err=Join-Path $root 'logs\server-error.log'; $p=Start-Process -FilePath $python -ArgumentList '-m uvicorn api:app --host 0.0.0.0 --port 8083' -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err -PassThru; Set-Content -Path (Join-Path $root 'logs\server.pid') -Value $p.Id"
if errorlevel 1 (
  echo ERROR: Could not launch the background server.
  pause
  exit /b 1
)

REM Open the dashboard after the health endpoint responds. This runs separately,
REM so this launcher can close immediately and leave no visible CMD window.
start "" /min powershell -NoProfile -ExecutionPolicy Bypass -Command "$u='%DASHBOARD%/health'; for($i=0;$i -lt 60;$i++){try{$r=Invoke-WebRequest -UseBasicParsing $u -TimeoutSec 2;if($r.StatusCode -eq 200){Start-Process '%DASHBOARD%';exit 0}}catch{};Start-Sleep -Seconds 1}"

echo Daily Signal launched in the background. This window will close.
endlocal
exit /b 0
