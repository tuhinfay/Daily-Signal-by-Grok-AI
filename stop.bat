@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "DAILY_SIGNAL_ROOT=%~dp0"

echo Stopping Daily Signal...

if not exist runtime.pid (
  echo No runtime.pid found. Daily Signal may already be stopped.
  goto :END
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$id=0; if(-not [int]::TryParse((Get-Content -Raw 'runtime.pid').Trim(),[ref]$id) -or $id -le 0){Write-Output 'Invalid runtime.pid. No process was stopped.'; exit 2}; $p=Get-CimInstance Win32_Process -Filter ('ProcessId = '+$id) -ErrorAction SilentlyContinue; if(-not $p){Write-Output 'Daily Signal process is already stopped.'; exit 0}; $expected=[IO.Path]::GetFullPath((Join-Path $env:DAILY_SIGNAL_ROOT '.venv\Scripts\python.exe')); $actual=''; try{$actual=[IO.Path]::GetFullPath($p.ExecutablePath)}catch{}; if($actual -ine $expected -or [string]$p.CommandLine -notmatch '(?i)-m\s+uvicorn\s+api:app'){Write-Output 'PID does not belong to this project. Refusing to stop another process.'; exit 3}; Stop-Process -Id $id -Force -ErrorAction Stop; Wait-Process -Id $id -Timeout 10 -ErrorAction SilentlyContinue; Write-Output ('Stopped Daily Signal (PID '+$id+').'); exit 0"
set "STOP_RESULT=%ERRORLEVEL%"

if "%STOP_RESULT%"=="0" (
  del runtime.pid >nul 2>&1
) else (
  echo Stop check failed. runtime.pid was kept for inspection.
)

:END
echo Done.
endlocal
